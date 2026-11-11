"""Evidence-grounded assessment: a deterministic combiner, not a judge.

The combiner maps verified evidence relationships to verdicts. A model
verifier (stage B) may *propose* relationships and reasons, but the citation
gate in `evidence.py` disposes: unverified excerpts never support a
verdict, and no unsupported categorical verdict is ever recorded.

Corroboration rules (independent corroboration, not recycled assertion):
- `corroborated`: support from >=1 primary record, or from >=2 distinct
  independence groups of secondary records. One strong primary record can
  outweigh several recycled articles.
- `insufficient_evidence`: no usable evidence, single uncorroborated
  secondary, primary evidence absent where required — or retrieved passages
  that only qualify or mention the claim without deciding it.
- `contradicted`: credible contradiction with no credible support.
- `disputed`: credible support and credible contradiction coexist.
- `insufficient_evidence`: no usable evidence, single uncorroborated
  secondary, or primary evidence absent where the matrix requires it.
- `not_applicable`: claim kind carries no truth verdict.
- `not_checked`: selected but unassessed (budgets exhausted, fetch failed).

"Independence group" identifies the originating record behind a page
(e.g. the issuer schedule, the original study): syndicated copies share
one group and never count as independent confirmation.
"""

from __future__ import annotations

from collections.abc import Callable

from .contracts import ClaimRecord, EvidenceRecord

# A single secondary page asserting alone never corroborates.
_MIN_SECONDARY_GROUPS = 2


def _groups(evidence: list[EvidenceRecord],
            relationship: str) -> set[str]:
    return {e.independence_group or e.url
            for e in evidence if e.relationship == relationship}


def _has_primary(evidence: list[EvidenceRecord], relationship: str,
                 primary_markers: frozenset[str] | None = None) -> bool:
    """Whether a primary record supports/contradicts (stage A: URL-marked).

    Stage A has no source-kind classifier: a page whose independence group
    is explicitly marked `primary:<name>` counts as primary. Stage B wires
    the real classifier; the combiner logic is unchanged.
    """
    _ = primary_markers
    return any(e.relationship == relationship
               and e.independence_group.startswith("primary:")
               for e in evidence)


def combine(claim_kind: str, claim_text: str,
            evidence: list[EvidenceRecord]) -> tuple[str, str]:
    """Return (assessment, reason) for verified evidence only."""
    from .contracts import NON_CHECKABLE_KINDS
    if claim_kind in NON_CHECKABLE_KINDS:
        return ("not_applicable",
                f"Claim kind {claim_kind} carries no truth verdict; "
                "checked for separable empirical premises only.")
    assessed = [e for e in evidence
                if e.relationship in ("supports", "contradicts", "qualifies")
                and e.excerpt and e.locator]
    supports = [e for e in assessed if e.relationship == "supports"]
    contradicts = [e for e in assessed if e.relationship == "contradicts"]
    if not assessed:
        return ("insufficient_evidence",
                "No retrieved passage directly supports or contradicts the claim.")
    if contradicts and not supports:
        src = contradicts[0]
        return ("contradicted",
                f"Independent evidence contradicts the claim "
                f"({src.publisher or src.title or src.url}).")
    if contradicts and supports:
        return ("disputed",
                "Credible retrieved material both supports and contradicts "
                "the claim; shown separately, not resolved.")
    support_groups = _groups(supports, "supports")
    if not supports and not contradicts:
        # Passages present but none directly supports or contradicts (only
        # context or qualifiers): not corroboration, and nothing to dispute.
        return ("insufficient_evidence",
                "Retrieved passages only qualify or mention the claim; none "
                "directly supports or contradicts it.")
    if _has_primary(supports, "supports") or len(support_groups) >= _MIN_SECONDARY_GROUPS:
        src = supports[0]
        return ("corroborated",
                f"Independent evidence supports the claim "
                f"({src.publisher or src.title or src.url}).")
    return ("insufficient_evidence",
            "Only a single uncorroborated secondary page supports the claim; "
            "syndicated copies do not count as independent confirmation.")


# A verifier proposes (relationship, independence_group, reason) per page;
# the citation gate and combiner dispose. Signature kept stable for stage B.
VerifierFn = Callable[[str, str, str], tuple[str, str, str]]


def assess_claim(record: ClaimRecord,
                 verified: list[EvidenceRecord],
                 verifier: VerifierFn | None = None) -> ClaimRecord:
    """Attach the verdict to an extracted claim record (no rewriting).

    Source identity is established independently of the verifier: a
    provider-assigned independence group always stands. A verifier may
    only fill an empty group, and never with a trusted `primary:` marker —
    model output can never promote a source into a trusted group.
    """
    if verifier is not None:
        proposed: list[EvidenceRecord] = []
        for ev in verified:
            rel, group, why = verifier(record.quote, ev.excerpt, ev.url)
            ev.relationship = rel
            if not ev.independence_group and group \
                    and not group.startswith("primary:"):
                ev.independence_group = group
            proposed.append(ev)
            _ = why  # records carry the combiner reason, not model prose
        verified = proposed
    assessment, reason = combine(record.claim_kind, record.quote, verified)
    record.assessment = assessment
    record.reason = reason
    record.evidence = verified
    return record
