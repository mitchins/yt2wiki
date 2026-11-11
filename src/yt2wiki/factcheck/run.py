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

DEFAULT_MAX_VERIFIER_CALLS = 40

@dataclass
class RunBudgets:
    max_claims: int = DEFAULT_MAX_CLAIMS
    queries_per_claim: int = DEFAULT_QUERIES_PER_CLAIM
    pages_per_claim: int = DEFAULT_PAGES_PER_CLAIM
    fetch_bytes: int = DEFAULT_FETCH_BYTES
    verifier: object = None  # optional VerifierFn; None = collection only
    max_verifier_calls: int = DEFAULT_MAX_VERIFIER_CALLS
    primary_domains: tuple[str, ...] = ()  # issuer/official domains, operator-set


@dataclass
class RunReport:
    overlay: OverlayRecord
    searches: int = 0
    fetches: int = 0
    fetch_bytes: int = 0
    failures: list[str] = field(default_factory=list)
    verifier_calls: int = 0
    verifier_budget: int | None = None
    verifier_exhausted: bool = False
    verifier_errors: int = 0


def _utcnow() -> str:
    return (datetime.datetime.now(datetime.UTC)
            .isoformat(timespec="seconds"))


def _claim_queries(quote: str, limit: int) -> list[str]:
    """Bounded query proposals: the claim, a head variant, keywords.

    Stage C qualification showed tail fragments routinely return zero
    results ("No results found") while wasting a request and a politeness
    pause; the third slot is a keyword query from distinctive content
    words instead. Deterministic; the per-claim budget stays enforced here.
    """
    words = quote.split()
    queries = [quote[:220]]
    if len(words) > 8:
        queries.append(" ".join(words[:12])[:220])
    keywords = [w.strip(".,;:!?\"'()").lower() for w in words]
    keywords = [w for w in dict.fromkeys(keywords)
                if len(w) >= 5 and w.isalpha()
                and w not in _PASSAGE_STOPWORDS][:6]
    if len(keywords) >= 3:
        queries.append(" ".join(keywords)[:220])
    seen: list[str] = []
    for q in queries:
        if q and q not in seen:
            seen.append(q)
    return seen[:limit]


def _gated_verifier(budgets: RunBudgets, report: RunReport):
    """Wrap the verifier with the finite per-run call budget.

    Exhaustion leaves remaining passages unassessed (context_only) and
    flags the run partial — never silently, never with invented verdicts.
    """
    base = budgets.verifier
    if base is None:
        return None
    state = {"calls": 0, "exhausted": False}

    def gated(claim: str, excerpt: str, url: str):
        if state["calls"] >= budgets.max_verifier_calls:
            state["exhausted"] = True
            return ("context_only", "", "verifier budget exhausted")
        state["calls"] += 1
        out = base(claim, excerpt, url)
        report.verifier_calls = state["calls"]
        if out[2].startswith("verifier call failed") or out[2] == (
                "verifier output rejected by schema gate"):
            report.verifier_errors += 1
        return out

    gated.state = state  # type: ignore[attr-defined]
    return gated


def _assess_one(provider: EvidenceProvider, record: ClaimRecord,
                budgets: RunBudgets, report: RunReport) -> ClaimRecord:
    verified: list[EvidenceRecord] = []
    seen_urls: set[str] = set()
    # Origin collapse: byte-identical fetched text is the same report, not
    # two independent corroborators. The first page's group stands; exact
    # copies merge into it. (Near-duplicate rewrites are not detectable
    # this cheaply — recorded residual, still needs a verifier's judgment.)
    origin_group: dict[str, str] = {}
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
            if hit.url in seen_urls:
                continue
            seen_urls.add(hit.url)
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
            if page.content_hash and page.content_hash in origin_group:
                group = origin_group[page.content_hash]
            elif page.content_hash:
                origin_group[page.content_hash] = group
            # Collection only: a verbatim excerpt proves the quotation is
            # authentic, not the relationship. Only a verifier may propose
            # supports/contradicts/qualifies, and the combiner still disposes.
            verified.append(EvidenceRecord(
                url=page.url, title=page.title or hit.title,
                publisher=hit.publisher or _host_publisher(page.url),
                published_at=page.published_at,
                retrieved_at=_utcnow(), locator=locator, excerpt=excerpt,
                relationship="context_only", independence_group=group,
                content_hash=page.content_hash))
            if len(verified) >= budgets.pages_per_claim:
                break
        if len(verified) >= budgets.pages_per_claim:
            break
    gated = _gated_verifier(budgets, report)
    record = assess_claim(record, verified, verifier=gated)
    if getattr(gated, "state", {}).get("exhausted"):
        record.reason += " (verifier budget exhausted; remaining passages unassessed)"
        report.verifier_exhausted = True
    if budgets.verifier is None and verified:
        record.reason += (" Passages were collected but no verifier assessed "
                          "their relationship to the claim: keyword overlap "
                          "is not corroboration.")
    if failed and record.assessment in ("not_checked", "insufficient_evidence"):
        record.reason += " (retrieval partially failed; no verdict invented)"
    return record


