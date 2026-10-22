"""Evidence records: observations kept strictly separate from generated prose."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .literals import Literal, extract_literals, normalize_value

EVIDENCE_KINDS = (
    "action",
    "prerequisite",
    "tool",
    "material",
    "config",
    "command",
    "code",
    "measurement",
    "identifier",
    "warning",
    "expected_result",
    "error",
    "correction",
    "alternative",
    "unresolved",
    "observation",
)

# Kinds whose loss most damages the article.
CRITICAL_KINDS = {
    "action",
    "command",
    "config",
    "measurement",
    "warning",
    "prerequisite",
    "correction",
    "identifier",
}


STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
    "be", "that", "this", "it", "as", "at", "by", "from", "you", "your", "then",
    "will", "can", "if", "so", "do", "not", "no", "we", "our",
})
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/@-]*", re.IGNORECASE)


def content_tokens(text: str) -> set[str]:
    """Meaningful tokens, trailing punctuation stripped, stopwords removed."""
    out: set[str] = set()
    for match in _TOKEN_RE.findall(str(text or "").lower()):
        tok = match.strip(".,;:!?()[]{}\"'`")
        if len(tok) >= 2 and tok not in STOPWORDS:
            out.add(tok)
    return out


def token_overlap(a: str, b: str) -> float:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def token_coverage(needle: str, haystack: str) -> float:
    """Fraction of `needle`'s content tokens present in `haystack`."""
    tn, th = content_tokens(needle), content_tokens(haystack)
    if not tn:
        return 0.0
    return len(tn & th) / len(tn)


@dataclass
class Evidence:
    evidence_id: str
    kind: str
    text: str
    normalized: str = ""
    literals: list[Literal] = field(default_factory=list)
    t_start: float | None = None
    t_end: float | None = None
    frame_ids: list[str] = field(default_factory=list)
    transcript_spans: list[dict[str, float]] = field(default_factory=list)
    confidence: float = 0.5
    model_key: str = ""
    prompt_version: str = ""
    superseded_by: str | None = None
    status: str = "active"

    def to_row(self, source_id: str) -> tuple:
        return (
            self.evidence_id, source_id, self.kind, self.text, self.normalized,
            _json(self.literals), self.t_start, self.t_end,
            _json(self.frame_ids), _json(self.transcript_spans),
            self.confidence, self.model_key, self.prompt_version,
            self.superseded_by, self.status, 0.0,
        )

    @classmethod
    def from_row(cls, row: Any) -> "Evidence":
        import json

        return cls(
            evidence_id=row["evidence_id"],
            kind=row["kind"],
            text=row["text"],
            normalized=row["normalized"] or "",
            literals=[Literal(**x) if isinstance(x, dict) else x
                      for x in _loads(row["literal_json"], [])],
            t_start=row["t_start"],
            t_end=row["t_end"],
            frame_ids=_loads(row["frame_ids_json"], []),
            transcript_spans=_loads(row["transcript_spans_json"], []),
            confidence=row["confidence"] or 0.5,
            model_key=row["model_key"] or "",
            prompt_version=row["prompt_version"] or "",
            superseded_by=row["superseded_by"],
            status=row["status"],
        )

    def literal_strings(self) -> list[str]:
        return [lit.literal for lit in self.literals]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "text": self.text,
            "normalized": self.normalized,
            "literals": [lit.to_dict() for lit in self.literals],
            "t_start": self.t_start,
            "t_end": self.t_end,
            "frame_ids": self.frame_ids,
            "confidence": self.confidence,
            "model_key": self.model_key,
            "prompt_version": self.prompt_version,
            "status": self.status,
        }

    def support_key(self) -> str:
        """Stable identity used to detect the same fact recorded twice."""
        norm = normalize_value(self.normalized or self.text).lower()
        return re.sub(r"[^a-z0-9]+", " ", norm).strip()


def _json(value: Any) -> str:
    """Serialise dataclass-bearing structures without stringifying them."""
    from .db.schema import json_dumps

    try:
        return json_dumps(value)
    except Exception:
        return "[]"


def _loads(value: Any, default: Any) -> Any:
    import json

    if not value:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def new_evidence(source_id: str, kind: str, text: str, **kwargs: Any) -> Evidence:
    import hashlib

    t_start = kwargs.get("t_start")
    t_end = kwargs.get("t_end")
    digest = hashlib.sha1(
        f"{source_id}|{kind}|{text.strip().lower()}|{t_start}|{t_end}".encode()
    ).hexdigest()[:12]
    evidence_id = f"ev-{digest}"
    if "literals" not in kwargs or not kwargs.get("literals"):
        kwargs["literals"] = extract_literals(text, t_start=t_start, t_end=t_end)
    return Evidence(evidence_id=evidence_id, kind=kind, text=text.strip(), **kwargs)


@dataclass
class ProcedureStep:
    position: int
    text: str
    detail: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    frame_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expected_result: str = ""
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "text": self.text,
            "detail": self.detail,
            "evidence_ids": self.evidence_ids,
            "frame_ids": self.frame_ids,
            "warnings": self.warnings,
            "expected_result": self.expected_result,
            "alternatives": self.alternatives,
        }


@dataclass
class Procedure:
    procedure_id: str
    title: str
    summary: str
    steps: list[ProcedureStep] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedure_id": self.procedure_id,
            "title": self.title,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
            "prerequisites": self.prerequisites,
            "alternatives": self.alternatives,
            "corrections": self.corrections,
            "unresolved": self.unresolved,
            "limitations": self.limitations,
        }


def chunk_transcript(
    rows: Sequence[Any],
    *,
    window_s: float = 90.0,
    overlap_s: float = 15.0,
    max_chars: int = 6000,
) -> list[list[Any]]:
    """Group transcript rows into overlapping analysis windows.

    Bounded window size keeps the prompt small and the token count predictable,
    while the overlap prevents evidence straddling a window boundary from being
    lost.
    """
    if not rows:
        return []
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_start = None
    for row in rows:
        start = float(row["start"])
        if current_start is None:
            current_start = start
        span = start - current_start
        total_chars = sum(len(r["text"] or "") for r in current)
        if current and (span > window_s or total_chars > max_chars):
            chunks.append(current)
            # carry a bounded overlap forward so evidence straddling the boundary
            # is still seen by the next window
            carry: list[Any] = []
            carried = 0.0
            for r in reversed(current):
                if carried >= overlap_s and carry:
                    break
                carry.insert(0, r)
                carried += float(r["end"]) - float(r["start"])
            current = carry
            current_start = float(current[0]["start"]) if current else start
        current.append(row)
    if current:
        chunks.append(current)
    return chunks
