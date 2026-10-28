"""Exact-value handling.

Technical values get special treatment: they are extracted from source spans,
kept in *literal* form (exactly as written) and only then normalised.  Nothing
may be rounded, reformatted or silently rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .commands import is_executable, program_name

VERSION_RE = re.compile(
    r"(?<![\w.])(?:v|V)?\d+\.\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.\-]+)?(?![\w])"
)
UNITS = (
    "mA|mV|kV|kΩ|MΩ|Ω|Hz|kHz|MHz|GHz|nm|µm|mm|cm|km|kg|mg|lb|ft|in|"
    "rpm|°C|°F|K|W|kW|mW|Wh|mAh|Ah|MB|GB|TB|KB|KiB|MiB|GiB|bps|kbps|Mbps|Gbps|"
    "%|ms|µs|ns|s|min|h|px|pt|em|dpi|lumen|torx|psi|bar|N|Nm|pF|nF|µF|uF|F|"
    "V|VDC|VAC|mA|A|rpm"
)
MEASURE_RE = re.compile(
    rf"(?<![\w.])(\d+(?:[.,]\d+)?)\s*({UNITS})(?![\w])"
)
PORT_RE = re.compile(r"(?<![\w.])port\s*[:=]?\s*(\d{2,5})(?!\d)(?!\.\d)", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
HEX_RE = re.compile(r"\b(?:0[xX])[0-9a-fA-F]+\b")
APPSEC_RE = re.compile(r"\b\d+\.\d+\.\d+(?:\.\d+)?\b")
PATH_RE = re.compile(
    r"(?:~/|/)(?:[\w.\-@]+/)+[\w.\-@]*|(?:[A-Za-z]:\\)(?:[\w.\- ]+\\)*[\w.\- ]*"
)
KEYVALUE_RE = re.compile(
    r"\b([A-Za-z_][\w.\-]{2,40})\s*[=:]\s*([^\s,;]{1,64})(?![\w])"
)
FLAG_RE = re.compile(r"(?<![\w])(--?[A-Za-z][\w-]{1,32})(?:[= ]([^\s,;)]{1,48}))?")
# A command may appear alone, behind a shell prompt, or after a spoken cue such
# as "run the command ...".  The cue form is what tutorials actually produce and
# it carries the reproducible instruction.
COMMAND_CUE_RE = re.compile(
    r"\b(?:the\s+command|the\s+commands|command|commands|run|running|type|typing|"
    r"execute|executing)[:,]?\s+",
    re.IGNORECASE,
)
_OPERATOR_SPLIT = re.compile(r"\|\||&&|[;|]")
_PROGRAM_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.+-]*$")
_FLAG_RE = re.compile(r"-{1,2}[A-Za-z][\w-]*$")
_PATHISH = re.compile(r"^(\.{0,2}/|~/|/|[A-Za-z]:\\\\)")
_WRAPPERS = {"sudo", "doas", "env", "nohup", "timeout", "then", "and", "the", "a"}

_VALUE_NORM: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\s*=\s*"), "="),
    (re.compile(r"\s*:\s*"), ":"),
    (re.compile(r"\s{2,}"), " "),
]


@dataclass(frozen=True)
class Literal:
    """One exact value found in the source, with the span it came from."""

    kind: str
    literal: str
    normalized: str = ""
    context: str = ""
    t_start: float | None = None
    t_end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "literal": self.literal,
            "normalized": self.normalized or normalize_value(self.literal),
            "context": self.context[:240],
            "t_start": self.t_start,
            "t_end": self.t_end,
        }


def normalize_value(value: str) -> str:
    """Collapse cosmetic differences only.  Digits and case are preserved."""
    s = str(value).strip().strip("`'\"")
    for pattern, repl in _VALUE_NORM:
        s = pattern.sub(repl, s)
    return s


# Ordered scanners: high-confidence identifiers first so their spans are
# claimed before looser patterns run. (pattern, kind, ctx_before, ctx_after,
# value_fn) — value_fn None means "the whole match".
_SCANNERS: tuple[tuple["re.Pattern[str]", str, int, int, Any], ...] = (
    (IP_RE, "ip_address", 30, 30, None),
    (MAC_RE, "mac_address", 30, 30, None),
    (UUID_RE, "uuid", 30, 30, None),
    (HEX_RE, "hex", 30, 30, None),
    (MEASURE_RE, "measurement", 40, 40, None),
    (PORT_RE, "port", 30, 30, lambda m: f"port {m.group(1)}"),
    (APPSEC_RE, "version", 40, 40, None),
    (VERSION_RE, "version", 40, 40, None),
    (PATH_RE, "path", 30, 20, lambda m: m.group(0).rstrip(".,;:")),
    (KEYVALUE_RE, "keyvalue", 25, 25, lambda m: f"{m.group(1)}={m.group(2)}"),
    (FLAG_RE, "flag", 25, 25,
     lambda m: m.group(1) + (f" {m.group(2)}" if m.group(2) else "")),
)


class _LiteralCollector:
    """Accumulates deduplicated literals with span-claim tracking."""

    def __init__(self, *, t_start: float | None = None,
                 t_end: float | None = None) -> None:
        self.out: list[Literal] = []
        self.seen: set[str] = set()
        self.claimed: list[tuple[int, int]] = []
        self.t_start = t_start
        self.t_end = t_end

    def free(self, start: int, end: int) -> bool:
        return not any(a <= start < b for a, b in self.claimed)

    def add(self, kind: str, value: str, context: str,
            span: tuple[int, int] | None = None) -> None:
        value = value.strip().strip("`'\"")
        if not value or len(value) < 2:
            return
        key = f"{kind}:{normalize_value(value).lower()}"
        if key in self.seen:
            return
        self.seen.add(key)
        self.out.append(
            Literal(
                kind=kind,
                literal=value,
                context=context.strip()[:240],
                t_start=self.t_start,
                t_end=self.t_end,
            )
        )
        if span:
            self.claimed.append(span)


def extract_literals(
    text: str,
    *,
    t_start: float | None = None,
    t_end: float | None = None,
    max_values: int = 40,
) -> list[Literal]:
    """Pull exact technical values out of a span of source text.

    High-confidence identifiers (IP, MAC, UUID, hex) claim their character spans
    first, so a dotted quad is never also reported as a "version".
    """
    if not text:
        return []
    acc = _LiteralCollector(t_start=t_start, t_end=t_end)

    for pattern, kind, before, after, value_fn in _SCANNERS:
        for m in pattern.finditer(text):
            if not acc.free(m.start(), m.end()):
                continue
            value = value_fn(m) if value_fn else m.group(0)
            acc.add(kind, value, text[max(0, m.start() - before):m.end() + after], m.span())

    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 4:
            continue
        for fragment in _command_fragments(stripped):
            acc.add("command", _clean_command(fragment), stripped[:240])

    return acc.out[:max_values]


_LEADING_CONJUNCTION = re.compile(
    r"^(?:then|and|now|next|after\s+that|finally|first|also|so)\s+", re.IGNORECASE)
_TRAILING_NARRATION = re.compile(
    r"\s+(?:and\s+)?(?:then\s+)?(?:press|hit|type|enter|click|run|choose|select|put|give|"
    r"wait|keep|make\s+sure|be\s+careful|you\s+should|you\s+will|you\s+can|to\s+continue|"
    r"to\s+do\s+this|if\s+needed|as\s+needed|and\s+so\s+on)\b.*$",
    re.IGNORECASE,
)


def _clean_command(candidate: str) -> str:
    """Strip narration around a command without touching the command itself."""
    text = candidate.strip().lstrip("$#> ").strip()
    for _ in range(4):
        stripped = COMMAND_CUE_RE.sub("", text, count=1).strip()
        stripped = _LEADING_CONJUNCTION.sub("", stripped).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    text = _TRAILING_NARRATION.sub("", text)
    return text.rstrip(".,;: ").strip()


def _command_fragments(line: str) -> list[str]:
    """Recover command fragments from one line of source text.

    Handles a bare command line (possibly prompt-prefixed), a pipeline split on
    ``&&``/``;``/``|``, and the "run the command ..." narrative form.
    """
    out: list[str] = []
    for body, permissive in _fragment_bodies(line):
        for fragment in _OPERATOR_SPLIT.split(body):
            cleaned = _normalise_fragment(fragment, permissive=permissive)
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _fragment_bodies(line: str) -> list[tuple[str, bool]]:
    """Candidate command bodies: cue-following text first (permissive), then
    the bare line (conservative)."""
    cues: list[str] = []
    for m in COMMAND_CUE_RE.finditer(line):
        cues.append(line[m.end():])
    bodies = cues + [line]
    return [(body, bool(cues) and body is cues[0]) for body in bodies]


def _normalise_fragment(fragment: str, *, permissive: bool) -> str:
    """Length/punctuation gate, acceptance test and narration strip."""
    frag = fragment.strip().lstrip("$#> ").strip()
    if len(frag) < 3 or len(frag) > 160:
        return ""
    if frag.rstrip()[-1:] in ".,;:!?":
        return ""
    if not looks_like_command(frag, permissive=permissive):
        return ""
    return _clean_command(frag)


def looks_like_command(fragment: str, *, permissive: bool = False) -> bool:
    """Conservative test: a program name plus a flag, a path, or a known tool.

    A bare line must start with a *known* program, otherwise a sentence that
    merely ends with a flag-like word would be captured.  Text that follows an
    explicit cue ("run the command ...") is allowed the wider test, because the
    narrator has already told us this is a command.
    """
    frag = fragment.strip()
    if not frag or frag[-1] in ".,;:!?":
        return False
    parts = frag.split()
    while parts and parts[0].lower().strip(":") in _WRAPPERS:
        parts = parts[1:]
    if not parts:
        return False
    name = parts[0].rstrip(":")
    if not _PROGRAM_RE.fullmatch(name):
        return False
    rest = parts[1:]
    if is_executable(name):
        return True
    if not permissive or not rest:
        return False
    if any(_FLAG_RE.fullmatch(t) for t in rest):
        return True
    if any(_PATHISH.match(t) for t in rest):
        return True
    return len(rest) <= 3 and any("=" in t for t in rest)


def dedupe_literals(literals: Iterable[Literal]) -> list[Literal]:
    out: list[Literal] = []
    seen: set[str] = set()
    for lit in literals:
        key = f"{lit.kind}:{normalize_value(lit.literal).lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(lit)
    return out


def values_match(a: str, b: str) -> bool:
    """True when two value strings denote the same technical value."""
    na, nb = normalize_value(a).lower(), normalize_value(b).lower()
    if na == nb:
        return True
    na2, nb2 = na.replace(" ", "").replace("'", "").replace('"', ""), nb.replace(" ", "").replace("'", "").replace('"', "")
    return na2 == nb2


def article_value_supported(value: str, supported: Iterable[str]) -> str | None:
    """Return the matching supported value, or None."""
    for cand in supported:
        if values_match(value, cand):
            return cand
    return None