def _host_publisher(url: str) -> str:
    """Publisher fallback: registrable host when the hit names none."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


_PASSAGE_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "from", "that", "this", "these", "those", "are", "was", "were", "has",
    "have", "had", "been", "will", "would", "can", "could", "its", "as",
    "at", "by", "be", "is", "it", "not", "but", "they", "their", "he",
    "she", "them", "his", "her", "you", "your", "our", "we", "then",
    "than", "when", "where", "which", "who", "what", "how", "all", "any",
    "more", "most", "other", "into", "out", "over", "after", "before",
    "between", "during", "through", "according", "says", "said",
})


def _relevant_passage(page_text: str, quote: str) -> str | None:
    """Claim-relevant passage genuinely present in the fetched page, if any.

    First the longest ordered run of claim words (a direct statement of
    the claim). Fallback: a window covering several distinct claim content
    words — contradictions rarely repeat the claim verbatim, but a page
    that shares its entities and still says otherwise is exactly what the
    verifier must assess. Either way the verifier disposes; the gate only
    establishes genuine presence in the fetched material.
    """
    import re as _re
    words = [w for w in _re.findall(r"[A-Za-z0-9%.'-]+", quote.lower())
             if len(w) > 1]
    if len(words) < 4:
        return None
    norm = _re.sub(r"\s+", " ", page_text).strip()
    low = norm.lower()
    for width in range(min(len(words), 14), 3, -1):
        for i in range(len(words) - width + 1):
            run = " ".join(words[i:i + width])
            at = low.find(run)
            if at >= 0:
                start = max(0, at - 120)
                end = min(len(norm), at + len(run) + 120)
                return norm[start:end].strip()
    # Fallback: minimal window covering distinct claim content words.
    content = [w for w in dict.fromkeys(words)
               if len(w) >= 4 and w.isalpha() and w not in _PASSAGE_STOPWORDS]
    need = max(3, len(content) // 3)
    occurrences: list[tuple[int, str]] = []
    for w in content:
        at = low.find(w)
        while at >= 0:
            occurrences.append((at, w))
            at = low.find(w, at + 1)
    occurrences.sort()
    best_span: tuple[int, int] | None = None
    for i in range(len(occurrences)):
        seen: set[str] = set()
        for j in range(i, len(occurrences)):
            seen.add(occurrences[j][1])
            if len(seen) >= need:
                span = (occurrences[i][0], occurrences[j][0])
                if best_span is None or span[1] - span[0] < best_span[1] - best_span[0]:
                    best_span = span
                break
    if best_span is None or best_span[1] - best_span[0] > 2_000:
        return None
    start = max(0, best_span[0] - 80)
    end = min(len(norm), best_span[1] + 80)
    return norm[start:end].strip()


def run_factcheck(db, article_id: str, *, provider: EvidenceProvider,
                  max_claims: int = DEFAULT_MAX_CLAIMS,
                  verifier=None,
                  primary_domains: tuple[str, ...] = (),
                  verifier_role: str | None = None,
                  verifier_model: str | None = None,
                  max_verifier_calls: int = DEFAULT_MAX_VERIFIER_CALLS,
                  ) -> RunReport:
    """Explicit fact-check run: read canonical revision, persist new overlay."""
    budgets = RunBudgets(max_claims=max_claims, verifier=verifier,
                         primary_domains=primary_domains,
                         max_verifier_calls=max_verifier_calls)
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
    # Non-checkable kinds (opinion, normative, preference, prediction,
    # satire) are recorded as not_applicable verdicts — never hidden in
    # `skipped`, never sent for retrieval.
    na_keys = set()
    for i, cand in enumerate(c for c in candidates if not c.checkable):
        na_keys.add((cand.section_id, cand.quote))
        na_record = ClaimRecord(
            claim_id=f"n{i + 1:02d}", section_id=cand.section_id,
            quote=cand.quote, offset=cand.offset,
            claim_kind=cand.claim_kind,
            attribution=cand.attribution,
            video_provenance=list(cand.video_provenance))
        assessed.append(assess_claim(na_record, []))
    skipped = [s for s in skipped
               if (s["section_id"], s["quote"]) not in na_keys]
    for i, cand in enumerate(selected):
        record = ClaimRecord(
            claim_id=f"c{i + 1:02d}", section_id=cand.section_id,
            quote=cand.quote, offset=cand.offset,
            claim_kind=cand.claim_kind,
            attribution=cand.attribution,
            video_provenance=list(cand.video_provenance))
        assessed.append(_assess_one(provider, record, budgets, report))

    checked = len(assessed)
    by_assessment: dict[str, int] = {}
    retrieved = 0
    for c in assessed:
        by_assessment[c.assessment] = by_assessment.get(c.assessment, 0) + 1
        if c.evidence:
            retrieved += 1
    failed_claims = sum(1 for c in assessed
                        if c.assessment in ("not_checked", "insufficient_evidence")
                        and "retrieval partially failed" in c.reason)
    report.verifier_budget = (budgets.max_verifier_calls
                              if budgets.verifier is not None else None)
    exhausted = report.verifier_exhausted
    if checked == 0 and skipped:
        status = "unavailable"
    elif failed_claims or exhausted or report.verifier_errors > 0 or any(
            "retrieval partially failed" in c.reason for c in assessed):
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
        verifier={
            "provider": getattr(provider, "name", "unknown"),
            "mode": "assessed" if verifier is not None else "collection",
            "role": verifier_role, "model": verifier_model,
            "policy_version": 1,
            **(getattr(verifier, "limits", None) or {}),
            **({"max_verifier_calls": budgets.max_verifier_calls}
               if verifier is not None else {}),
        },
        run_status=status,
        coverage={"eligible_claims": len(candidates),
                  "checked_claims": checked,
                  "retrieved_claims": retrieved,
                  "skipped_claims": skipped,
                  "by_assessment": by_assessment},
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
