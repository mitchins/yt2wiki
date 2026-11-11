"""FACTCHECK-01 stage A: offline contracts, ledger, mock runs, presentation.

No network, no models, no GPU. Covers the mission's offline matrix subset:
revision binding, staleness, citation gate (fabricated excerpts rejected),
unavailable/partial failure handling, unsupported-citation accounting, and
legacy byte-identity when the overlay is off.
"""

from __future__ import annotations

import pytest

from yt2wiki.factcheck import assess, claims, contracts, evidence, panel, run, store

SAMPLE_MD = """# Test article

> Source video: https://example.com/v
> Compiled locally from the narration.

## Key facts

The bridge span measures 380 meters in total length across the river valley.

## Method

Torque each bolt to 42 newton meters before the load test begins today.
The presenter says this design is beautiful and inspiring to behold here.
Keep the fee below 0.5 percent according to the presenter's own advice.

## Verdict

This bridge is the finest structure ever built by human hands today.
"""

PAGE_SPAN = (
    "The northern crossing report states the bridge span measures 380 meters "
    "in total length across the river valley, confirmed by survey."
)


def _mock_provider() -> evidence.MockProvider:
    return evidence.MockProvider(
        hits={
            "The bridge span measures 380 meters in total length across the river valley.": [
                evidence.SearchHit(url="https://survey.example/report",
                                   title="Northern crossing survey",
                                   publisher="Survey Office",
                                   published_at="2024-03-01",
                                   snippet="span measures 380 meters..."),
                evidence.SearchHit(url="https://news.example/bridge",
                                   title="Bridge opens",
                                   publisher="Local News",
                                   snippet="span measures 380 meters..."),
            ],
            "Torque each bolt to 42 newton meters before the load test begins today.": [
                evidence.SearchHit(url="https://manual.example/torque",
                                   title="Workshop manual",
                                   publisher="Manual Press",
                                   snippet="torque each bolt..."),
            ],
        },
        pages={
            "https://survey.example/report": evidence.FetchedPage(
                url="https://survey.example/report",
                title="Northern crossing survey", text=PAGE_SPAN,
                published_at="2024-03-01"),
            "https://news.example/bridge": evidence.FetchedPage(
                url="https://news.example/bridge",
                title="Bridge opens", text=PAGE_SPAN),
            "https://manual.example/torque": evidence.FetchedPage(
                url="https://manual.example/torque",
                title="Workshop manual", text=(
                    "Workshop manual: torque each bolt to 42 newton meters before the "
                    "load test begins today, then verify with a calibrated wrench.")),
        },
    )


# ---------------------------------------------------------------- contracts

def test_revision_binding_stable_and_sensitive():
    a = contracts.revision_sha256(SAMPLE_MD)
    b = contracts.revision_sha256(SAMPLE_MD.replace("\n", "\r\n"))
    assert a == b and len(a) == 64
    assert contracts.revision_sha256(SAMPLE_MD + " ") != a


def test_overlay_validation_rejects_bad_records():
    overlay = contracts.OverlayRecord(
        schema_version=contracts.OVERLAY_SCHEMA_VERSION,
        overlay_id="fc-1", article_id="art9-x",
        article_revision_sha256="0" * 64, article_type="report",
        checked_at="2026-01-01T00:00:00+00:00",
        verifier={}, run_status="complete",
        coverage={"eligible_claims": 0, "checked_claims": 0, "skipped_claims": []},
        claims=[],
    )
    assert overlay.validate() == []
    overlay.run_status = "true"
    assert overlay.validate()
    overlay.run_status = "complete"
    overlay.article_revision_sha256 = "short"
    assert overlay.validate()


# ------------------------------------------------------------------- claims

def test_extraction_skips_structure_and_classifies():
    cands = claims.extract_candidates(SAMPLE_MD)
    quotes = [c.quote for c in cands]
    assert any("380 meters" in q for q in quotes)
    assert any("42 newton meters" in q for q in quotes)
    assert not any(q.startswith("*Source") for q in quotes)
    assert not any("![" in q for q in quotes)
    span = next(c for c in cands if "380 meters" in c.quote)
    assert span.claim_kind == "empirical" and span.checkable
    opinion = next(c for c in cands if "beautiful" in c.quote)
    assert not opinion.checkable
    # Deterministic: same input, same order and kinds.
    again = claims.extract_candidates(SAMPLE_MD)
    assert [(c.section_id, c.quote, c.claim_kind) for c in again] == [
        (c.section_id, c.quote, c.claim_kind) for c in cands]


