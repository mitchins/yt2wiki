"""FACTCHECK-01 follow-up: proxy gap, syndication, evidence-only reporting,
backend bounds.

Narrow scope: no core-writer changes, no paid providers.
"""

from __future__ import annotations

import threading

import pytest

from yt2wiki.factcheck import evidence as ev
from yt2wiki.factcheck import run as runmod
from yt2wiki.factcheck import verifier as vf


def _article(db, article_id, md):
    db.execute("INSERT OR IGNORE INTO sources(source_id, kind, created_at) "
               "VALUES('src-fc-f', 'upload', 1)")
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, status, "
        "version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (article_id, "src-fc-f", "T", md, "published", 1, 1.0, 1.0))


class _P(ev.MockProvider):
    def independence_group(self, url):
        from urllib.parse import urlparse

        from yt2wiki.factcheck.ddgs_provider import registrable_domain
        return registrable_domain(urlparse(url).hostname or "")


# ---------------------------------------------------------------- proxy ----

PAGE = (b"<html><head><title>Survey</title></head><body>"
        b"<p>The bridge span measures 380 meters in total length here.</p>"
        b"</body></html>")


@pytest.fixture()
def _page_server():
    import threading as _th
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture()
def _proxy_recorder():
    import threading as _th
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(400)
            self.end_headers()

        def do_CONNECT(self):
            seen.append(self.path)
            self.send_response(400)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()


def test_evidence_fetch_ignores_environment_proxies(_page_server, _proxy_recorder,
                                                    monkeypatch):
    """HTTP(S)_PROXY/ALL_PROXY must neither steer nor observe evidence fetches."""
    from yt2wiki.factcheck import ddgs_provider as dd
    proxy_port, seen = _proxy_recorder
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, proxy_url)
    monkeypatch.setattr(dd, "_resolve_public_ips",
                        lambda host, port: ["127.0.0.1"])
    monkeypatch.setattr("yt2wiki.factcheck.ddgs_provider.safe_url",
                        lambda url: url.strip() or None)
    provider = dd.DDGSProvider()
    page = provider.fetch(f"http://127.0.0.1:{_page_server}/page")
    assert "span measures 380 meters" in page.text
    assert seen == [], f"proxy observed evidence traffic: {seen}"


def test_proxy_control_proves_adversarial_setup(_page_server, _proxy_recorder,
                                                monkeypatch):
    """Control: a default-trust client DOES use the environment proxy."""
    import httpx
    proxy_port, seen = _proxy_recorder
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
    try:
        with httpx.Client(timeout=5) as client:
            client.get(f"http://127.0.0.1:{_page_server}/page")
    except Exception:  # noqa: BLE001 — proxy may refuse CONNECT outright
        pass
    assert seen, "control proxy received nothing; test setup is not adversarial"


