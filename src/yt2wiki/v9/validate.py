"""Type-aware publication validation, Pass 5 (PR C).

Deterministic checks that run after image selection and before
``v9.publish.persist``. Three outcomes:

- ``ready``: publish normally;
- ``needs_review``: a meaningful candidate exists but a critical check
  failed — keep a private candidate plus a finding ledger, do NOT
  overwrite an existing published canonical article;
- ``refused``: no meaningful article (all sections empty/no-speech).

Checks (all deterministic — no model critique can green-light a
critical finding):

1. type/slot invariants: closed enum, template version, frozen type,
   required and allowed blocks, no ungrounded executable blocks;
2. exact-value/entity ledger: every exact literal in the article must
   be supported by the section's own evidence spans (King Arthur
   quantities, Vox $300k, Dimorphos, Git quoting are regression
   fixtures); mid-sentence proper names must occur in the transcript;
3. temporal: spans inside the source, overlapping real segments;
4. origin/role: section spans never overlap decision-excluded
   (sponsor/housekeeping) spans;
5. coverage: every non-excluded window is represented or explicitly
   listed as unassigned (unrepresented substantive windows are
   findings, capped);
6. reader integrity: no internal ids, raw dicts, model-emitted HTML
   or decision-trace language in prose.

Cosmetic warnings never fail publication: only ``critical`` findings
produce ``needs_review``.
"""

from __future__ import annotations

import re
from typing import Any

from .. import literals as _literals
from . import article_types as AT
from .values import extract_exact_values as _extract_values

#: Finding dicts carry code/severity/detail and optional section_id.
CRITICAL = "critical"
MAJOR = "major"

_INTERNAL_ID = re.compile(
    r"\bev-[0-9a-f]{4,}\b|\bsec-[0-9a-z-]{4,}\b|\bw-\d{3}\b|\bfact-[\w-]+\b")
_BALANCED_CODE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_RAW_HTML = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
#: A name never spans a line break: headings and list items sit on
#: their own lines, and a match reaching across one is two fragments,
#: not a person or product.
_NAME = re.compile(r"\b((?:[A-Z][a-z]{2,}|[A-Z]{2,})[A-Za-z']*"
                    r"(?:[ \t]+(?:[A-Z][a-z]{2,}|[A-Z]{2,})[A-Za-z']*)?)\b")
_SENTENCE_END = re.compile(r"[.!?]\s*$")
#: Speaker-attribution constructions: even sentence-initial, the named
#: voice must exist in the source. Checked for discussion/report types
#: where invented attribution is the characteristic failure.
_ATTRIBUTION = re.compile(
    r"\b([A-Z][a-z]{2,}(?:[ \t]+[A-Z][a-z]{2,})?)[ \t]+"
    r"(?:says?|said|argues?|argued|claims?|claimed|notes?|noted|adds?|"
    r"added|explains?|explained|warns?|warned|asks?|asked|replies?|"
    r"replied|responds?|responded|believes?|thinks?|thinks)\b"
    r"|\baccording\s+to\s+([A-Z][a-z]{2,}(?:[ \t]+[A-Z][a-z]{2,})?)\b")

#: Ordinary capitalized words that are never proper-name evidence.
_NAME_ALLOWLIST = frozenset(
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December", "The", "This", "That", "These", "Those", "When", "Where", "What", "Which", "While", "Because", "Since", "Before", "After", "During", "There", "Here", "Video", "Audio", "Chapter", "Section", "Figure", "Table", "First", "Second", "Third", "Next", "Then", "Finally", "Also", "Just", "Even", "Still", "Yet", "Already", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"])

_KNOWN_UNITS = frozenset(["V", "volts", "A", "amps", "W", "watts", "Hz", "kHz", "MHz", "GHz", "nits", "dB", "dBm", "FPS", "fps", "RPM", "rpm", "GB", "MB", "KB", "TB", "mph", "kph", "km", "mi", "ft", "feet", "inch", "inches", "mm", "cm", "m", "s", "ms", "min", "hrs", "percent", "USD", "EUR", "cents", "dollars", "OLED", "LCD", "LED", "HDR", "IPS", "WiFi", "Bluetooth", "USB", "HDMI", "NVMe", "SSD", "CPU", "GPU", "APU", "RAM", "AMD", "Intel", "Valve", "LTT", "PBS", "Vox", "TLD", "DIY", "OS", "API", "URL", "PDF", "JSON", "HTML", "CSS", "AM", "PM", "EDT", "PDT", "UTC", "DIY", "AI", "RL", "LLM", "RSI", "GRPO", "RLVR", "SFT", "DART", "NASA", "PBS", "LTT", "WVFRM"])


