"""Fact-check runner: explicit, bounded, read-only w.r.t. the article.

`run_factcheck` reads one published, immutable article revision and
persists a new, independently versioned overlay row. It never writes the
`articles`, `article_revisions`, `article_assets` or `generations` tables,
never alters source citations, and never changes publication status or
gate v2 outcomes.

Staleness: the overlay binds to `article_revision_sha256`. When the
canonical markdown changes (edit/republish), a stored overlay is retained
historically but `current_overlay` no longer presents it; rechecking
creates a new overlay revision, never an in-place overwrite.

Failure never invents verdicts: provider failures mark the affected claims
`not_checked`/`insufficient_evidence` and the run `partial`/`unavailable`.
No external search runs unless this function is explicitly invoked.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .assess import assess_claim
from .claims import extract_candidates, select_claims
from .contracts import (
    DEFAULT_FETCH_BYTES,
    DEFAULT_MAX_CLAIMS,
    DEFAULT_PAGES_PER_CLAIM,
    DEFAULT_QUERIES_PER_CLAIM,
    OVERLAY_SCHEMA_VERSION,
    ClaimRecord,
    EvidenceRecord,
    OverlayRecord,
    new_overlay_id,
    revision_sha256,
)
from .evidence import (
    EvidenceProvider,
    FetchedPage,
    SearchFailure,
    safe_url,
    verify_excerpt,
)


@dataclass
class RunBudgets:
    max_claims: int = DEFAULT_MAX_CLAIMS
    queries_per_claim: int = DEFAULT_QUERIES_PER_CLAIM
    pages_per_claim: int = DEFAULT_PAGES_PER_CLAIM
    fetch_bytes: int = DEFAULT_FETCH_BYTES
    verifier: object = None  # optional VerifierFn; None = deterministic combiner only
    primary_domains: tuple[str, ...] = ()  # issuer/official domains, operator-set


@dataclass
class RunReport:
    overlay: OverlayRecord
    searches: int = 0
    fetches: int = 0
    fetch_bytes: int = 0
    failures: list[str] = field(default_factory=list)


def _utcnow() -> str:
    return (datetime.datetime.now(datetime.UTC)
            .isoformat(timespec="seconds"))


def _claim_queries(quote: str, limit: int) -> list[str]:
    """Bounded query proposals: the claim itself, then key-phrase variants.

    Stage A proposes queries deterministically (no model). Stage B may use
    a model proposer; the per-claim budget stays enforced here.
    """
    words = quote.split()
    queries = [quote[:220]]
    if len(words) > 8:
        queries.append(" ".join(words[:12])[:220])
        queries.append(" ".join(words[-12:])[:220])
    seen: list[str] = []
    for q in queries:
        if q not in seen:
            seen.append(q)
    return seen[:limit]


def _assess_one(provider: EvidenceProvider, record: ClaimRecord,
                budgets: RunBudgets, report: RunReport) -> ClaimRecord:
    verified: list[EvidenceRecord] = []
    failed = False
    for query in _claim_queries(record.quote, budgets.queries_per_claim):
        try:
            hits = provider.search(query, limit=budgets.pages_per_claim)
            report.searches += 1
        except SearchFailure as exc:
            report.failures.append(f"search failed for {record.claim_id}: {exc.kind}")
            failed = True
            continue
        for hit in hits[:budgets.pages_per_claim]:
            if safe_url(hit.url) is None:
                report.failures.append(
                    f"rejected unsafe evidence URL for {record.claim_id}")
                continue
            try:
                page: FetchedPage = provider.fetch(hit.url)
                report.fetches += 1
                report.fetch_bytes += min(len(page.text.encode("utf-8", "replace")),
                                          budgets.fetch_bytes)
            except SearchFailure as exc:
                report.failures.append(
                    f"fetch failed for {record.claim_id}: {exc.kind}")
                failed = True
                continue
            # The citation gate: snippets alone never corroborate. Only a
            # verbatim passage genuinely present in the fetched page counts,
            # and only the claim-relevant passage is kept.
            excerpt = _relevant_passage(page.text, record.quote)
            locator = verify_excerpt(page.text, excerpt) if excerpt else None
            if locator is None:
                continue
            grouper = getattr(provider, "independence_group", None)
            group = grouper(page.url) if callable(grouper) else ""
            verified.append(EvidenceRecord(
                url=page.url, title=page.title or hit.title,
                publisher=hit.publisher, published_at=page.published_at,
                retrieved_at=_utcnow(), locator=locator, excerpt=excerpt,
                relationship="supports", independence_group=group,
                content_hash=page.content_hash))
            if len(verified) >= budgets.pages_per_claim:
                break
        if len(verified) >= budgets.pages_per_claim:
            break
    record = assess_claim(record, verified, verifier=budgets.verifier)
    if failed and record.assessment in ("not_checked", "insufficient_evidence"):
        record.reason += " (retrieval partially failed; no verdict invented)"
    return record


def _relevant_passage(page_text: str, quote: str) -> str | None:
    """Longest claim-token window present verbatim in the page, if any.

    Stage A overlap heuristic (offline): finds the longest run ofClaim
    words appearing in order in the fetched text and returns that passage.
    Stage B keeps this gate and adds model-proposed passages — still
    verified here before any verdict.
    """
    import re as _re
    words = [w for w in _re.findall(r"[A-Za-z0-9%.'-]+", quote.lower()) if len(w) > 2]
    if len(words) < 4:
        return None
    norm = _re.sub(r"\s+", " ", page_text).strip()
    low = norm.lower()
    # Longest ordered run of claim words present in the page.
    best = ""
    for width in range(min(len(words), 14), 3, -1):
        for i in range(len(words) - width + 1):
            run = " ".join(words[i:i + width])
            at = low.find(run)
            if at >= 0:
                start = max(0, at - 120)
                end = min(len(norm), at + len(run) + 120)
                return norm[start:end].strip()
        if best:
            return best
    return None


def run_factcheck(db, article_id: str, *, provider: EvidenceProvider,
                  max_claims: int = DEFAULT_MAX_CLAIMS,
                  verifier=None,
                  primary_domains: tuple[str, ...] = ()) -> RunReport:
    """Explicit fact-check run: read canonical revision, persist new overlay."""
    budgets = RunBudgets(max_claims=max_claims, verifier=verifier,
                         primary_domains=primary_domains)
    grouper = getattr(provider, "independence_group", None)
    if callable(grouper):
        try:
            provider.primary_domains = tuple(primary_domains)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
    row = db.query_one(
        "SELECT article_id, title, markdown, article_type FROM articles "
        "WHERE article_id=?", (article_id,))
    if row is None:
        raise KeyError(f"no published article {article_id}")
    markdown = row["markdown"]
    revision = revision_sha256(markdown)

    candidates = extract_candidates(markdown)
    selected, skipped = select_claims(candidates, budgets.max_claims)
    report = RunReport(overlay=None)  # type: ignore[arg-type]
    assessed: list[ClaimRecord] = []
    for i, cand in enumerate(selected):
        record = ClaimRecord(
            claim_id=f"c{i + 1:02d}", section_id=cand.section_id,
            quote=cand.quote, offset=cand.offset,
            claim_kind=cand.claim_kind)
        assessed.append(_assess_one(provider, record, budgets, report))

    checked = len(assessed)
    failed_claims = sum(1 for c in assessed
                        if c.assessment in ("not_checked", "insufficient_evidence")
                        and "retrieval partially failed" in c.reason)
    if checked == 0 and skipped:
        status = "unavailable"
    elif failed_claims or any("retrieval partially failed" in c.reason for c in assessed):
        status = "partial"
    else:
        status = "complete"

    overlay = OverlayRecord(
        schema_version=OVERLAY_SCHEMA_VERSION,
        overlay_id=new_overlay_id(),
        article_id=article_id,
        article_revision_sha256=revision,
        article_type=row["article_type"],
        checked_at=_utcnow(),
        verifier={"provider": getattr(provider, "name", "unknown"),
                  "model": None, "policy_version": 1},
        run_status=status,
        coverage={"eligible_claims": len(candidates),
                  "checked_claims": checked,
                  "skipped_claims": skipped},
        claims=assessed,
    )
    errors = overlay.validate()
    if errors:
        raise ValueError(f"invalid overlay record: {errors}")
    _persist_overlay(db, overlay)
    report.overlay = overlay
    return report


def _persist_overlay(db, overlay: OverlayRecord) -> None:
    from .store import persist_overlay as _persist
    _persist(db, overlay)
