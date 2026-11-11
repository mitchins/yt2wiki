"""FACTCHECK-01 corrective review: P0/P1 adversarial qualification.

Proves the corrected design against the defects found on inspection:

P0: collection without a verifier never yields a categorical factual
verdict, even when two independent pages share the claim's entities.
A verifier must assess each exact passage; a verbatim excerpt proves
authenticity of the quotation, not the relationship.

P1: source identity is provider-owned (verifiers cannot mint trusted
groups); plain factual assertions default to empirical; coverage counts
each verdict class separately; timestamps/attribution/provenance are
populated where available; DNS is pinned per request; sidecars and zips
bind to the live revision atomically; stale sidecars never ship.
"""

from __future__ import annotations

import pytest

from yt2wiki.factcheck import assess, claims, store
from yt2wiki.factcheck import evidence as ev
from yt2wiki.factcheck import run as runmod
from yt2wiki.factcheck import verifier as vf


def _article(db, article_id, md, status="published"):
    db.execute("INSERT OR IGNORE INTO sources(source_id, kind, created_at) "
               "VALUES('src-fc-x', 'upload', 1)")
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, status, "
        "version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (article_id, "src-fc-x", "T", md, status, 1, 1.0, 1.0))


class _P(ev.MockProvider):
    def independence_group(self, url):
        from urllib.parse import urlparse

        from yt2wiki.factcheck.ddgs_provider import registrable_domain
        domain = registrable_domain(urlparse(url).hostname or "")
        if domain in {d.lower() for d in getattr(self, "primary_domains", ())}:
            return f"primary:{domain}"
        return domain


def _run(db, article_id, md, provider, **kw):
    _article(db, article_id, md)
    return runmod.run_factcheck(db, article_id, provider=provider, **kw)


FALSE_MD = ("# T\n\n## Facts\n\nDimorphos is larger than Didymos and hosts "
            "a deep liquid water ocean today.\n")

REFUTE_A = ("Observatory bulletin: Dimorphos is smaller than Didymos, a rubble "
            "moonlet with no ocean; the claim that Dimorphos is larger than "
            "Didymos and hosts a deep liquid water ocean is false.")
REFUTE_B = ("Planetary review: Didymos is the primary body and Dimorphos the "
            "smaller companion. No ocean has been detected; reports of a deep "
            "liquid water ocean on Dimorphos are refuted by density data.")


def _refuting_provider():
    return _P(
        hits={FALSE_MD.split("\n\n")[2].strip(): [
            ev.SearchHit(url="https://obs-one.example/bulletin", title="Bulletin"),
            ev.SearchHit(url="https://review-two.example/planets", title="Review")]},
        pages={"https://obs-one.example/bulletin": ev.FetchedPage(
            url="https://obs-one.example/bulletin", text=REFUTE_A),
               "https://review-two.example/planets": ev.FetchedPage(
            url="https://review-two.example/planets", text=REFUTE_B)})


# ------------------------------------------------------------------ P0 ----

def test_independent_refutation_without_verifier_is_not_corroborated(tmp_path):
    """Two independent pages share every entity yet explicitly refute the
    claim. Without semantic verification the run must not corroborate."""
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    report = _run(db, "art9-fc-p0a", FALSE_MD, _refuting_provider())
    claim = report.overlay.claims[0]
    assert claim.assessment == "insufficient_evidence"
    assert len(claim.evidence) == 2
    assert {e.relationship for e in claim.evidence} == {"context_only"}
    assert "no verifier" in claim.reason


def test_honest_verifier_contradicts_refuting_pages(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)

    def _chat(messages):
        import json as _json
        return _json.dumps({"relationship": "contradicts", "independence_group": "",
                            "reason": "Both pages explicitly refute the claim."})
    report = _run(db, "art9-fc-p0b", FALSE_MD, _refuting_provider(),
                  verifier=vf.verifier_from_chat(_chat))
    assert report.overlay.claims[0].assessment == "contradicted"


def test_misleading_but_authentic_quotation_never_corroborates(tmp_path):
    """The page quotes the exact claim inside a debunking ('Myth: X.
    Fact: not X'). The excerpt is authentic; the relationship is not
    support — and without a verifier nobody may claim otherwise."""
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nCold fusion powers homes in Utah since 1989 "
          "according to the segment.\n")
    page = ("Myth: cold fusion powers homes in Utah since 1989 according to "
            "the segment. Fact: no reproducible excess heat was ever shown; "
            "the claim is rejected by every replication attempt.")
    provider = _P(
        hits={md.split("\n\n")[2].strip(): [
            ev.SearchHit(url="https://debunk.example/myths", title="Debunked")]},
        pages={"https://debunk.example/myths": ev.FetchedPage(
            url="https://debunk.example/myths", text=page)})
    report = _run(db, "art9-fc-myth", md, provider)
    claim = report.overlay.claims[0]
    assert claim.assessment == "insufficient_evidence"
    assert all(e.relationship == "context_only" for e in claim.evidence)