def _finding(code: str, severity: str, detail: str,
             section_id: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"code": code, "severity": severity, "detail": detail}
    if section_id:
        f["section_id"] = section_id
    return f


def _spans_of(section: dict[str, Any]) -> list[list[float]]:
    spans = section.get("source_windows") or []
    out = []
    for w in spans:
        try:
            out.append([float(w[0]), float(w[1])])
        except (TypeError, ValueError, IndexError):
            continue
    if not out:
        try:
            out.append([float(section.get("t_start", 0) or 0),
                        float(section.get("t_end", 0) or 0)])
        except (TypeError, ValueError):
            pass
    return out


def _evidence_text(segs: list[dict[str, Any]],
                   spans: list[list[float]]) -> str:
    lines = []
    for a, b in spans:
        for r in segs:
            try:
                if float(r["end"]) < a or float(r["start"]) > b:
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            lines.append(str(r.get("text") or ""))
    return "\n".join(lines)


def check_type_slots(decision: dict[str, Any], plan: dict[str, Any],
                     article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 1: the frozen type survives through to the article."""
    out: list[dict[str, Any]] = []
    tid = decision.get("template_id")
    if AT.validate_selection(decision):
        return [_finding("bad_decision", CRITICAL,
                         "stored type decision fails closed-enum validation")]
    if tid != article.get("article_type"):
        out.append(_finding("type_mismatch", CRITICAL,
                            f"article type {article.get('article_type')!r} != "
                            f"frozen {tid!r}"))
    if (decision.get("template_version") is not None
            and article.get("template_version") is not None
            and decision.get("template_version")
            != article.get("template_version")):
        out.append(_finding("template_version_mismatch", CRITICAL,
                            "article template version != frozen decision"))
    if AT.validate_plan(plan, tid or ""):
        out.append(_finding("bad_plan", CRITICAL,
                            "stored plan fails the frozen slot contract"))
        return out
    allowed: set[str] = set()
    tdef = AT.type_definition(tid or "")
    if tdef:
        allowed = set(tdef["core_slots"]) | set(tdef["optional_slots"])
    for sec in article.get("sections") or []:
        slot = sec.get("slot")
        if slot not in allowed:
            out.append(_finding("foreign_slot", CRITICAL,
                                f"section {sec.get('section_id')!r} uses "
                                f"slot {slot!r} outside {tid}",
                                sec.get("section_id")))
    return out


def check_executable_blocks(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 1b: only procedures carry executable blocks.

    A fenced block in a non-procedure article is an ungrounded action
    unless its content is quoted discussion/reporting, not instruction.
    """
    out: list[dict[str, Any]] = []
    if article.get("article_type") == "procedure":
        return out
    for sec in article.get("sections") or []:
        body = str(sec.get("body") or "")
        for m in _FENCE.finditer(body):
            block = m.group(0)
            inner = "\n".join(ln for ln in block.splitlines()[1:-1]
                              if ln.strip())[:400]
            if not inner:
                continue
            if _literals.looks_like_command(inner, permissive=True):
                out.append(_finding(
                    "ungrounded_executable_block", CRITICAL,
                    f"fenced command-like block in {article.get('article_type')} "
                    f"article: {inner[:120]!r}", sec.get("section_id")))
            else:
                out.append(_finding(
                    "fenced_block_outside_procedure", MAJOR,
                    "fenced block in a non-procedure article; prose preferred",
                    sec.get("section_id")))
    return out


_MD_LINK_TARGET = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)>\]]+")
_BARE_KEYVALUE_VALUE = re.compile(r"[\W_]*$")
_LIST_MARKER = re.compile(r"(?m)^(\s*)(?:[*\-+]\s+|\d+[.)]\s+)")
#: Assignment-form keyvalues only: `key=value` or `key:value` with no
#: space after the separator. `Label: description` with a space is
#: English definition-list prose (ubiquitous in typed articles), never
#: an exact technical value.
_STRICT_KV = re.compile(r"\b([A-Za-z_][\w.\-]{2,40})\s*(=|:(?=\S))([^\s,;]{1,64})")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PERCENT_WORD = re.compile(r"(\d+(?:\.\d+)?)\s+percent\b", re.IGNORECASE)