def test_selection_caps_and_records_skipped_reasons():
    cands = claims.extract_candidates(SAMPLE_MD)
    selected, skipped = claims.select_claims(cands, 1)
    assert len(selected) == 1
    assert len(skipped) == len(cands) - 1
    reasons = {s["reason"] for s in skipped}
    assert any("priority cap" in r or "no truth verdict" in r for r in reasons)


# ----------------------------------------------------------------- evidence

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "javascript:alert(1)",
    "http://127.0.0.1/secret",
    "http://10.0.0.5/admin",
    "http://169.254.169.254/latest",
    "http://user:pass@example.com/",
    "https://example.localhost/page",
    "https://[::1]/x",
])
def test_safe_url_rejects(url):
    assert evidence.safe_url(url) is None


def test_safe_url_accepts_public():
    assert evidence.safe_url("https://survey.example/report") is not None


def test_verify_excerpt_gate():
    loc = evidence.verify_excerpt(PAGE_SPAN, "span measures 380 meters in total length")
    assert loc is not None and loc.startswith("char ")
    assert evidence.verify_excerpt(PAGE_SPAN, "span measures 999 meters") is None
    assert evidence.verify_excerpt(PAGE_SPAN, "short") is None
    # Near-quote is not a quote.
    assert evidence.verify_excerpt(PAGE_SPAN, "span measure 380 meters in total length") is None


# ------------------------------------------------------------------ assess

def _ev(url, rel, group="", excerpt="the bridge span measures 380 meters in total length"):
    return contracts.EvidenceRecord(url=url, title="t", publisher="p",
                                    locator="char 0-10", excerpt=excerpt,
                                    relationship=rel, independence_group=group)


def test_combine_rules():
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "supports",
                                                 "primary:issuer-schedule")])
    assert a == "corroborated"
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "supports", "g1"),
                                             _ev("https://b.example", "supports", "g2")])
    assert a == "corroborated"
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "supports", "g1")])
    assert a == "insufficient_evidence"
    # Syndicated copies share one group: no independent confirmation.
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "supports", "wire"),
                                             _ev("https://b.example", "supports", "wire")])
    assert a == "insufficient_evidence"
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "contradicts",
                                                 "primary:archive")])
    assert a == "contradicted"
    a, _ = assess.combine("empirical", "q", [_ev("https://a.example", "supports",
                                                 "primary:x"),
                                             _ev("https://b.example", "contradicts",
                                                 "primary:y")])
    assert a == "disputed"
    a, reason = assess.combine("normative_judgment", "q", [_ev("https://a.example", "supports",
                                                                "primary:x")])
    assert a == "not_applicable" and reason
    a, _ = assess.combine("empirical", "q", [])
    assert a == "insufficient_evidence"


# ---------------------------------------------------------------------- run

def _publish_article(db, article_id="art9-fc-test", markdown=SAMPLE_MD):
    db.execute(
        "INSERT INTO sources(source_id, kind, created_at) VALUES(?,?,1)",
        ("src-fc", "upload"))
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, status, "
        "version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (article_id, "src-fc", "Test article", markdown, "published", 1, 1.0, 1.0))


