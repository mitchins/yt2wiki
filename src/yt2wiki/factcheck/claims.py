"""Atomic claim extraction from immutable article markdown (offline).

Deterministic, rule-based, no models: splits published sections into
candidate sentences, classifies each into the mission's claim kinds, scores
materiality, and caps the selected set — recording both the selected and
the not-checked material claims so N checked claims are never presented
as complete verification of the whole article.

Only `## ` section bodies are mined. Attribution quotes, lead metadata,
figure markup, captions and per-section source-reference lines are
structural, not claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import NON_CHECKABLE_KINDS

# Sentences shorter than this are fragments/headings, not atomic claims.
MIN_CLAIM_CHARS = 25

_ATTRIBUTION_VERBS = (
    "said", "says", "argues", "claimed", "claims", "thinks", "feels",
    "believes", "believe", "loves", "hates", "prefers", "admits",
    "calls", "call", "describes", "describe",
    "according to",
)

_FIRST_PERSON = (" i ", " we ", " my ", " our ", " me ", " us ")

_CAUSAL_MARKERS = (
    "because", "causes", "caused by", "leads to", "lead to", "results in",
    "due to", "makes it", "triggers", "prevents",
)

# Attributed effectiveness assertions ("X cures Y", "X prevents Z") carry a
# separately checkable embedded factual premise even when merely reported.
_EFFECT_MARKERS = (
    "cure", "cures", "treat", "treats", "prevent", "prevents", "heal",
    "heals", "kills", "efficacy", "effective",
)

_ADVICE_MARKERS = (
    "should", "must", "never ", "always ", "keep ", "use ",
    "make sure", "avoid ", "don't ", "do not ",
)

# Evaluative / subjective language: the sentence expresses a judgment,
# not a checkable assertion — even when it carries no number.
_SUBJECTIVE_MARKERS = (
    "beautiful", "ugly", "finest", "best", "worst", "greatest",
    "terrible", "awful", "amazing", "inspiring", "incredible",
    "love", "loves", "hate", "hates", "prefer", "prefers",
    "think ", "thinks ", "feel ", "feels ", "believe ", "opinion ",
)

# Meta-discourse: kept empirical (honest default) but deprioritised so the
# claim cap spends retrieval on material assertions first.
_DISCOURSE_MARKERS = (
    "in this section", "this section", "let's ", "as mentioned",
    "see below", "shown below", "as shown",
)

_FUTURE_MARKERS = ("will ", "going to", "predict", "forecast", "by 203")

_YEAR_RE = re.compile(r"\b(19|20)\d{2}s?\b")
_TIME_UNIT_RE = re.compile(
    r"\b(seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b", re.I)


def _has_measure(sentence: str) -> bool:
    """A quantity beyond bare calendar references.

    "Next year" and "in 2022" are temporal settings, not measurements: a
    future-tense sentence whose only numbers are calendar references is a
    prediction, while "5 percent" or "380 meters" stays empirical.
    """
    scrubbed = _YEAR_RE.sub(" ", sentence)
    scrubbed = _TIME_UNIT_RE.sub(" ", scrubbed)
    return bool(_NUMBER_RE.search(scrubbed) or _UNIT_RE.search(scrubbed))

_HIGH_STAKES = (
    "safe", "safety", "danger", "risk", "fee", "fees", "percent", "%",
    "dose", "toxic", "warranty", "guarantee", "cure", "disease",
)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?|\b\d{4}s?\b")
_UNIT_RE = re.compile(
    r"\b(mm|cm|m|km|mg|g|kg|ml|l|hz|khz|v|volts|w|watts|°c|°f|degrees?|"
    r"minutes?|hours?|days?|weeks?|months?|years?|dollars?|\$)\b",
    re.I,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])")
_QUOTED_RE = re.compile(r'"([^"]{10,})"')


@dataclass
class CandidateClaim:
    section_id: str
    quote: str
    offset: int
    claim_kind: str
    priority: int
    checkable: bool
    attribution: str = "article_narration"
    video_provenance: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.video_provenance is None:
            self.video_provenance = []


def _section_id(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
    return slug or "section"


def _classify(sentence: str) -> tuple[str, bool]:
    """Return (claim_kind, checkable). Never editorialises the wording.

    Corrective rule: a declarative sentence is empirical by default. Only
    explicit subjectivity (evaluative language, cognition/emotion verbs),
    quantity-free prediction, or non-factual kinds mark a sentence
    non-checkable. Attribution never demotes a factual assertion: an
    attributed empirical premise is checkable with attribution kept.
    """
    low = f" {sentence.lower()} "
    quoted = bool(_QUOTED_RE.search(sentence))
    attributed = quoted or any(v in low for v in _ATTRIBUTION_VERBS)
    first_person = any(p in low for p in _FIRST_PERSON)

    is_causal = any(m in low for m in _CAUSAL_MARKERS)
    is_advice = sentence.strip().rstrip(".").split()
    imperative = bool(is_advice) and is_advice[0][0].isupper() and is_advice[0].lower() not in (
        "the", "this", "that", "these", "those", "it", "they", "he", "she",
        "a", "an", "i", "we")
    is_advice_flag = imperative or any(m in low for m in _ADVICE_MARKERS)
    is_future = any(m in low for m in _FUTURE_MARKERS)
    is_effect = any(m in low for m in _EFFECT_MARKERS)
    is_subjective = any(m in low for m in _SUBJECTIVE_MARKERS)

    if is_future and not _has_measure(sentence):
        return "prediction", False
    if is_advice_flag:
        # The factual premise of advice (an asserted fee, effectiveness,
        # or safety property) is checkable; the advice itself is not.
        return "actionable_factual_premise", True
    if is_causal or is_effect:
        # Reported effectiveness claims carry a checkable embedded premise.
        return "causal", True
    if is_subjective:
        if first_person:
            return "personal_preference", False
        if attributed:
            return "attributed_opinion", False
        return "normative_judgment", False
    # Default: declarative factual assertion (attributed or not).
    return "empirical", True


def _priority(sentence: str, kind: str) -> int:
    low = f" {sentence.lower()} "
    score = 0
    if _NUMBER_RE.search(sentence):
        score += 2  # quantitatively precise
    if kind == "causal":
        score += 1
    if kind == "actionable_factual_premise":
        score += 1  # consequential to conclusions
    if any(w in low for w in _HIGH_STAKES):
        score += 1  # high-stakes
    if any(m in low for m in _DISCOURSE_MARKERS):
        score -= 5  # meta-discourse sinks below material assertions
    return score


def _attribution(sentence: str) -> str:
    """Who the article attributes the sentence to (kept, never resolved)."""
    low = f" {sentence.lower()} "
    if _QUOTED_RE.search(sentence):
        return "quoted_person"
    if any(v in low for v in _ATTRIBUTION_VERBS):
        return "presenter"
    return "article_narration"


_CLOCK_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[–-]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _clock_to_seconds(mm: str, ss: str, hh: str | None) -> float:
    if hh is not None:
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    return int(mm) * 60 + int(ss)


def _section_windows(line: str) -> list[dict]:
    """Video provenance from a per-section source-reference line, if any."""
    m = _CLOCK_RANGE_RE.search(line)
    if not m:
        return []
    a = _clock_to_seconds(m.group(1), m.group(2), m.group(3))
    b = _clock_to_seconds(m.group(4), m.group(5), m.group(6))
    if b < a:
        a, b = b, a
    return [{"start_s": a, "end_s": b}]


def _is_structural(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith(("![", "|", ">", "*", "#", "-", "```", "<")):
        return True
    if re.match(r"^\d+[.)]\s", s):
        return False  # numbered steps carry claims
    return False


def _section_window_map(lines: list[str]) -> dict[str, list[dict]]:
    """First pass: admitted video windows per section.

    Source-reference lines may follow the prose they describe, so windows
    are collected up front and bound section-wide during extraction.
    """
    windows: dict[str, list[dict]] = {}
    section_id = ""
    for line in lines:
        m = re.match(r"^##\s+(.*)", line)
        if m:
            section_id = _section_id(m.group(1).strip())
            continue
        if section_id and line.strip().startswith("*Source"):
            found = _section_windows(line)
            if found:
                windows[section_id] = found
    return windows


def extract_candidates(markdown: str) -> list[CandidateClaim]:
    """All atomic candidates in document order (section order, then offset)."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    window_map = _section_window_map(lines)
    candidates: list[CandidateClaim] = []
    heading = ""
    section_id = ""
    section_windows: list[dict] = []
    body_chars = 0  # offset base within the current section body
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^##\s+(.*)", line)
        if m:
            heading = m.group(1).strip()
            section_id = _section_id(heading)
            section_windows = window_map.get(section_id, [])
            body_chars = 0
            continue
        if not section_id:
            continue
        if _is_structural(line):
            continue
        for sent in _SENTENCE_SPLIT_RE.split(line.strip()):
            sent = sent.strip()
            if len(sent) < MIN_CLAIM_CHARS or not re.search(r"[A-Za-z0-9]", sent):
                continue
            kind, checkable = _classify(sent)
            offset = body_chars + max(0, line.find(sent))
            candidates.append(CandidateClaim(
                section_id=section_id, quote=sent, offset=offset,
                claim_kind=kind, priority=_priority(sent, kind),
                checkable=checkable, attribution=_attribution(sent),
                video_provenance=list(section_windows)))
        body_chars += len(line) + 1
    return candidates


def select_claims(candidates: list[CandidateClaim],
                  max_claims: int) -> tuple[list[CandidateClaim], list[dict]]:
    """Split into selected (checkable, by priority) and skipped records.

    Ties break by document order, so selection is fully deterministic.
    Non-checkable kinds and below-cap material are recorded as skipped
    with explicit reasons — never silently dropped.
    """
    order = {id(c): i for i, c in enumerate(candidates)}
    checkable = [c for c in candidates if c.checkable]
    checkable.sort(key=lambda c: (-c.priority, order[id(c)]))
    selected = checkable[:max_claims]
    selected_ids = {id(c) for c in selected}
    skipped = []
    for c in candidates:
        if id(c) in selected_ids:
            continue
        if not c.checkable:
            reason = f"kind {c.claim_kind} carries no truth verdict"
        elif c.claim_kind in NON_CHECKABLE_KINDS:
            reason = f"kind {c.claim_kind} is not fact-checkable"
        else:
            reason = "below priority cap"
        skipped.append({"section_id": c.section_id, "quote": c.quote,
                        "claim_kind": c.claim_kind, "reason": reason})
    return selected, skipped