#: English number words the transcript spells out while prose (and the
#: reader) uses digits: both sides are numeralised before value
#: comparison so "two and a half teaspoons" supports "2 1/2 teaspoons".
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "dozen": "12",
}
_AND_A_HALF = re.compile(r"\b(\w+)\s+and\s+a\s+half\b", re.IGNORECASE)
_AND_A_QUARTER = re.compile(r"\b(\w+)\s+and\s+a\s+quarter\b", re.IGNORECASE)
_A_HALF = re.compile(r"\ba\s+half\b", re.IGNORECASE)


def _a_unit_re() -> re.Pattern:
    units = ("cups?|teaspoons?|tablespoons?|tsp|tbsp|ounces?|oz|pounds?|"
             "lbs?|grams?|degrees?|hertz|miles?|feet|foot|inches?|nits|"
             "liters?|milliliters?|pints?|quarts?|gallons?|minutes?|"
             "hours?|days?|weeks?|months?|years?|loaves?|servings?|"
             "dollars?|cents?")
    return re.compile(rf"\b[an]+\s+(?=(?:{units})\b)", re.IGNORECASE)


_A_UNIT = _a_unit_re()

#: Spelled-out storage units the transcript uses while prose (and the
#: reader) abbreviates: canonicalised symmetrically on both sides.
_STORAGE_UNIT = re.compile(r"\b(giga|mega|kilo|tera)bytes?\b", re.IGNORECASE)
_STORAGE_ABBR = {"giga": "GB", "mega": "MB", "kilo": "KB", "tera": "TB"}
_NUMBER_WORD = re.compile(r"\b(zero|one|two|three|four|five|six|seven|eight|"
                           r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                           r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
                           r"forty|fifty|sixty|seventy|eighty|ninety|hundred|"
                           r"dozen)\b", re.IGNORECASE)


def _numeralize(text: str) -> str:
    text = _AND_A_HALF.sub(r"\1 1/2", text)
    text = _AND_A_QUARTER.sub(r"\1 1/4", text)
    text = _A_HALF.sub("1/2", text)
    text = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)
    text = _A_UNIT.sub("1 ", text)
    text = _STORAGE_UNIT.sub(lambda m: _STORAGE_ABBR[m.group(1).lower()], text)
    # "110%" and "110 percent" denote the same value.
    text = _PERCENT_WORD.sub(r"\1%", text)
    return text


def _prose_values_text(body: str) -> str:
    """Prose with link machinery removed: labels stay, targets go.

    Link targets and bare URLs are navigation, not claims: the shared
    keyvalue scanner would otherwise read `https=...` as an exact
    value and every cited article would fail its own gate. Markdown
    emphasis is stripped so `**Label:**` list markup never becomes a
    `Label=**` keyvalue. Number words become digits symmetrically on
    both sides of the comparison.
    """
    text = _MD_LINK_TARGET.sub(r"\1", body)
    text = _BARE_URL.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ")
    return _numeralize(text)


