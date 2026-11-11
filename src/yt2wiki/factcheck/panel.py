"""Separate reader presentation + export sidecars (offline, pure functions).

The canonical article stays legible and untouched: one compact
"Independent fact-check" panel near the article, per-claim results at
their section anchors available via a collapsible view, text labels
(never colour alone), and an `unavailable` state when nothing current
exists. Markdown/ZIP exports keep `article.md` byte-identical and add a
separate `factcheck.md` sidecar.
"""

from __future__ import annotations

import html as _html

from .contracts import OverlayRecord

_STATUS_LABEL = {
    "complete": "complete",
    "partial": "partial — some claims could not be retrieved",
    "unavailable": "unavailable — no external evidence retrieved",
    "failed": "failed — no verdicts recorded",
}

_ASSESSMENT_LABEL = {
    "corroborated": "Corroborated",
    "contradicted": "Contradicted",
    "disputed": "Disputed",
    "insufficient_evidence": "Insufficient evidence",
    "not_applicable": "Not checked",
    "not_checked": "Not checked",
}


def _esc(text: str | None) -> str:
    return _html.escape(str(text or ""), quote=True)


def _breakdown_line(overlay: OverlayRecord) -> str:
    by_assessment = overlay.coverage.get("by_assessment", {})
    bits = [f"{_ASSESSMENT_LABEL.get(k, k)}: {v}"
            for k, v in by_assessment.items() if v]
    return " · ".join(bits)


def _claim_meta_html(claim) -> str:
    bits = []
    if claim.attribution and claim.attribution != "article_narration":
        bits.append(f"attributed to {_esc(claim.attribution)}")
    for span in claim.video_provenance or []:
        try:
            bits.append(f"source video {_esc(str(span.get('start_s')))}–"
                        f"{_esc(str(span.get('end_s')))}s")
        except AttributeError:
            continue
    if not bits:
        return ""
    return f'<p class="fc-meta">{"; ".join(bits)}</p>'


def _collection_banner(overlay: OverlayRecord) -> str:
    """True when the run collected evidence without semantic assessment."""
    if overlay.verifier.get("mode", "collection") == "assessed":
        return ""
    return ("No semantic assessment was run (no verifier): passages below "
            "were collected as authentic quotations of their sources. "
            "Collection is not fact-checking — no factual verdict is rendered.")


def render_panel_html(overlay: OverlayRecord) -> str:
    """Accessible overlay panel HTML for the shared reader."""
    checked = overlay.coverage.get("checked_claims", len(overlay.claims))
    eligible = overlay.coverage.get("eligible_claims", checked)
    retrieved = overlay.coverage.get("retrieved_claims", 0)
    skipped = overlay.coverage.get("skipped_claims", [])
    n_skipped = len(skipped) if isinstance(skipped, list) else int(skipped or 0)
    status = _STATUS_LABEL.get(overlay.run_status, overlay.run_status)
    date = _esc(overlay.checked_at[:10] if overlay.checked_at else "unknown date")
    banner = _collection_banner(overlay)
    parts = [
        '<section class="factcheck-panel" aria-label="Independent fact-check">',
        "<h2>Independent fact-check</h2>",
        f"<p>External corroboration checked {date}: "
        f"{checked} of {eligible} material claims checked "
        f"({retrieved} with retrieved passages, {n_skipped} skipped). "
        f"Run {status}.</p>",
    ]
    if banner:
        parts.append(f"<p><strong>{_esc(banner)}</strong></p>")
    breakdown = _breakdown_line(overlay)
    if breakdown:
        parts.append(f"<p>{_esc(breakdown)}</p>")
    parts.extend([
        "<p>What the video claims and what independent evidence says are "
        "shown separately. Unchecked text was not verified. "
        "Collected passages are authentic quotations of their sources; "
        "only verifier-assessed relationships support a verdict.</p>",
        "<ul>",
    ])
    for claim in overlay.claims:
        label = _ASSESSMENT_LABEL.get(claim.assessment, claim.assessment)
        parts.append(
            f'<li data-section="{_esc(claim.section_id)}">'
            f"<strong>{_esc(label)}</strong> "
            f"<span>{_esc(claim.quote)}</span>"
            f"{_claim_meta_html(claim)}"
            f"<details><summary>Sources and reasons</summary>"
            f"<p>{_esc(claim.reason)}</p>")
        if claim.evidence:
            parts.append("<ul>")
            for ev in claim.evidence:
                link = (f'<a href="{_esc(ev.url)}" rel="noopener noreferrer">'
                        f"{_esc(ev.title or ev.url)}</a>" if ev.url else _esc(ev.title))
                parts.append(
                    f"<li>{link} — {_esc(ev.publisher)}"
                    f"{f' (published {_esc(ev.published_at)})' if ev.published_at else ''}"
                    f"{f' (retrieved {_esc(ev.retrieved_at)})' if ev.retrieved_at else ''}"
                    f"<blockquote>{_esc(ev.excerpt)}</blockquote>"
                    f"<p>Passage: {_esc(ev.locator)} · "
                    f"relation: {_esc(ev.relationship)}</p></li>")
            parts.append("</ul>")
        parts.append("</details></li>")
    parts.append("</ul></section>")
    return "\n".join(parts)


