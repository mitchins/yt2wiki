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
    "according to",
)

_FIRST_PERSON = (" i ", " we ", " my ", " our ", " me ", " us ")

_CAUSAL_MARKERS = (
    "because", "causes", "caused by", "leads to", "lead to", "results in",
    "due to", "makes it", "triggers", "prevents",
)

_ADVICE_MARKERS = (
    "should", "must", "never ", "always ", "keep ", "use ",
    "make sure", "avoid ", "don't ", "do not ",
)

_FUTURE_MARKERS = ("will ", "going to", "predict", "forecast", "by 203")

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


def _section_id(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
    return slug or "section"


def _classify(sentence: str) -> tuple[str, bool]:
    """Return (claim_kind, checkable). Never editorialises the wording."""
    low = f" {sentence.lower()} "
    quoted = bool(_QUOTED_RE.search(sentence))
    attributed = quoted or any(v in low for v in _ATTRIBUTION_VERBS)
    first_person = any(p in low for p in _FIRST_PERSON)

    has_number = bool(_NUMBER_RE.search(sentence) or _UNIT_RE.search(sentence))
    is_causal = any(m in low for m in _CAUSAL_MARKERS)
    is_advice = sentence.strip().rstrip(".").split()
    imperative = bool(is_advice) and is_advice[0][0].isupper() and is_advice[0].lower() not in (
        "the", "this", "that", "these", "those", "it", "they", "he", "she", "a", "an")
    is_advice_flag = imperative or any(m in low for m in _ADVICE_MARKERS)
    is_future = any(m in low for m in _FUTURE_MARKERS)

    # Normative / preference language without checkable content.
    if (attributed or first_person) and not has_number and not is_causal:
        kind = "attributed_opinion" if attributed else "personal_preference"
        return kind, False
    if is_future and not has_number:
        return "prediction", False
    if is_advice_flag and has_number:
        # The factual premise of the advice is checkable (e.g. an asserted
        # fee, effectiveness or safety property); the advice itself is not.
        return "actionable_factual_premise", True
    if is_causal:
        return "causal", True
    if has_number:
        return "empirical", True
    if attributed and has_number:
        return "empirical", True
    return "attributed_opinion", False


def _priority(sentence: str, kind: str) -> int:
    low = sentence.lower()
    score = 0
    if _NUMBER_RE.search(sentence):
        score += 2  # quantitatively precise
    if kind == "causal":
        score += 1
    if kind == "actionable_factual_premise":
        score += 1  # consequential to conclusions
    if any(w in low for w in _HIGH_STAKES):
        score += 1  # high-stakes
    return score


def _is_structural(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith(("![", "|", ">", "*", "#", "-", "```", "<")):
        return True
    if re.match(r"^\d+[.)]\s", s):
        return False  # numbered steps carry claims
    return False


def extract_candidates(markdown: str) -> list[CandidateClaim]:
    """All atomic candidates in document order (section order, then offset)."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    candidates: list[CandidateClaim] = []
    heading = ""
    section_id = ""
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
            body_chars = 0
            continue
        if not section_id or _is_structural(line):
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
                checkable=checkable))
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