def check_exact_values(article: dict[str, Any],
                       segs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invariant 2a: every exact literal in prose is source-supported.

    Direction is article -> evidence: dropped values are omissions
    (coverage), invented ones are critical. Comparison normalises
    cosmetically but preserves the original literal.
    """
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = _prose_values_text(str(sec.get("body") or ""))
        spans = _spans_of(sec)
        evidence = _evidence_text(segs, spans)
        if not evidence.strip():
            continue
        supported = [lit.literal for lit in
                     _extract_values(_numeralize(evidence), max_values=200)]
        strict_kv = {m.group(1) + "=" + m.group(3)
                     for m in _STRICT_KV.finditer(body)}
        for lit in _extract_values(body, max_values=200):
            if lit.kind == "keyvalue" and lit.literal not in strict_kv:
                # `Label: description` is definition-list prose, not an
                # assignment claim: skip, do not flag.
                continue
            if _literals.article_value_supported(lit.literal, supported) is None:
                out.append(_finding(
                    "unsupported_exact_value", CRITICAL,
                    f"{lit.kind} {lit.literal!r} has no source span in "
                    f"this section's evidence", sec.get("section_id")))
    return out


def check_entities(article: dict[str, Any],
                   segs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invariant 2b: mid-sentence proper names occur in the transcript.

    Sentence-initial capitalisation is prose position, not evidence;
    allowlisted common words and known units never fire. A name with a
    near-identical transcript neighbour (Dimorphus vs Dimorphos) is a
    corruption — critical. A name with no neighbour at all may be an
    ASR gap rather than an invention — major, for operator adjudication.
    """
    import difflib

    out: list[dict[str, Any]] = []
    transcript = "\n".join(str(r.get("text") or "") for r in segs)
    lowered = transcript.lower()
    tokens = set(re.findall(r"[a-z][a-z']+", lowered))

    def _check_name(name: str, sec_id: str | None) -> None:
        head = name.split()[0]
        if head in _NAME_ALLOWLIST or head in _KNOWN_UNITS:
            return
        parts = name.lower().split()
        if name.lower() in lowered or all(p in tokens for p in parts):
            return
        close = difflib.get_close_matches(head.lower(), tokens, n=1,
                                          cutoff=0.85)
        if close and head.lower().rstrip("s") == close[0].rstrip("s"):
            # A bare plural ("Conditions" for "condition") is style, not
            # corruption: major, for operator adjudication.
            severity, why = MAJOR, "plural of a transcript word"
        elif close:
            severity, why = CRITICAL, f"near {close[0]!r} — likely corruption"
        else:
            severity, why = MAJOR, "no transcript neighbour — ASR gap or invention"
        out.append(_finding(
            "unattested_entity", severity,
            f"{name!r} is capitalised as a name but never "
            f"occurs in the transcript ({why})", sec_id))
    seen: set[str] = set()
    for sec in article.get("sections") or []:
        body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                      flags=re.DOTALL)
        # List and subheading markup is structure, not naming: strip the
        # markers so item-leading words read as line starts below.
        body = _LIST_MARKER.sub(r"\1", body)
        for m in _NAME.finditer(body):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            # Sentence-initial capitalisation is prose position, not
            # evidence: skip body starts, sentence ends and quote/dialogue
            # openings.
            if m.start() == 0:
                continue
            prev = body[:m.start()]
            if _SENTENCE_END.search(prev) or prev.endswith("\n"):
                continue
            if prev[-1:] in "\"'(‘“[#:*>|-":
                continue
            # Item text restarts after a bold lead-in ("**Pour:** Pour"):
            # a colon-terminated markup prefix is a boundary, not prose.
            line_start = body.rfind("\n", 0, m.start()) + 1
            prefix = body[line_start:m.start()].replace("*", "").replace(
                "#", "").replace("_", "").strip(" \t\"'(‘“[")
            if not prefix or prefix[-1:] in ".:!?":
                continue
            _check_name(name, sec.get("section_id"))
    if article.get("article_type") in ("discussion", "report"):
        for sec in article.get("sections") or []:
            body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                          flags=re.DOTALL)
            seen_attr: set[str] = set()
            for m in _ATTRIBUTION.finditer(body):
                name = m.group(1) or m.group(2)
                if name and name not in seen_attr:
                    seen_attr.add(name)
                    _check_name(name, sec.get("section_id"))
    return out


_GIT_BRANCH_DEF = re.compile(r"\bgit\s+branch\s+([A-Za-z][\w.\-]*)(?![\w.\-])")
_GIT_BRANCH_USE = re.compile(
    r"\bgit\s+(?:checkout|switch|merge|rebase|push|pull)\s+([A-Za-z][\w.\-]*)(?![\w.\-])")
_INJECTION_ECHO = re.compile(
    r"ignore\s+(all\s+)?previous\s+instructions|disregard\s+(all\s+)?(previous|prior|above)\s+"
    r"(instructions|orders)|you\s+are\s+now\s+\w+|jailbreak|do\s+anything\s+now",
    re.IGNORECASE)