def test_run_persists_overlay_and_leaves_article_untouched(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    _publish_article(db)
    before = db.query_one("SELECT markdown, version, status FROM articles "
                          "WHERE article_id='art9-fc-test'")
    report = run.run_factcheck(db, "art9-fc-test", provider=_mock_provider())
    after = db.query_one("SELECT markdown, version, status FROM articles "
                         "WHERE article_id='art9-fc-test'")
    assert dict(before) == dict(after)  # canonical row byte-identical
    ov = report.overlay
    assert ov.validate() == []
    assert ov.run_status in ("complete", "partial")
    assert ov.coverage["checked_claims"] >= 1
    by_kind = {c.claim_kind: c for c in ov.claims}
    assert by_kind["empirical"].assessment == "corroborated"
    # Two distinct groups corroborate the span claim.
    span = next(c for c in ov.claims if "380 meters" in c.quote)
    assert span.assessment == "corroborated" and len(span.evidence) == 2
    # History + currency.
    assert store.current_overlay(db, "art9-fc-test").overlay_id == ov.overlay_id
    assert len(store.overlay_history(db, "art9-fc-test")) == 1


def test_stale_overlay_never_presented_as_current(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    _publish_article(db)
    first = run.run_factcheck(db, "art9-fc-test", provider=_mock_provider()).overlay
    # Manual edit republishes the article (new canonical revision).
    db.execute("UPDATE articles SET markdown=markdown || '\nEdited line.' WHERE article_id=?",
               ("art9-fc-test",))
    assert store.current_overlay(db, "art9-fc-test") is None
    history = store.overlay_history(db, "art9-fc-test")
    assert [o.overlay_id for o in history] == [first.overlay_id]  # retained
    second = run.run_factcheck(db, "art9-fc-test", provider=_mock_provider()).overlay
    assert second.overlay_id != first.overlay_id
    assert store.current_overlay(db, "art9-fc-test").overlay_id == second.overlay_id


def test_provider_failure_marks_unavailable_without_verdicts(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    _publish_article(db)
    failing = evidence.MockProvider(
        failures={"*": evidence.SearchFailure("network", "offline")})
    # Force a blanket failure regardless of query.
    def _boom(query, *, limit):
        raise evidence.SearchFailure("network", "offline")
    failing.search = _boom  # type: ignore[method-assign]
    report = run.run_factcheck(db, "art9-fc-test", provider=failing)
    assert report.overlay.run_status in ("partial", "unavailable")
    assert report.searches == 0
    for claim in report.overlay.claims:
        if claim.claim_kind in ("attributed_opinion", "normative_judgment",
                                "personal_preference", "prediction", "satire"):
            # Classification verdicts need no retrieval.
            assert claim.assessment == "not_applicable"
        else:
            assert claim.assessment in ("not_checked", "insufficient_evidence")
        assert not claim.evidence


def test_snippet_only_evidence_never_corroborates(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe widget costs 19 dollars at retail stores "
          "across the country today.\n")
    _publish_article(db, article_id="art9-fc-snip", markdown=md)
    provider = evidence.MockProvider(
        hits={"The widget costs 19 dollars at retail stores across the country today.": [
            evidence.SearchHit(url="https://shop.example/w",
                               title="Widget page",
                               snippet="The widget costs 19 dollars...")]},
        pages={"https://shop.example/w": evidence.FetchedPage(
            url="https://shop.example/w", title="Widget page",
            text="Buy our widget today. Great value, best quality, free shipping.")},
    )
    report = run.run_factcheck(db, "art9-fc-snip", provider=provider)
    claim = report.overlay.claims[0]
    assert claim.evidence == []  # snippet alone is not corroboration
    assert claim.assessment == "insufficient_evidence"


# -------------------------------------------------------------- presentation

def test_panel_escapes_and_labels(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = "# T\n\n## Facts\n\n<script>alert(1)</script> costs 5 dollars today here.\n"
    _publish_article(db, article_id="art9-fc-xss", markdown=md)
    report = run.run_factcheck(db, "art9-fc-xss", provider=evidence.MockProvider())
    html = panel.render_panel_html(report.overlay)
    assert "<script>" not in html
    assert "Independent fact-check" in html
    assert "of " in html and "material claims checked" in html
    md_side = panel.render_factcheck_md(report.overlay)
    assert md_side.startswith("# Independent fact-check (addendum)")
    assert "article.md" in md_side or "canonical article" in md_side


def test_unavailable_panel_states_non_endorsement():
    html = panel.render_unavailable_html()
    assert "Not checked" in html and "Nothing here has been verified" in html


# --------------------------------------------------------------- web seam

def test_factcheck_context_none_without_overlay(tmp_path):
    from tests.fixtures import in_memory_db
    from yt2wiki.web import views
    _, db = in_memory_db(tmp_path)
    _publish_article(db)
    assert views.factcheck_context(db, "art9-fc-test") is None


def test_factcheck_context_panel_with_overlay(tmp_path):
    from tests.fixtures import in_memory_db
    from yt2wiki.web import views
    _, db = in_memory_db(tmp_path)
    _publish_article(db)
    run.run_factcheck(db, "art9-fc-test", provider=_mock_provider())
    ctx = views.factcheck_context(db, "art9-fc-test")
    assert ctx is not None and "Independent fact-check" in str(ctx["panel_html"])