def render_stale_html(overlay: OverlayRecord) -> str:
    """Historical overlay bound to an older revision — never current."""
    date = _esc(overlay.checked_at[:10] if overlay.checked_at else "unknown date")
    return (
        '<section class="factcheck-panel" aria-label="Independent fact-check">'
        "<h2>Independent fact-check</h2>"
        f"<p>Checked {date} against an older revision of this article; "
        "not current for the text shown. No current verification exists — "
        "nothing here has been verified.</p>"
        "</section>"
    )


def render_unavailable_html() -> str:
    """Visible non-endorsement when no current overlay exists."""
    return (
        '<section class="factcheck-panel" aria-label="Independent fact-check">'
        "<h2>Independent fact-check</h2>"
        "<p>Not checked — no independent external corroboration has been "
        "run for this article revision. Nothing here has been verified.</p>"
        "</section>"
    )


def render_factcheck_md(overlay: OverlayRecord) -> str:
    """Standalone `factcheck.md` sidecar (canonical `article.md` untouched)."""
    checked = overlay.coverage.get("checked_claims", len(overlay.claims))
    eligible = overlay.coverage.get("eligible_claims", checked)
    lines = [
        "# Independent fact-check (addendum)",
        "",
        f"Article: `{overlay.article_id}`",
        f"Article revision: `{overlay.article_revision_sha256}`",
        f"Checked: {overlay.checked_at} · run {overlay.run_status}",
        f"Coverage: {checked} of {eligible} material claims checked.",
        "",
        "This addendum assesses selected claims against independent "
        "retrieved evidence. It does not alter the canonical article, its "
        "citations, or its publication status. Unchecked text was not verified.",
        "",
    ]
    banner = _collection_banner(overlay)
    if banner:
        lines.append(f"NOTE: {banner}")
        lines.append("")
    else:
        verifier = overlay.verifier
        lines.append(f"Verifier: assessed (role={verifier.get('role')} "
                     f"model={verifier.get('model')} "
                     f"policy v{verifier.get('policy_version')}; "
                     f"output cap {verifier.get('max_tokens')} tokens, "
                     f"call timeout {verifier.get('timeout_s')}s)")
        lines.append("")
    breakdown = _breakdown_line(overlay)
    if breakdown:
        lines.append(f"Verdicts: {breakdown}")
        lines.append("")
    for claim in overlay.claims:
        label = _ASSESSMENT_LABEL.get(claim.assessment, claim.assessment)
        lines.append(f"## [{label}] {claim.quote}")
        lines.append("")
        lines.append(f"- Section: `{claim.section_id}` · kind: {claim.claim_kind} "
                     f"· attribution: {claim.attribution or 'article_narration'}")
        for span in claim.video_provenance or []:
            try:
                lines.append(f"- Source video span: {span.get('start_s')}–"
                             f"{span.get('end_s')}s")
            except AttributeError:
                continue
        lines.append(f"- Reason: {claim.reason}")
        for ev in claim.evidence:
            lines.append(f"- Evidence ({ev.relationship}): [{ev.title}]({ev.url})"
                         + (f" — {ev.publisher}" if ev.publisher else "")
                         + (f" (published {ev.published_at})" if ev.published_at else "")
                         + (f" (retrieved {ev.retrieved_at})" if ev.retrieved_at else ""))
            if ev.excerpt:
                lines.append(f"  > {ev.excerpt}")
                lines.append(f"  > Passage: {ev.locator}")
        lines.append("")
    skipped = overlay.coverage.get("skipped_claims", [])
    if skipped:
        lines.append(f"## Not checked ({len(skipped)})")
        lines.append("")
        for s in skipped:
            lines.append(f"- ({s.get('claim_kind')}) {s.get('quote')} "
                         f"— {s.get('reason')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