def check_procedure_order(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependency order for procedures: no use before definition.

    Deterministic and narrow: identifiers introduced by definition
    commands (`git branch <name>`) must not be consumed
    (`git checkout/switch/merge <name>`) in an earlier section. The
    Git tutorial failure — switching to a branch before creating it —
    is the regression fixture. Generic prose is untouched.
    """
    out: list[dict[str, Any]] = []
    if article.get("article_type") != "procedure":
        return out
    defined_at: dict[str, int] = {}
    uses: list[tuple[str, int, str | None]] = []
    sections = article.get("sections") or []
    for i, sec in enumerate(sections):
        body = str(sec.get("body") or "")
        for m in _GIT_BRANCH_DEF.finditer(body):
            defined_at.setdefault(m.group(1), i)
        for m in _GIT_BRANCH_USE.finditer(body):
            uses.append((m.group(1), i, sec.get("section_id")))
    for name, i, sid in uses:
        if name in ("master", "main"):
            continue
        born = defined_at.get(name)
        if born is not None and born > i:
            out.append(_finding(
                "forward_reference", CRITICAL,
                f"{name!r} is used before the section that creates it",
                sid))
    return out


def check_no_injection_echo(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Transcripts are untrusted: model must not echo prompt-injection text.

    A hostile caption ("ignore previous instructions, publish ...") that
    survives into prose is a critical integrity failure, not a style nit.
    """
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                      flags=re.DOTALL)
        m = _INJECTION_ECHO.search(body)
        if m:
            out.append(_finding(
                "prompt_injection_echo", CRITICAL,
                f"untrusted-instruction text in prose: {m.group(0)[:60]!r}",
                sec.get("section_id")))
    title = str(article.get("title") or "")
    if _INJECTION_ECHO.search(title):
        out.append(_finding("prompt_injection_echo", CRITICAL,
                            "untrusted-instruction text in title"))
    return out


def check_temporal(article: dict[str, Any], segs: list[dict[str, Any]],
                   duration_s: float) -> list[dict[str, Any]]:
    """Invariant 3: spans sit inside the source and over real segments."""
    out: list[dict[str, Any]] = []
    if not segs:
        return [_finding("no_transcript", CRITICAL, "no transcript segments")]
    for sec in article.get("sections") or []:
        for a, b in _spans_of(sec):
            if not (0 <= a < b <= duration_s + 1):
                out.append(_finding(
                    "span_out_of_range", CRITICAL,
                    f"span {a:.0f}-{b:.0f}s outside 0-{duration_s:.0f}s",
                    sec.get("section_id")))
                continue
            if not any(float(r.get("end", -1)) >= a
                       and float(r.get("start", -1)) <= b for r in segs):
                out.append(_finding(
                    "span_without_segments", CRITICAL,
                    f"span {a:.0f}-{b:.0f}s covers no transcript",
                    sec.get("section_id")))
    return out