def test_verifier_cannot_mint_trusted_groups():
    """A model proposing primary:evil must not promote the source."""
    from yt2wiki.factcheck.contracts import ClaimRecord, EvidenceRecord
    record = ClaimRecord(
        claim_id="c01", section_id="s", quote="X is Y and Z today here.",
        claim_kind="empirical")
    page = EvidenceRecord(url="https://evil.example/p", excerpt="X is Y and Z today here.",
                          locator="char 0-10", independence_group="",
                          content_hash="h")

    def _chat(messages):
        import json as _json
        return _json.dumps({"relationship": "supports",
                            "independence_group": "primary:evil.example",
                            "reason": "States it."})
    out = assess.assess_claim(record, [page],
                              verifier=vf.verifier_from_chat(_chat))
    assert out.evidence[0].independence_group == ""
    assert "primary:" not in out.evidence[0].independence_group

    # Provider-assigned groups stand even when the verifier proposes another.
    page2 = EvidenceRecord(url="https://obs-one.example/b", excerpt="X is Y and Z today here.",
                           locator="char 0-10", independence_group="obs-one.example",
                           content_hash="h")

    def _chat2(messages):
        import json as _json
        return _json.dumps({"relationship": "supports",
                            "independence_group": "wire.example",
                            "reason": "States it."})
    out2 = assess.assess_claim(record, [page2],
                               verifier=vf.verifier_from_chat(_chat2))
    assert out2.evidence[0].independence_group == "obs-one.example"


# ------------------------------------------------------- classification ----

@pytest.mark.parametrize("sentence, kind, checkable", [
    ("The bridge is made of steel and concrete today.", "empirical", True),
    ("The host says the bridge is made of steel today.", "empirical", True),
    ("This bridge is the finest structure ever built.", "normative_judgment", False),
    ("I love this design so much today.", "personal_preference", False),
    ("The presenter calls the design beautiful and inspiring.", "attributed_opinion", False),
    ("Avoid touching the contacts here.", "actionable_factual_premise", True),
    ("The rover will launch next year from the cape.", "prediction", False),
])
def test_classification_defaults(sentence, kind, checkable):
    got, ok = claims._classify(sentence)
    assert (got, ok) == (kind, checkable), sentence


def test_extraction_populates_attribution_and_provenance():
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n\n*Source 1:00–2:00 — "
          "[1:00](https://example.com/watch?v=x&t=60)*\n\n"
          'The host says "this span is 380 meters" proudly today.\n')
    cands = claims.extract_candidates(md)
    plain = next(c for c in cands if c.quote.startswith("The bridge span"))
    assert plain.attribution == "article_narration"
    assert plain.video_provenance == [{"start_s": 60.0, "end_s": 120.0}]
    quoted = next(c for c in cands if "proudly" in c.quote)
    assert quoted.attribution == "quoted_person"


# ------------------------------------------------------------- coverage ----

