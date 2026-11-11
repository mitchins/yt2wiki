"""Fact-check overlay contracts: immutable revision binding, record schema.

Stage A (offline): pure data contracts, no network, no models. A fact-check
overlay binds to the exact canonical markdown of a published article
revision (`article_revision_sha256`). A changed or manually edited article
invalidates the current overlay — old overlays stay in history but are
never presented as current.

The overlay is advisory and separately versioned. It never rewrites the
canonical article, alters source citations, touches gate v2, or changes
publication status.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field

# Version of the overlay record schema (persisted per overlay row).
OVERLAY_SCHEMA_VERSION = 1

# Default cap on claims assessed per overlay run (mission: 8-12).
DEFAULT_MAX_CLAIMS = 10

# Per-claim retrieval budgets (overridable per run, never unbounded).
DEFAULT_QUERIES_PER_CLAIM = 3
DEFAULT_PAGES_PER_CLAIM = 4
DEFAULT_FETCH_BYTES = 200_000

ASSESSMENTS = (
    "corroborated",
    "contradicted",
    "disputed",
    "insufficient_evidence",
    "not_applicable",
    "not_checked",
)

RUN_STATUSES = ("complete", "partial", "unavailable", "failed")

CLAIM_KINDS = (
    "empirical",
    "causal",
    "actionable_factual_premise",
    "attributed_opinion",
    "normative_judgment",
    "personal_preference",
    "prediction",
    "satire",
)

# Claim kinds that carry no truth verdict (extracted but never assessed).
NON_CHECKABLE_KINDS = frozenset({
    "attributed_opinion",
    "normative_judgment",
    "personal_preference",
    "prediction",
    "satire",
})

EVIDENCE_RELATIONSHIPS = ("supports", "contradicts", "qualifies", "context_only")


def revision_sha256(markdown: str) -> str:
    """Canonical binding: sha256 over CRLF-normalised article markdown."""
    normalised = markdown.replace("\r\n", "\n").encode("utf-8")
    return hashlib.sha256(normalised).hexdigest()


def new_overlay_id() -> str:
    return f"fc-{uuid.uuid4().hex[:12]}"


@dataclass
class EvidenceRecord:
    url: str
    title: str = ""
    publisher: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    locator: str = ""
    excerpt: str = ""
    relationship: str = "context_only"
    independence_group: str = ""
    content_hash: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.url:
            errors.append("evidence.url is required")
        if self.relationship not in EVIDENCE_RELATIONSHIPS:
            errors.append(f"evidence.relationship must be one of {EVIDENCE_RELATIONSHIPS}")
        if self.relationship in ("supports", "contradicts", "qualifies") and not self.excerpt:
            errors.append("assessed evidence requires a verbatim excerpt")
        return errors


@dataclass
class ClaimRecord:
    claim_id: str
    section_id: str
    quote: str
    offset: int = 0
    video_provenance: list[dict] = field(default_factory=list)
    attribution: str = "article_narration"
    claim_kind: str = "empirical"
    assessment: str = "not_checked"
    reason: str = ""
    evidence: list[EvidenceRecord] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors = []
        if not self.claim_id:
            errors.append("claim.claim_id is required")
        if not self.quote:
            errors.append("claim.quote is required (exact article wording)")
        if self.claim_kind not in CLAIM_KINDS:
            errors.append(f"claim.claim_kind must be one of {CLAIM_KINDS}")
        if self.assessment not in ASSESSMENTS:
            errors.append(f"claim.assessment must be one of {ASSESSMENTS}")
        for ev in self.evidence:
            errors.extend(f"{self.claim_id}: {e}" for e in ev.validate())
        return errors


@dataclass
class OverlayRecord:
    schema_version: int
    overlay_id: str
    article_id: str
    article_revision_sha256: str
    article_type: str | None
    checked_at: str
    verifier: dict
    run_status: str
    coverage: dict
    claims: list[ClaimRecord] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors = []
        if self.schema_version != OVERLAY_SCHEMA_VERSION:
            errors.append(f"schema_version must be {OVERLAY_SCHEMA_VERSION}")
        if not self.overlay_id or not self.article_id:
            errors.append("overlay_id and article_id are required")
        if len(self.article_revision_sha256) != 64:
            errors.append("article_revision_sha256 must be a sha256 hex digest")
        if self.run_status not in RUN_STATUSES:
            errors.append(f"run_status must be one of {RUN_STATUSES}")
        for key in ("eligible_claims", "checked_claims", "retrieved_claims",
                    "skipped_claims", "by_assessment"):
            if key not in self.coverage:
                errors.append(f"coverage.{key} is required")
        by_assessment = self.coverage.get("by_assessment", {})
        if isinstance(by_assessment, dict):
            for verdict in by_assessment:
                if verdict not in ASSESSMENTS:
                    errors.append(f"coverage.by_assessment has unknown verdict {verdict}")
        for claim in self.claims:
            errors.extend(claim.validate())
        return errors

    def to_dict(self) -> dict:
        return asdict(self)