def test_concurrent_fetches_stay_correct(_page_server, monkeypatch):
    """The process-global DNS pin serializes safely under threads."""
    from yt2wiki.factcheck import ddgs_provider as dd
    monkeypatch.setattr(dd, "_resolve_public_ips",
                        lambda host, port: ["127.0.0.1"])
    monkeypatch.setattr("yt2wiki.factcheck.ddgs_provider.safe_url",
                        lambda url: url.strip() or None)
    provider = dd.DDGSProvider(cache=dd.FactcheckCache(directory=None))
    results: dict[str, str] = {}
    errors: list[str] = []

    def _work(name):
        try:
            page = provider.fetch(f"http://127.0.0.1:{_page_server}/{name}")
            results[name] = page.text
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    threads = [threading.Thread(target=_work, args=(f"p{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert len(results) == 4
    assert all("span measures 380 meters" in text for text in results.values())


# ------------------------------------------------------------ syndication ----

def _supports_stub():
    import json as _json

    def _chat(messages):
        return _json.dumps({"relationship": "supports", "independence_group": "",
                            "reason": "Passage states the claim."})
    return vf.verifier_from_chat(_chat)


def test_syndicated_copies_do_not_corroborate(tmp_path):
    """Two domains, byte-identical report text: one origin, not two
    corroborators — even with a verifier assessing support."""
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    claim = md.split("\n\n")[2].strip()
    text = ("Wire report: the bridge span measures 380 meters in total length "
            "across the river valley, confirmed today by surveyors.")
    provider = _P(
        hits={claim: [ev.SearchHit(url="https://a-wire.example/1", title="A"),
                      ev.SearchHit(url="https://b-wire.example/2", title="B")]},
        pages={"https://a-wire.example/1": ev.FetchedPage(
            url="https://a-wire.example/1", text=text),
               "https://b-wire.example/2": ev.FetchedPage(
            url="https://b-wire.example/2", text=text)})
    _article(db, "art9-fc-synd2", md)
    report = runmod.run_factcheck(db, "art9-fc-synd2", provider=provider,
                                  verifier=_supports_stub())
    claim_rec = report.overlay.claims[0]
    assert claim_rec.assessment == "insufficient_evidence"
    assert {e.independence_group for e in claim_rec.evidence} == {"a-wire.example"}


def test_distinct_reports_still_corroborate(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    claim = md.split("\n\n")[2].strip()
    provider = _P(
        hits={claim: [ev.SearchHit(url="https://a-wire.example/1", title="A"),
                      ev.SearchHit(url="https://b-paper.example/2", title="B")]},
        pages={"https://a-wire.example/1": ev.FetchedPage(
            url="https://a-wire.example/1",
            text="Wire report: the bridge span measures 380 meters in total length "
                 "across the river valley, confirmed today."),
               "https://b-paper.example/2": ev.FetchedPage(
            url="https://b-paper.example/2",
            text="Gazette surveyors independently measured the bridge span at 380 "
                 "meters in total length across the river valley yesterday.")})
    _article(db, "art9-fc-legit", md)
    report = runmod.run_factcheck(db, "art9-fc-legit", provider=provider,
                                  verifier=_supports_stub())
    assert report.overlay.claims[0].assessment == "corroborated"


# ------------------------------------------------- evidence-only reporting ----

def test_cli_reports_collection_without_verdicts(tmp_path, capsys):
    from tests.fixtures import in_memory_db
    from yt2wiki.cli import main
    settings, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    _article(db, "art9-fc-eo", md)
    rc = main(["--data-dir", str(tmp_path), "factcheck", "run", "art9-fc-eo",
               "--provider", "mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "evidence collection only" in out
    assert "no categorical factual verdicts" in out
    assert "retrieved passages" in out
    assert "Corroborated" not in out


def test_panel_and_sidecar_disclaim_unassessed_collection(tmp_path):
    from tests.fixtures import in_memory_db
    from yt2wiki.factcheck import panel
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    _article(db, "art9-fc-eop", md)
    report = runmod.run_factcheck(db, "art9-fc-eop", provider=_P(hits={}, pages={}))
    assert report.overlay.verifier["mode"] == "collection"
    html = panel.render_panel_html(report.overlay)
    assert "No semantic assessment was run" in html
    assert "Collection is not fact-checking" in html
    md_side = panel.render_factcheck_md(report.overlay)
    assert "NOTE: No semantic assessment was run" in md_side


def test_show_reports_mode_and_breakdown(tmp_path, capsys):
    from tests.fixtures import in_memory_db
    from yt2wiki.cli import main
    settings, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley.\n")
    _article(db, "art9-fc-eos", md)
    assert main(["--data-dir", str(tmp_path), "factcheck", "run", "art9-fc-eos",
                 "--provider", "mock"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "factcheck", "show",
                 "art9-fc-eos"]) == 0
    out = capsys.readouterr().out
    assert "mode     : collection" in out
    assert "with retrieved passages" in out
    assert "insufficient_evidence" in out


# ------------------------------------------------------- backend bounds ----

def test_verifier_budget_exhaustion_yields_partial(tmp_path):
    from tests.fixtures import in_memory_db
    _, db = in_memory_db(tmp_path)
    md = ("# T\n\n## Facts\n\nThe bridge span measures 380 meters in total "
          "length across the river valley here today.\n")
    claim = md.split("\n\n")[2].strip()
    text = ("Report: the bridge span measures 380 meters in total length across "
            "the river valley here today, confirmed by survey.")
    provider = _P(
        hits={claim: [ev.SearchHit(url="https://a-one.example/1", title="A"),
                      ev.SearchHit(url="https://b-two.example/2", title="B")]},
        pages={"https://a-one.example/1": ev.FetchedPage(
            url="https://a-one.example/1", text=text),
               "https://b-two.example/2": ev.FetchedPage(
            url="https://b-two.example/2", text=text + " Second outlet version.")})
    _article(db, "art9-fc-budget", md)
    full = runmod.run_factcheck(db, "art9-fc-budget", provider=provider,
                                verifier=_supports_stub())
    assert full.verifier_calls >= 1
    assert full.verifier_budget == 40
    assert full.overlay.verifier["max_tokens"] == 256
    assert full.overlay.verifier["timeout_s"] == 90.0
    assert full.overlay.verifier["mode"] == "assessed"
    # Force exhaustion with a tiny budget: remaining evidence unassessed,
    # honest partial — never invented verdicts.
    starved = runmod.run_factcheck(db, "art9-fc-budget", provider=provider,
                                   verifier=_supports_stub(),
                                   max_verifier_calls=1)
    assert starved.verifier_calls == 1
    assert starved.verifier_exhausted is True
    assert starved.overlay.run_status == "partial"
    claim = starved.overlay.claims[0]
    assert any(e.relationship == "context_only" for e in claim.evidence)
    assert "budget exhausted" in claim.reason


def test_verifier_timeout_is_not_a_verdict():
    def _slow(messages):
        import time as _t
        _t.sleep(5)
        return "{}"
    verify = vf.verifier_from_chat(_slow, timeout_s=0.2)
    rel, group, reason = verify("some claim here today", "a sufficiently long excerpt here",
                                "https://example.com/x")
    assert rel == "context_only"
    assert "TimeoutError" in reason


def test_verifier_records_limits_and_backend_caps_tokens():
    seen: dict = {}

    def _chat(messages, max_new_tokens=None):
        seen["max_new_tokens"] = max_new_tokens
        import json as _json
        return _json.dumps({"relationship": "context_only", "independence_group": "",
                            "reason": "No support."})
    verify = vf.verifier_from_chat(_chat)
    assert verify.limits == {"max_tokens": 256, "timeout_s": 90.0}
    verify("some claim here today", "a sufficiently long excerpt here for gates",
           "https://example.com/x")
    assert seen["max_new_tokens"] == 256