def test_coverage_counts_every_verdict_class(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n\nI love this bridge so much today.\n")
    report = _run(db, "art9-fc-cov", md, _P(hits={}, pages={}))
    cov = report.overlay.coverage
    by_assessment = cov["by_assessment"]
    assert cov["checked_claims"] == sum(by_assessment.values())
    assert by_assessment.get("insufficient_evidence", 0) >= 1
    assert by_assessment.get("not_applicable", 0) >= 1
    assert "not_checked" not in by_assessment  # absent when nothing failed


# --------------------------------------------------------------- net/persist

PAGE_HTML = (b"<html><head><title>Survey</title></head><body>"
               b"<p>The bridge span measures 380 meters in total length here.</p>"
               b"</body></html>")


@pytest.fixture()
def _local_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE_HTML)))
            self.end_headers()
            self.wfile.write(PAGE_HTML)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_dns_rebinding_cannot_steer_fetch(_local_server, monkeypatch):
    """Resolver returns loopback first, then a private address (rebind).
    The pinned request must still reach the verified target."""
    import socket as _socket

    from yt2wiki.factcheck import ddgs_provider as dd
    calls = {"n": 0}
    real = _socket.getaddrinfo

    def _flapping(host, port, *args, **kwargs):
        if host == "rebind.test":
            calls["n"] += 1
            if calls["n"] == 1:
                return [(2, 1, 6, "", ("127.0.0.1", port))]
            return [(2, 1, 6, "", ("10.9.9.9", port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", _flapping)
    import ipaddress as _ip

    def _ok(ip):
        return ip == "127.0.0.1" or _ip.ip_address(ip).is_global

    monkeypatch.setattr(dd, "_public_ip_ok", _ok)
    monkeypatch.setattr("yt2wiki.factcheck.ddgs_provider.safe_url",
                        lambda url: url.strip() or None)
    provider = dd.DDGSProvider()
    page = provider.fetch(f"http://rebind.test:{_local_server}/page")
    assert "span measures 380 meters" in page.text
    # Pre-resolve consulted the flapping resolver once; the pin then
    # intercepted every in-request lookup. Prove the averted attack: the
    # very next lookup for the same host is already poisoned.
    assert calls["n"] == 1
    from socket import getaddrinfo as _live_lookup
    assert _live_lookup("rebind.test", _local_server)[0][4][0] == "10.9.9.9"
    assert calls["n"] == 2


def test_redirect_to_private_target_blocked(monkeypatch):
    import socket as _socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from yt2wiki.factcheck import ddgs_provider as dd

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location",
                             "http://probe.internal-test.example/page")
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    real = _socket.getaddrinfo

    def _spoof(host, port_, *args, **kwargs):
        if host == "probe.internal-test.example":
            return [(2, 1, 6, "", ("10.9.9.9", port_))]
        return real(host, port_, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", _spoof)
    real_resolve = dd._resolve_public_ips

    def _resolve(host, port_):
        if host == "127.0.0.1":
            return ["127.0.0.1"]  # test server only; real check otherwise
        return real_resolve(host, port_)

    monkeypatch.setattr(dd, "_resolve_public_ips", _resolve)
    monkeypatch.setattr("yt2wiki.factcheck.ddgs_provider.safe_url",
                        lambda url: url.strip() or None)
    provider = dd.DDGSProvider()
    with pytest.raises(ev.SearchFailure) as exc:
        provider.fetch(f"http://127.0.0.1:{port}/go")
    assert exc.value.kind == "blocked"
    srv.shutdown()


def test_edit_during_run_yields_historical_overlay_no_sidecar(tmp_path):
    """Article edited mid-run: overlay binds the start revision, goes
    historical, and no sidecar ships."""
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    from yt2wiki.config import Settings
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    # NB: insert directly — the _run() helper would insert a second time.
    db.execute("INSERT OR IGNORE INTO sources(source_id, kind, created_at) "
               "VALUES('src-fc-x', 'upload', 1)")
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, status, "
        "version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("art9-fc-race", "src-fc-x", "T", md, "published", 1, 1.0, 1.0))
    adir = settings.paths.article_dir("art9-fc-race")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "article.md").write_text(md, encoding="utf-8")
    import zipfile as _zipfile
    with _zipfile.ZipFile(adir / "article.zip", "w",
                           _zipfile.ZIP_DEFLATED) as zf:
        zf.write(adir / "article.md", "article.md")

    class _Mutating(ev.MockProvider):
        def fetch(self, url):
            db.execute("UPDATE articles SET markdown=markdown || '\nEdited.' "
                       "WHERE article_id='art9-fc-race'")
            return super().fetch(url)

    provider = _Mutating(
        hits={md.split("\n\n")[2].strip(): [
            ev.SearchHit(url="https://survey.example/report", title="S")]},
        pages={"https://survey.example/report": ev.FetchedPage(
            url="https://survey.example/report",
            text="survey: the bridge span measures 380 meters in total length "
                 "across the river valley, confirmed.")})
    report = runmod.run_factcheck(db, "art9-fc-race", provider=provider)
    assert store.current_overlay(db, "art9-fc-race") is None
    assert len(store.overlay_history(db, "art9-fc-race")) == 1
    assert store.refresh_export_sidecar(db, settings.paths, "art9-fc-race",
                                        report.overlay) is None
    with _zipfile.ZipFile(adir / "article.zip", "r") as zin:
        assert "factcheck.md" not in zin.namelist()


def test_stale_sidecar_never_ships_through_export(tmp_path):
    """A stale factcheck.md on disk with no current overlay is stripped,
    never shipped."""
    from tests.fixtures import in_memory_db
    settings, db = in_memory_db(tmp_path)
    _article(db, "art9-fc-stalezip", "# T\n\n## F\n\nNothing checkable here.\n")
    adir = settings.paths.article_dir("art9-fc-stalezip")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "article.md").write_text("# T\n", encoding="utf-8")
    (adir / "factcheck.md").write_text("# stale\n", encoding="utf-8")
    import zipfile as _zipfile
    with _zipfile.ZipFile(adir / "article.zip", "w",
                           _zipfile.ZIP_DEFLATED) as zf:
        zf.write(adir / "article.md", "article.md")
        zf.write(adir / "factcheck.md", "factcheck.md")
    store.ship_sidecar_in_zip(settings.paths, "art9-fc-stalezip", None)
    assert not (adir / "factcheck.md").exists()
    with _zipfile.ZipFile(adir / "article.zip", "r") as zin:
        names = zin.namelist()
    assert names == ["article.md"]


def test_stale_overlay_renders_non_endorsement(tmp_path):
    from tests.fixtures import in_memory_db
    from yt2wiki.web import views
    _, db = in_memory_db(tmp_path)
    md = "# T\n\n## Facts\n\nThe widget costs 19 dollars at retail today here.\n"
    _run(db, "art9-fc-stalepanel", md, _P(hits={}, pages={}))
    assert views.factcheck_context(db, "art9-fc-stalepanel")["run_status"] in (
        "complete", "partial", "unavailable")
    db.execute("UPDATE articles SET markdown=markdown || '\nMore.' "
               "WHERE article_id='art9-fc-stalepanel'")
    ctx = views.factcheck_context(db, "art9-fc-stalepanel")
    assert ctx is not None and ctx["run_status"] == "stale"
    assert "not current" in str(ctx["panel_html"])
    assert "nothing here has been verified" in str(ctx["panel_html"]).lower()
