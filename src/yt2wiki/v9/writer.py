"""V9 article writer: narration-first structured articles with stable sections.

Reads complete timestamped transcript segments, never the M7 evidence pile.
Writes sections in explicit transcript-coverage batches so no part of the
video is silently discarded.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..article import timestamp_url, timestamp_url_pattern
from ..backends.remote_openai import TruncatedResponseError

log = logging.getLogger("yt2wiki.v9.writer")

# Publication-correctness contract (editorial integrity, §1): a section gets
# at most two writer attempts — the initial call plus one bounded retry.
# Anything still unusable fails the writing stage and the job; a raw
# transcript excerpt must never be published as article prose.
WRITER_MAX_ATTEMPTS = 2

# A section body shorter than this has no usable substance (the no-speech
# path never reaches the model, so every model-written section must clear it).
MIN_BODY_CHARS = 40


class WriterError(RuntimeError):
    """A writing failure with structured fields for stage metrics and the job log."""


class SectionWriteError(WriterError):
    """A required section (or the title) could not be generated.

    Carries the stable plan identity and timestamp interval so the failure
    names the exact section, plus the sanitised failure category, the attempt
    count and a short diagnostic transcript excerpt (evidence only — never
    published prose).
    """

    def __init__(self, section_id: str, heading: str, t_start: float, t_end: float,
                 category: str, attempts: int, detail: str = "",
                 excerpt: str = ""):
        super().__init__(
            f"section {section_id} ({t_start:.0f}s-{t_end:.0f}s) failed "
            f"after {attempts} attempt(s): {category}"
            + (f": {detail[:200]}" if detail else ""))
        self.section_id = section_id
        self.heading = heading
        self.t_start = t_start
        self.t_end = t_end
        self.category = category
        self.attempts = attempts
        self.detail = detail
        self.excerpt = excerpt

    def metrics(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "category": self.category,
            "attempts": self.attempts,
            "detail": self.detail[:300],
            # Diagnostic evidence for the failure: the input that could not be
            # written. Stored in stage metrics, never published as prose.
            "excerpt": self.excerpt[:1000],
        }


class _SectionInvalid(Exception):
    """One writer response failed validation (retried or fatal at the call site)."""

    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category
        self.detail = detail

TITLE_PROMPT = """You write concise technical documentation about a tutorial video's SUBJECT, for a reader who has not watched it.

You are given chapter titles plus representative excerpts from the beginning, middle and end of the video. Do not summarise only the opening: represent the whole video, including later substantive chapters. Never describe the video, the creator or their community; describe what the subject achieves and how.

Return JSON only: {"title": "...", "summary": "2-3 sentences", "kind": "procedure|explainer|experimental"}.
Rules: title under 90 chars, no clickbait, summary states what the reader will be able to do or understand. kind is "procedure" only when the video teaches concrete actions or instructions; otherwise "explainer"."""

SECTION_PROMPT = """Write ONE documentation section about the technical subject below, for a reader who has not watched the video.

Your subject is the TOPIC of this span — not the video, the transcript, the speaker or the demonstration. Never narrate what the video, transcript, speaker or demonstration says, shows or covers. Write directly about the subject: what it is, how it is configured, what it does, what to expect.

Context:
- article title: {video_title}
- section heading: {heading} (you may return a better subject-specific heading of at most 8 words; never a sentence about the video)
- transcript span: {t_start:.1f}s - {t_end:.1f}s

Transcript (evidence — quote exact commands, values, paths and timestamps from it; never invent steps, commands, settings or results):
\"\"\"
{transcript}
\"\"\"

Return JSON only:
{{"heading": {heading!r},
  "body": "short paragraphs and bullets; keep exact commands, values, paths verbatim; {cite_rule}",
  "key_points": ["..."],
  "warnings": ["..."]}}