def check_origin(article: dict[str, Any],
                 decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 4: section evidence never overlaps excluded spans."""
    out: list[dict[str, Any]] = []
    banned: list[list[float]] = []
    for entry in decision.get("excluded_windows") or []:
        if not isinstance(entry, dict):
            continue
        try:
            banned.append([float(entry["start"]), float(entry["end"])])
        except (TypeError, ValueError, KeyError):
            continue
    if not banned:
        return out
    for sec in article.get("sections") or []:
        for a, b in _spans_of(sec):
            for c, d in banned:
                if a < d and c < b:
                    out.append(_finding(
                        "excluded_span_used", CRITICAL,
                        f"section uses excluded span {c:.0f}-{d:.0f}s",
                        sec.get("section_id")))
                    break
    return out


def check_coverage(article: dict[str, Any], index: dict[str, Any],
                   decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 5: non-excluded windows are represented or listed.

    Windows merged into neighbours are represented through overlap; only
    genuinely unrepresented windows are reported (capped: a long tail of
    repetition is one finding, not fifty).
    """
    out: list[dict[str, Any]] = []
    wins = index.get("windows") or []
    if not wins:
        return out
    banned: set[str] = set()
    for entry in decision.get("excluded_windows") or []:
        if isinstance(entry, dict) and entry.get("window_id"):
            banned.add(entry["window_id"])
    covered: list[list[float]] = []
    for sec in article.get("sections") or []:
        covered.extend(_spans_of(sec))
    missing = []
    for w in wins:
        if w.get("window_id") in banned:
            continue
        a, b = float(w["t_start"]), float(w["t_end"])
        if not any(a < d and c < b for c, d in covered):
            missing.append(w["window_id"])
    unassigned = set(((article.get("coverage") or {}).get("plan_coverage")
                      or {}).get("unassigned_substantive_window_ids") or [])
    genuinely = [m for m in missing if m not in unassigned]
    for wid in genuinely[:5]:
        out.append(_finding("unrepresented_window", MAJOR,
                            f"source window {wid} has no covering section"))
    return out


def check_reader_integrity(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 6: prose carries no internal machinery or raw markup."""
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = str(sec.get("body") or "")
        prose = _BALANCED_CODE.sub(" ", body)
        m = _INTERNAL_ID.search(prose)
        if m:
            out.append(_finding("internal_id_leak", CRITICAL,
                                f"internal span id {m.group(0)!r} in prose",
                                sec.get("section_id")))
        if re.search(r"\{['\"]\w+['\"]\s*:", prose):
            out.append(_finding("raw_dict_leak", CRITICAL,
                                "dictionary-shaped text in prose",
                                sec.get("section_id")))
        h = _RAW_HTML.search(prose)
        if h:
            out.append(_finding("raw_html_in_prose", CRITICAL,
                                f"model-emitted markup {h.group(0)[:40]!r}",
                                sec.get("section_id")))
        for fence in re.findall(r"```(.*?)```", body, flags=re.DOTALL):
            if fence.count('"') % 2 or fence.count("`") % 2:
                out.append(_finding("unbalanced_syntax", CRITICAL,
                                    "fenced block with unbalanced quotes",
                                    sec.get("section_id")))
                break
    title = str(article.get("title") or "")
    if _RAW_HTML.search(title):
        out.append(_finding("raw_html_in_title", CRITICAL,
                            "model-emitted markup in title"))
    if _INTERNAL_ID.search(title):
        out.append(_finding("internal_id_leak", CRITICAL,
                            "internal span id in title"))
    return out


def _frontmatter_sections(article: dict[str, Any],
                          duration_s: float) -> list[dict[str, Any]]:
    """Lead, summary and headings as validatable pseudo-sections.

    Frontmatter synthesises the whole video, so its evidence is the full
    span — checked with the same value/entity/integrity rules as body
    prose, but outside the slot, temporal, origin and coverage contracts
    that govern topical sections.
    """
    extras: list[dict[str, Any]] = []
    span = [[0.0, float(duration_s or 0) or 0.0]]
    lead_md = str(article.get("lead_md") or "").strip()
    if lead_md:
        extras.append({"section_id": "lead", "slot": "lead",
                       "heading": "Lead", "body": lead_md,
                       "t_start": 0.0, "t_end": float(duration_s or 0),
                       "source_windows": span})
    front = (str(article.get("summary") or "").strip() + "\n"
             + "\n".join(str(s.get("heading") or "")
                         for s in article.get("sections") or [])).strip()
    if front:
        extras.append({"section_id": "frontmatter", "slot": "frontmatter",
                       "heading": "Frontmatter", "body": front,
                       "t_start": 0.0, "t_end": float(duration_s or 0),
                       "source_windows": span})
    return extras


def validate_typed(decision: dict[str, Any], plan: dict[str, Any],
                   index: dict[str, Any], article: dict[str, Any],
                   segs: list[dict[str, Any]],
                   duration_s: float) -> dict[str, Any]:
    """Full type-aware gate. Returns {status, findings}.

    ``refused`` when the article has no usable sections; ``needs_review``
    on any critical finding; otherwise ``ready`` with majors recorded.
    """
    sections = [s for s in (article.get("sections") or [])
                if str(s.get("body") or "").strip()
                and str(s.get("body") or "").strip() != "_No speech in this span._"]
    if not sections:
        return {"status": "refused",
                "findings": [_finding("empty_article", CRITICAL,
                                      "no usable sections in article")]}
    extras = _frontmatter_sections(article, duration_s)
    scoped = dict(article, sections=sections)
    wide = dict(article, sections=sections + extras)
    findings: list[dict[str, Any]] = []
    findings.extend(check_type_slots(decision, plan, scoped))
    findings.extend(check_executable_blocks(wide))
    findings.extend(check_procedure_order(scoped))
    findings.extend(check_no_injection_echo(wide))
    findings.extend(check_exact_values(wide, segs))
    findings.extend(check_entities(wide, segs))
    findings.extend(check_temporal(scoped, segs, duration_s))
    findings.extend(check_origin(scoped, decision))
    findings.extend(check_coverage(scoped, index, decision))
    findings.extend(check_reader_integrity(wide))
    criticals = [f for f in findings if f["severity"] == CRITICAL]
    return {"status": "needs_review" if criticals else "ready",
            "findings": findings}