Rules:
- Use only what the narration states or clearly implies. Never invent commands, settings, steps or results.
- Explain supported concepts, configuration, operations, outcomes and limitations directly.
- Omit sponsor pitches, community promotions, audience appeals and introductory filler.
- Preserve prerequisites, corrections and failed attempts mentioned in the span.
- Prefer specific source-supported detail over generic advice. No marketing language."""


@dataclass
class V9Section:
    section_id: str
    heading: str
    body: str
    t_start: float
    t_end: float
    key_points: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "body": self.body,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "key_points": self.key_points,
            "warnings": self.warnings,
        }


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "section")[:40]


# Leading filler a transcript-fragment heading must skip: greetings, channel
# business, "in this video" throat-clearing and hype openers carry no subject.
_FILLER_OPENERS = re.compile(
    r"^(hey|hi|hello|welcome|thanks|thank you|okay so|so |now |alright|"
    r"in this video|today we|today i|let'?s (dive|jump|get)|make sure (you |to )?"
    r"(like|subscribe)|don'?t forget to (like|subscribe)|before we (start|begin)|"
    r"you guys|what'?s up|the (hottest|best|ultimate|coolest) (new )?"
    r"(device|product|thing|gadget|video))\b",
    re.IGNORECASE)


def _heading_from_transcript(transcript: Sequence[dict[str, Any]],
                               t_start: float, t_end: float) -> str:
    """Derive a subject heading from the transcript slice when no chapter exists.

    Skips leading filler sentences (greetings, channel business, "in this
    video" openers) and takes the first substantive sentence, trimmed to 5–7
    content words. Falls back to 'Introduction' / 'Overview' style labels only
    when the slice has no speech, preserving chronological structure without
    inventing operations.
    """
    words = _first_substantive_words(transcript, t_start, t_end)
    if not words:
        # No speech in window — generic but not "Part N"
        return "Overview" if t_start < 30 else "Additional details"
    return _content_heading(words)


_HEADING_STOP = {"the", "a", "an", "and", "or", "but", "so", "then", "we", "you",
                 "i", "it", "this", "that", "is", "are", "was", "were", "to",
                 "for", "of", "in", "on", "with", "as", "at"}


def _first_substantive_words(transcript: Sequence[dict[str, Any]],
                             t_start: float, t_end: float) -> list[str]:
    """Words of the first substantive non-filler sentence in the window."""
    for r in transcript:
        if float(r["end"]) < t_start or float(r["start"]) > t_end:
            continue
        text = (r.get("text") or "").strip()
        if not text:
            continue
        for sent in re.split(r"[.!?]+", text):
            sent = sent.strip()
            if len(sent.split()) >= 4 and not _FILLER_OPENERS.search(sent):
                return sent.split()
    return []


def _content_heading(words: list[str]) -> str:
    """Strip leading stop words, take 6 content words, safe title case."""
    while words and words[0].lower() in _HEADING_STOP:
        words.pop(0)
    heading = " ".join(words[:6]).strip(" ,;:").strip()
    if not heading:
        return "Overview"
    return heading[0].upper() + heading[1:60]


def cite_rule(url: str | None) -> str:
    """Timestamp citation rule for the section prompt.

    The old template said "[m:ss](url)" without ever substituting it, so the
    model hallucinated video URLs for YouTube sources and wrote bare "m:ss"
    for uploads (which have no origin URL). Link form shares
    :func:`timestamp_url_pattern` with the pipeline, so the cited pattern can
    never drift from the links `normalize_timestamps` emits — including
    replacing a pre-existing `t` param instead of duplicating it.
    """
    if url and "youtu" in url:
        example = timestamp_url(url, 159)
        assert example is not None
        return (f"cite timestamps ONLY as [m:ss]({timestamp_url_pattern(url)}), e.g. "
                f"[2:39]({example}). Never write bare seconds like [159] or "
                "[159:0], never invent any other URL")
    return ("cite timestamps ONLY as plain [m:ss] with no link, e.g. "
            "[0:04] (this upload has no source URL). Never write seconds "
            "like [159] or [159:0], never invent a URL")


def _classify_error(exc: BaseException) -> tuple[str, bool]:
    """Map a writer-call failure to (sanitised category, retryable).

    Categories distinguish what the evidence permits: HTTP timeout,
    transport/server errors, invalid JSON, server-side truncation and invalid
    content. Unknown failures are labelled "error" and get one retry; a
    persistent failure then fails the section loudly.
    """
    if isinstance(exc, TruncatedResponseError):
        return "truncated", True
    if isinstance(exc, _SectionInvalid):
        # Editorial failures are corrected with a reinforced prompt; anything
        # else structural gets one plain retry.
        return exc.category, True
    mod = type(exc).__module__ or ""
    name = type(exc).__name__
    if mod.startswith("httpx"):
        if "Timeout" in name:
            return "timeout", True
        if name == "HTTPStatusError":
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            if status == 429 or status >= 500:
                return "server", True
            return "transport", False
        return "transport", True
    if isinstance(exc, ValueError):
        # JSON extraction (extract_json) failures land here.
        return "invalid_json", True
    return "error", True


def validate_section_response(out: Any, *, section_id: str, heading_fallback: str,
                               excerpt: str = "") -> dict[str, Any]:
    """Validate one writer response; return the normalised section fields.

    Raises _SectionInvalid("invalid_content", ...) when the response is not a
    usable section: not an object, empty body, leaked fallback marker, a raw
    transcript dump, or structurally broken Markdown. Repaired or truncated
    JSON is accepted only when every required field and content check passes.
    """
    if not isinstance(out, dict):
        raise _SectionInvalid("invalid_content",
                              f"response is {type(out).__name__}, not a JSON object")
    body = str(out.get("body") or "").strip()
    if len(body) < MIN_BODY_CHARS:
        raise _SectionInvalid("invalid_content",
                              f"empty body ({len(body)} chars)")
    if "_Transcript excerpt" in body or "(model unavailable)" in body:
        raise _SectionInvalid("invalid_content",
                              "raw-transcript fallback marker leaked into body")
    if body.count("```") % 2:
        raise _SectionInvalid("invalid_content", "unclosed fenced code block")
    if excerpt and _is_verbatim_dump(body, excerpt):
        raise _SectionInvalid("invalid_content",
                              "body is a raw transcript dump, not a section")
    heading = str(out.get("heading") or "").strip()[:120] or heading_fallback

    def _str_list(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(x).strip() for x in value if str(x).strip()][:limit]

    return {"heading": heading, "body": body,
            "key_points": _str_list(out.get("key_points"), 8),
            "warnings": _str_list(out.get("warnings"), 6)}


def validate_title_response(out: Any, **ignored: Any) -> dict[str, Any]:
    """Validate the title-writer response; title itself is non-negotiable."""
    if not isinstance(out, dict):
        raise _SectionInvalid("invalid_content",
                              f"response is {type(out).__name__}, not a JSON object")
    if not str(out.get("title") or "").strip():
        raise _SectionInvalid("invalid_content", "empty title")
    return {"title": str(out["title"]).strip(),
            "summary": str(out.get("summary") or "").strip(),
            "kind": str(out.get("kind") or "explainer").strip().lower()}


# section_transcript prefixes every line with its span ([633.9-639.3] ...).
# A body that copies the narration without those prefixes is still a raw
# dump, so both sides are compared with the prefixes removed.
_TS_PREFIX = re.compile(r"(?m)^\[\d+(?:\.\d+)?-\d+(?:\.\d+)?\]\s*")


def _normalise_text(text: str, lower: bool = True) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower() if lower else text


def _is_verbatim_dump(body: str, excerpt: str) -> bool:
    """True when the body is the input transcript, not a written section."""
    # Strip span prefixes first: normalisation collapses newlines, so the
    # line-anchored pattern would no longer match afterwards.
    nb = _normalise_text(_TS_PREFIX.sub("", body or ""))
    ne = _normalise_text(_TS_PREFIX.sub("", excerpt or ""))
    if not nb or not ne:
        return False
    # An exact normalised copy is a dump at any length: no written section
    # is byte-identical to its span transcript.
    if nb == ne:
        return True
    # Scale floor for approximate similarity only: below ~500 excerpt
    # chars a near-verbatim body is legitimate concise writing (a short
    # span restated in other words), not a section-scale transcript dump.
    if len(ne) < 500:
        return False
    if nb in ne or ne in nb:
        return True
    if abs(len(nb) - len(ne)) > max(len(nb), len(ne)) * 0.2:
        return False
    import difflib
    return difflib.SequenceMatcher(None, nb, ne, autojunk=True).ratio() > 0.9


# Deterministic editorial check (§2): obvious metanarration and placeholder
# leakage that must never ship in a standalone technical article. Code spans
# are stripped before matching, and each pattern names the violated rule so a
# bounded correction attempt can address it. Deliberately narrow: words like
# "video" or "transcript" alone are legitimate technical subjects.
_EDITORIAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bthe transcript (also )?(gives|shows|mentions|says|states|explains|"
                r"describes|demonstrates|provides|covers|walks|notes|highlights|"
                r"points out|examines|explores|details|looks at)\b", re.IGNORECASE),
     "narrates the transcript instead of writing about the subject"),
    (re.compile(r"\bthe video (covers|shows|demonstrates|explains|walks|is about|starts|"
                r"begins|ends|also mentions|examines|explores|details|looks at|"
                r"takes? a look at)\b", re.IGNORECASE),
     "narrates the video instead of writing about the subject"),
    (re.compile(r"\b(this video|the demonstration|the speaker|the creator|the narrator|"
                r"the demonstrator) (covers|shows|demonstrates|explains|gives|walks|"
                r"says|states|is about|examines|explores|details|looks at|"
                r"takes? a look at)\b", re.IGNORECASE),
     "narrates the speaker/demonstration instead of writing about the subject"),
    (re.compile(r"\bin this video\b", re.IGNORECASE),
     "locates content in the video rather than in the subject"),
    (re.compile(r"\b(like and subscribe|subscribe to (the|this|my|our) channel|"
                r"smash (that|the) (like|subscribe)|ring the bell)\b", re.IGNORECASE),
     "audience appeal that does not belong in a technical article"),
    (re.compile(r"\b(free school community|join (our|the|my) community|"
                r"today'?s sponsor|sponsor(ed)? by|link in the description)\b",
                re.IGNORECASE),
     "promotion or sponsor pitch that does not belong in a technical article"),
    (re.compile(r"\b(TODO|FIXME|XXX|Lorem ipsum|\[insert\b|\(insert\b|as an AI\b|"
                r"as a language model\b)", re.IGNORECASE),
     "placeholder or test leakage"),
]


def editorial_violations(body: str) -> list[str]:
    """Distinct violated editorial rules in a section body (empty when clean)."""
    prose = _normalise_text(body, lower=False)
    found: list[str] = []
    for pattern, rule in _EDITORIAL_PATTERNS:
        if pattern.search(prose) and rule not in found:
            found.append(rule)
    return found


def _correction_instruction(violations: list[str]) -> str:
    items = "\n".join(f"- {v}" for v in violations)
    return (
        "Rewrite the section body to fix these editorial problems, keeping "
        "every technical fact, command, value, path and timestamp exactly intact:\n"
        f"{items}\n"
        "Write about the technical SUBJECT for a reader who has not watched "
        "the video. Do not narrate what the video, transcript, speaker or "
        "demonstration says, shows or covers. Return JSON only with the same schema.")


# A title that promises construction the transcript never shows (the "build
# an agent from scratch" promotional intro) must not ship as a how-to title.
_CONSTRUCTION_PROMISE = re.compile(
    r"\b(build(?:ing)?|creat(?:e|ing)|mak(?:e|ing)|set ?up|setting ?up|"
    r"from scratch|step.by.step|"
    r"how to (build|create|make|set ?up|deploy|install))\b", re.IGNORECASE)


def _title_promises_construction(title: str) -> bool:
    return bool(_CONSTRUCTION_PROMISE.search(title or ""))


# Character budgets for the bounded title context: chapters get their own
# share so sampled excerpts always fit, and the 3000-char total is a
# backstop, never the mechanism that silently drops the video's tail.
_TITLE_BUDGET = 3000
_TITLE_CHAPTER_BUDGET = 1200
_TITLE_SAMPLES = 16


def _thin_middle(items: list, limit: int) -> list:
    """Drop middle items evenly until len fits, always keeping both ends."""
    items = list(items)
    while len(items) > limit and len(items) > 2:
        del items[len(items) // 2]
    return items[:limit]



def _chapter_context_block(chapters: Sequence[dict[str, Any]] | None) -> str:
    """Formatted chapter list under its own character budget.

    Thin middle chapters evenly so the sampled excerpts below still fit and
    the tail is never cut.
    """
    if not chapters:
        return ""
    lines = []
    for ch in chapters[:20]:
        try:
            lo, hi = float(ch.get("start_time", 0)), float(ch.get("end_time", 0))
            title = str(ch.get("title") or "").strip()
        except (TypeError, ValueError):
            continue
        if title:
            lines.append(f"- [{lo:.0f}s-{hi:.0f}s] {title[:80]}")
    if not lines:
        return ""
    while len("Chapters:\n" + "\n".join(lines)) > _TITLE_CHAPTER_BUDGET \
            and len(lines) > 2:
        lines = _thin_middle(lines, len(lines) - 1)
    return ("Chapters:\n" + "\n".join(lines))[:_TITLE_CHAPTER_BUDGET]


def _sample_indices(segs: Sequence[dict[str, Any]],
                    chapters: Sequence[dict[str, Any]] | None) -> list[int]:
    """One sample per chapter midpoint (so later substantive chapters are
    always represented), plus the very first and last segments, then an even
    subsample — never an earliest-prefix truncation that drops the final
    chapters/segments first."""
    n = len(segs)
    idxs = {0, 1, n - 2, n - 1}
    starts = []
    for r in segs:
        try:
            starts.append(float(r["start"]))
        except (KeyError, TypeError, ValueError):
            starts.append(float("inf"))
    for ch in chapters or []:
        try:
            mid = (float(ch.get("start_time", 0)) + float(ch.get("end_time", 0))) / 2
        except (TypeError, ValueError):
            continue
        best = min(range(n), key=lambda i: abs(starts[i] - mid)
                   if starts[i] != float("inf") else float("inf"))
        idxs.add(best)
    ordered = sorted(i for i in idxs if 0 <= i < n)
    if len(ordered) > _TITLE_SAMPLES:
        inner = _thin_middle(ordered[1:-1], _TITLE_SAMPLES - 2)
        return sorted(set([ordered[0]] + inner + [ordered[-1]]))
    return ordered


def _title_context(segs: Sequence[dict[str, Any]],
                   chapters: Sequence[dict[str, Any]] | None,
                   duration_s: float | None) -> str:
    """Bounded whole-video context for the title writer (no extra LLM pass).

    Meaningful chapter labels plus sampled excerpts from the beginning, middle
    and end, so later substantive chapters are represented — never just the
    promotional opening.
    """
    parts: list[str] = []
    if duration_s:
        parts.append(f"Duration: {duration_s:.0f}s.")
    chapters_block = _chapter_context_block(chapters)
    if chapters_block:
        parts.append(chapters_block)
    picks = _sample_indices(segs, chapters)
    rendered = []
    for i in picks:
        r = segs[i]
        try:
            rendered.append(f"[{float(r['start']):.0f}s] "
                            f"{(r.get('text') or '').strip()[:160]}")
        except (KeyError, TypeError, ValueError):
            continue
    # Samples share the remaining budget after duration + chapters; thin
    # middles first so the final segment excerpt always survives. Joining
    # costs one newline per rendered line.
    head = "\n".join(parts)
    room = _TITLE_BUDGET - len(head)
    while len(rendered) > 2 and \
            sum(len(x) for x in rendered) + len(rendered) > room:
        rendered = _thin_middle(rendered, len(rendered) - 1)
    parts.extend(_thin_middle(rendered, _TITLE_SAMPLES))
    out = "\n".join(parts)
    return out[:_TITLE_BUDGET]


_ACTION_VERBS = frozenset([
    "run", "install", "configure", "set", "create", "open", "click", "type",
    "paste", "copy", "download", "upload", "enable", "disable", "connect",
    "select", "choose", "enter", "add", "remove", "delete", "edit", "save",
    "build", "deploy", "start", "stop", "restart", "execute", "use", "go",
    "navigate", "launch", "create", "update", "change", "switch", "toggle",
    "check", "verify", "test", "send", "receive", "write", "read",
    "import", "export"])
_IMPERATIVE = None


def _imperative_re():
    global _IMPERATIVE
    if _IMPERATIVE is None:
        verb = "(?:{})".format("|".join(sorted(_ACTION_VERBS)))
        _IMPERATIVE = re.compile(
            rf"^(?:\d+[.)]\s*|[-*\u2022]\s*)?"
            rf"(?:(?:then|next|now|also|just|simply|please)\s+)?{verb}\b",
            re.IGNORECASE)
    return _IMPERATIVE


def _action_hits(sections: Sequence[dict[str, Any]]) -> int:
    """Count source-supported action evidence in section bodies.

    Imperative instruction sentences, fenced command blocks and explicit step
    markers — the justification a `procedure` classification must stand on.
    Sentences (not lines) are the unit, so a compact two-sentence 30-second
    instruction still counts as actionable.
    """
    hits = 0
    imperative = _imperative_re()
    stepmark = re.compile(r"\bstep\s+\d+\b", re.IGNORECASE)
    for sec in sections:
        body = str(sec.get("body") or "")
        prose = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
        for sent in re.split(r"[.!?]+\s*", prose):
            if imperative.match(sent.strip()):
                hits += 1
        hits += len(stepmark.findall(prose))
        hits += body.count("```") // 2  # each fenced block shows an action
    return hits


def justify_kind(kind: str, sections: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """Downgrade an unjustified `procedure` to `explainer`.

    A procedure classification must be earned by actual source-supported
    actions in the resulting content — a promotional introduction with no
    instructions is an explainer, however useful-sounding. Returns the
    (kind, note) pair; note is empty when the kind stands.
    """
    if kind != "procedure":
        return kind, ""
    if _action_hits(sections) >= 2:
        return kind, ""
    return ("explainer",
            "downgraded from procedure: no source-supported actions in content")



def _section_bounds(chapters: Sequence[dict[str, Any]] | None,
                    total: float) -> list[tuple[float, float, str | None]]:
    """Chapter-derived bounds, or fixed 150 s windows without chapters."""
    bounds: list[tuple[float, float, str | None]] = []
    if chapters:
        for ch in chapters:
            try:
                bounds.append((float(ch.get("start_time", 0)),
                               float(ch.get("end_time", total)),
                               str(ch.get("title") or "")))
            except (TypeError, ValueError):
                continue
        bounds.sort()
    if not bounds:
        step = 150.0
        t = 0.0
        while t < total:
            bounds.append((t, min(t + step, total), ""))
            t += step
    return bounds


def _merge_short_spans(bounds: list[tuple[float, float, str | None]]) -> list[list]:
    """Merge spans shorter than 25 s into the previous one (keep first heading)."""
    merged: list[list] = []
    for lo, hi, title in bounds:
        if merged and (hi - lo) < 25.0:
            merged[-1][1] = hi
            if title and not merged[-1][2]:
                merged[-1][2] = title
        else:
            merged.append([lo, hi, title])
    return merged


def plan_sections(transcript: Sequence[dict[str, Any]],
                  chapters: Sequence[dict[str, Any]] | None,
                  duration_s: float | None) -> list[dict[str, Any]]:
    """Stable section plan with explicit transcript coverage (no silent drops)."""
    if not transcript:
        raise ValueError("empty transcript")
    total = duration_s or float(transcript[-1]["end"])
    bounds = _section_bounds(chapters, total)
    merged = _merge_short_spans(bounds)
    tend = max([hi for _, hi, _ in merged] + [float(transcript[-1]["end"])])
    if merged:
        merged[-1][1] = tend
    plan = []
    for i, (lo, hi, title) in enumerate(merged):
        heading = title.strip() if title and title.strip() else _heading_from_transcript(transcript, lo, hi)
        plan.append({
            "section_id": f"sec-{i + 1:02d}-{slug(heading)}",
            "heading": heading,
            "t_start": round(lo, 1),
            "t_end": round(hi, 1),
        })
    # Coverage assertion: plan must span the whole transcript.
    assert plan[0]["t_start"] <= float(transcript[0]["start"]) + 1.0
    assert plan[-1]["t_end"] >= float(transcript[-1]["end"]) - 1.0
    return plan


def section_transcript(transcript: Sequence[dict[str, Any]],
                       t_start: float, t_end: float, *, cap_chars: int = 9000) -> str:
    lines = []
    for r in transcript:
        if float(r["end"]) < t_start or float(r["start"]) > t_end:
            continue
        lines.append(f"[{float(r['start']):.1f}-{float(r['end']):.1f}] {r['text'].strip()}")
        if sum(len(x) for x in lines) > cap_chars:
            break
    return "\n".join(lines)


def _run_attempt(chat, call, *, validate, editorial: bool, section_id: str,
                 heading: str, t_start: float, t_end: float,
                 excerpt: str = "") -> dict[str, Any]:
    """One section through at most WRITER_MAX_ATTEMPTS writer calls.

    Transport failures, invalid JSON, truncation and correctable editorial
    problems get a single bounded retry; the second failure (or a
    non-retryable one) raises SectionWriteError with the exact section,
    interval, attempt count and sanitised category. The original plan
    input, timestamp interval and stable section ID are preserved on the
    error for stage metrics — never substituted with fallback prose.
    """
    last: tuple[str, str] | None = None
    calls = 0
    correction = ""
    while calls < WRITER_MAX_ATTEMPTS:
        calls += 1
        try:
            out = call(correction) if calls > 1 else call("")
        except Exception as exc:  # noqa: BLE001 - classified below
            category, retryable = _classify_error(exc)
            last = (category, f"{type(exc).__name__}: {exc}"[:300])
            log.warning("section %s attempt %d failed (%s): %s",
                        section_id, calls, category, exc)
            if retryable and calls < WRITER_MAX_ATTEMPTS:
                continue
            break
        try:
            checked = validate(out, section_id=section_id, heading_fallback=heading,
                               excerpt=excerpt)
        except _SectionInvalid as bad:
            last = (bad.category, bad.detail)
            log.warning("section %s attempt %d invalid (%s): %s",
                        section_id, calls, bad.category, bad.detail)
            if calls < WRITER_MAX_ATTEMPTS:
                continue
            break
        problems = editorial_violations(checked["body"]) if editorial else []
        if problems and calls < WRITER_MAX_ATTEMPTS:
            # Bounded correction: one reinforced rewrite, facts intact.
            correction = _correction_instruction(problems)
            last = ("editorial", "; ".join(problems)[:300])
            log.info("section %s attempt %d needs editorial correction: %s",
                     section_id, calls, problems)
            continue
        if problems:
            last = ("editorial", "; ".join(problems)[:300])
            break
        checked["attempts"] = calls
        return checked
    category, detail = last or ("error", "unknown failure")
    raise SectionWriteError(section_id, heading, t_start, t_end,
                            category, calls, detail, excerpt)


def _write_title(chat, segs, chapters, srow, video_title) -> tuple[dict[str, Any], str]:
    """Title/summary pass with neutralising fallbacks (never fail the job
    over summary narration). Returns (thead, title_fallback_note)."""
    def _title_call(extra: str) -> dict[str, Any]:
        context = _title_context(segs, chapters, srow["duration_s"])
        user = (TITLE_PROMPT + f"\n\nVideo title: {video_title}\n{context}")
        if extra:
            user += ("\n\nCorrection: the previous reply was unusable. "
                     "Return ONLY a JSON object with title, summary and kind.")
        return chat(
            [{"role": "system", "content": "Return JSON only."},
             {"role": "user", "content": user}],
            max_new_tokens=400, temperature=0.1)

    # Action evidence in the raw transcript: a title that promises
    # construction while the transcript shows no actions is a promotional
    # artefact, not a how-to title (§3).
    transcript_hits = _action_hits([{"body": str(r.get("text") or "")}
                                    for r in segs])

    # The best title seen so far: a persistently narrating summary must not
    # take a valid title down with it — the summary blanks, the title stands.
    saved_title: dict[str, Any] = {"title": "", "kind": "explainer"}

    def _title_validate(out: Any, **ignored: Any) -> dict[str, Any]:
        checked = validate_title_response(out)
        if transcript_hits == 0 and _title_promises_construction(checked["title"]):
            raise _SectionInvalid(
                "invalid_content",
                "title promises construction the transcript never shows")
        problems = editorial_violations(checked["summary"] or "")
        if problems:
            if str(checked["title"]).strip():
                saved_title["title"] = str(checked["title"]).strip()[:120]
                saved_title["kind"] = checked["kind"]
            raise _SectionInvalid("editorial", "; ".join(problems))
        return checked

    title_fallback = ""
    try:
        return (_run_attempt(chat, _title_call, validate=_title_validate,
                             editorial=False, section_id="title",
                             heading="title", t_start=0.0, t_end=0.0), "")
    except SectionWriteError as exc:
        if exc.section_id == "title" and exc.category == "editorial" and saved_title["title"]:
            # Valid title, unfixable summary narration: keep the title, drop
            # the summary rather than fail the job over two sentences.
            log.warning("summary neutralised after editorial failure: %s", exc)
            thead = {"title": saved_title["title"], "summary": "",
                     "kind": saved_title["kind"]}
            return thead, "summary blanked after editorial failure"
        if transcript_hits == 0 and exc.section_id == "title":
            # Nothing actionable to title: fall back to the source's own title
            # rather than publish an invented construction promise.
            log.warning("title neutralised to source title: %s", exc)
            note = "neutralised to source title: transcript shows no actions"
            return {"title": video_title, "summary": "", "kind": "explainer"}, note
        raise


def _write_sections(chat, plan, segs, video_title,
                    rule) -> tuple[list[V9Section], list[tuple[float, float]]]:
    """Every planned section, in order; empty spans get a stub body."""
    sections: list[V9Section] = []
    represented: list[tuple[float, float]] = []
    for p in plan:
        text = section_transcript(segs, p["t_start"], p["t_end"])
        if not text.strip():
            out = {"heading": p["heading"], "body": "_No speech in this span._",
                   "key_points": [], "warnings": [], "attempts": 0}
        else:
            def _section_call(extra: str, _p=p, _text=text) -> dict[str, Any]:
                user = SECTION_PROMPT.format(
                    video_title=video_title, heading=_p["heading"],
                    t_start=_p["t_start"], t_end=_p["t_end"], transcript=_text,
                    cite_rule=rule)
                if extra:
                    user += "\n\n" + extra
                return chat(
                    [{"role": "system", "content": "Return JSON only."},
                     {"role": "user", "content": user}],
                    max_new_tokens=1600, temperature=0.15)

            out = _run_attempt(chat, _section_call, validate=validate_section_response,
                               editorial=True, section_id=p["section_id"],
                               heading=p["heading"], t_start=p["t_start"],
                               t_end=p["t_end"], excerpt=text)
        # A writer-suggested heading must itself be subject-specific; a
        # narrating heading falls back to the stable plan heading.
        heading = str(out.get("heading") or p["heading"])
        if heading != p["heading"] and editorial_violations(heading):
            heading = p["heading"]
        sections.append(V9Section(
            section_id=p["section_id"], heading=heading,
            body=str(out.get("body", "")).strip(), t_start=p["t_start"], t_end=p["t_end"],
            key_points=[str(x) for x in (out.get("key_points") or [])][:8],
            warnings=[str(x) for x in (out.get("warnings") or [])][:6]))
        represented.append((p["t_start"], p["t_end"]))
    return sections, represented



def write_article(settings, db, source_id: str, *, backend=None,
                  inference_snapshot: dict[str, Any] | None = None,
                  recorder=None) -> dict[str, Any]:
    """Generate the V9 ASR article. Returns {article fields, coverage, timings}."""
    from ..backends.loader import ModelManager
    from ..config import Settings as _S  # noqa: F401 (type only)

    started = time.time()
    srow = db.query_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if srow is None:
        raise KeyError(f"unknown source {source_id}")
    segs = [dict(r) for r in db.query(
        "SELECT idx, start, end, text FROM transcript_segments WHERE source_id=? ORDER BY idx",
        (source_id,))]
    if not segs:
        raise RuntimeError("no transcript available; run the transcribe stage first")
    meta = json.loads(srow["meta_json"] or "{}")
    chapters = (meta.get("provenance") or {}).get("chapters") or (meta.get("chapters") or [])
    plan = plan_sections(segs, chapters, srow["duration_s"])
    admitted = [(p["t_start"], p["t_end"]) for p in plan]

    video_title = srow["title"] or source_id
    url = srow["origin_url"]
    rule = cite_rule(url)
    if recorder is None:
        def _default_rec(entry: dict) -> None:
            entry.setdefault("source_id", source_id)
            db.record_model_load(entry)

        recorder = _default_rec
    manager = ModelManager(settings, recorder=recorder,
                           inference_snapshot=inference_snapshot)

    def _chat(payload_msgs, **kw):
        if backend is not None:
            raw = backend.chat(payload_msgs, **kw)
            from ..backends.llm import extract_json
            return extract_json(raw)
        with manager.acquire("text") as b:
            return b.chat_json(payload_msgs, **kw)

    thead, title_fallback = _write_title(
        _chat, segs, chapters, srow, video_title)
    title = str(thead.get("title") or video_title)[:120]
    summary = str(thead.get("summary") or "")
    kind = str(thead.get("kind") or "explainer").strip().lower()
    if kind not in ("procedure", "explainer", "experimental"):
        kind = "explainer"

    sections, represented = _write_sections(
        _chat, plan, segs, video_title, rule)

    kind, kind_note = justify_kind(kind, [s.to_dict() for s in sections])
    coverage: dict[str, Any] = {
        "admitted_windows": admitted, "represented_windows": represented,
        "n_sections": len(sections),
        "transcript_chars": sum(len(r["text"]) for r in segs)}
    if kind_note:
        coverage["kind_adjusted"] = kind_note
    if title_fallback:
        coverage["title_adjusted"] = title_fallback
    return {
        "title": title, "summary": summary, "kind": kind,
        "sections": [s.to_dict() for s in sections],
        "coverage": coverage,
        "elapsed_s": round(time.time() - started, 3),
    }

