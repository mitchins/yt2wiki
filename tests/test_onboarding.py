"""First-run onboarding: guided save-test, qualification lifecycle, readiness,
submission gating and login-gated add pages. Offline (httpx mocked)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import (  # noqa: E402
    TEST_OPERATOR_PASSWORD,
    configure_ready,
    login_client,
    post_json,
)
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.web import app as webapp  # noqa: E402
from yt2wiki.web import inference as inf  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402


@pytest.fixture()
def onboarded(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    settings = Settings.load(data_dir=tmp_path / "data", profile="compact", device_index=None)
    Database(settings.paths.db_path)
    app = create_app(settings)
    client, csrf = login_client(app)
    return app, settings, client, csrf


def _mock_provider(monkeypatch, *, writer=True, vision=True):
    """httpx mock: /models lists one model; probes succeed per flags."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if path.endswith("/chat/completions"):
            import json as _json
            body = _json.loads(request.content.decode() or "{}")
            msgs = _json.dumps(body.get("messages", []))
            if "V9" in msgs or "image" in msgs.lower():
                if not vision:
                    return httpx.Response(200, json={"choices": [{"message": {"content": "junk"}}]})
                return httpx.Response(200, json={"choices": [{"message": {"content":
                    '{"scores": [{"idx": 1, "relevance": 2, "clarity": 2, "instructional": 2, "payoff": 2, "veto": false}], "decision": "KEEP", "winner": 1, "explanation": "clear demo"}'}}]})
            if not writer:
                return httpx.Response(200, json={"choices": [{"message": {"content": "junk"}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_get(url, **kw):
        with orig_client(transport=transport) as c:
            return c.get(url, **kw)

    def patched_post(url, **kw):
        with orig_client(transport=transport) as c:
            return c.post(url, **kw)

    monkeypatch.setattr(httpx, "get", patched_get)
    monkeypatch.setattr(httpx, "post", patched_post)


def test_guided_save_test_assigns_qualified_roles(onboarded, monkeypatch):
    _app, settings, client, csrf = onboarded
    _mock_provider(monkeypatch)
    r = post_json(client, csrf, "/api/settings/inference/save-test",
                  {"name": "hal", "url": "http://hal:8731/v1", "model": "m", "api_key_env": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["roles"] == {"writer": "hal", "vision": "hal"}
    assert body["result"]["qualified_for_writer"] is True
    assert body["result"]["qualified_for_vision"] is True
    cfg = inf.load(settings.paths)
    assert cfg["roles"]["writer"] == "hal"
    assert cfg["roles"]["vision"] == "hal"
    # Readiness reflects the fresh qualification.
    ready = client.get("/api/settings/readiness").json()
    assert ready["writer"]["state"] == "Ready"
    assert ready["vision"]["state"] == "Ready"


def test_guided_save_test_failure_assigns_nothing(onboarded, monkeypatch):
    _app, settings, client, csrf = onboarded
    _mock_provider(monkeypatch, writer=False, vision=False)
    r = post_json(client, csrf, "/api/settings/inference/save-test",
                  {"name": "weak", "url": "http://weak:8000/v1", "model": "m"})
    body = r.json()
    assert body["ok"] is True  # reachable...
    assert body["roles"] == {}  # ...but qualified for nothing
    assert inf.load(settings.paths)["roles"].get("writer") in (None, "")


def test_identity_change_invalidates_old_pass(onboarded, monkeypatch):
    _app, settings, client, csrf = onboarded
    _mock_provider(monkeypatch)
    post_json(client, csrf, "/api/settings/inference/save-test",
              {"name": "hal", "url": "http://hal:8731/v1", "model": "m"})
    assert client.get("/api/settings/readiness").json()["writer"]["state"] == "Ready"
    # Change the URL (same name): the old PASS must not be reused.
    post_json(client, csrf, "/api/settings/inference/save-test",
              {"name": "hal", "url": "http://other:9999/v1", "model": "m"})
    st = inf.provider_status(settings.paths, "hal")
    # New identity was tested against the same mock (reachable+qualified).
    assert st.get("url") == "http://other:9999/v1"
    # Now break the endpoint (unreachable) and re-test: failure recorded.
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    post_json(client, csrf, "/api/settings/inference/save-test",
              {"name": "hal", "url": "http://other:9999/v1", "model": "m"})
    ready = client.get("/api/settings/readiness").json()
    assert ready["writer"]["state"] in ("Failed", "Needs setup")


def test_asr_env_fallback_readiness(onboarded, monkeypatch):
    # Superseded by test_env_asr_url_alone_is_not_ready: an environment URL
    # alone is not Ready, only a matching validated test is.
    _app, settings, client, csrf = onboarded
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://asr:9000")
    ready = client.get("/api/settings/readiness").json()
    assert ready["asr"]["state"] == "Needs setup"
    monkeypatch.delenv("YT2WIKI_ASR_URL")
    ready = client.get("/api/settings/readiness").json()
    assert ready["asr"]["state"] == "Needs setup"


def test_submission_refused_without_services(onboarded, monkeypatch):

    _app, _settings, client, csrf = onboarded
    monkeypatch.setattr(webapp.Service, "preview_url", lambda self, url: {"ok": True})
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert "Settings" in r.json()["detail"]
    # Configure only writing: transcription still blocks.
    configure_ready(_settings, writer=True, asr=False)
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert "ranscription" in r.json()["detail"]


def test_submission_allowed_when_ready_or_captioned(onboarded, monkeypatch):

    _app, settings, client, csrf = onboarded
    configure_ready(settings)
    monkeypatch.setattr(webapp.Service, "preview_url",
                        lambda self, url: {"ok": True, "has_manual_captions": False})
    db = Database(settings.paths.db_path)
    import time as _time
    db.execute("INSERT OR IGNORE INTO sources(source_id, kind, title, created_at) VALUES('yt-cap','youtube','T',?)",
               (_time.time(),))
    monkeypatch.setattr(webapp.Service, "create_youtube_source",
                        lambda self, url: "yt-cap")
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    # Captions-only video passes the transcription gate without ASR.
    _app2 = create_app(settings)
    client2, csrf2 = login_client(_app2)
    monkeypatch.setattr(webapp.Service, "preview_url",
                        lambda self, url: {"ok": True, "has_manual_captions": True})
    from tests.conftest import configure_ready as _cr
    alt = settings.paths.root.parent / "data2"
    alt_settings = Settings.load(data_dir=str(alt), profile="compact", device_index=None)
    alt_db = Database(alt_settings.paths.db_path)
    _cr(alt_settings, writer=True, asr=False)
    alt_db.execute("INSERT OR IGNORE INTO sources(source_id, kind, title, created_at) VALUES('yt-cap2','youtube','T',?)",
                   (_time.time(),))
    monkeypatch.setattr(webapp.Service, "create_youtube_source",
                        lambda self, url: "yt-cap2")
    app3 = create_app(alt_settings)
    client3, csrf3 = login_client(app3)
    r = client3.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                     headers={"X-CSRF-Token": csrf3})
    assert r.status_code == 200, r.text


def test_add_pages_require_login_and_show_login_box(tmp_path, monkeypatch):

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    settings = Settings.load(data_dir=tmp_path / "addpg", profile="compact", device_index=None)
    Database(settings.paths.db_path)
    app = create_app(settings)
    anon = TestClient(app)
    assert "Operator login" in anon.get("/add").text
    assert "action=\"/add/url\"" not in anon.get("/add").text
    client, csrf = login_client(app)
    page = client.get("/add").text
    assert "Operator login" not in page
    assert "csrf_token" in page


def test_settings_shows_readiness_and_no_remote_banner(onboarded):
    _app, _settings, client, _csrf = onboarded
    text = client.get("/settings").text
    assert "Readiness" in text
    assert "Operator only" not in text
    assert "docker exec" not in text
    assert "YT2WIKI_" in text  # key-variable prefix guidance present


def test_submission_pages_have_no_gpu_selector(tmp_path, monkeypatch):
    """Normal video submission offers no hardware profile choice."""
    from tests.conftest import login_client

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    settings = Settings.load(data_dir=tmp_path / "nosel", profile="compact", device_index=None)
    Database(settings.paths.db_path)
    app = create_app(settings)
    anon_html = TestClient(app).get("/add").text
    assert "profile_picker" not in anon_html
    assert "Fits an 8 GB GPU" not in anon_html
    assert "Choose the size that matches the GPU" not in anon_html
    client, _csrf = login_client(app)
    authed = client.get("/add").text
    assert "csrf_token" in authed
    assert "profile_picker" not in authed
    home = client.get("/").text
    assert "profile_picker" not in home
    assert "all on this machine" not in home


def test_asr_word_timestamp_contract(tmp_path, monkeypatch):
    """The /asr contract test: multipart audio_file + query, words preserved."""
    from unittest import mock

    from yt2wiki.backends import asr_http as _m
    from yt2wiki.backends.asr_http import parse

    wav = tmp_path / "clip.wav"
    _m.write_sine_wav(wav, seconds=2.0)
    seen: dict = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            def _word(s, e, w):
                return {"start": s, "end": e, "word": w, "probability": 0.9}
            return {
                "language": "en", "language_probability": 0.99,
                "text": "hello world",
                "segments": [{"id": 0, "seek": 0, "start": 0.0, "end": 1.5,
                              "text": "hello world", "tokens": [1, 2],
                              "avg_logprob": -0.2, "no_speech_prob": 0.01,
                              "compression_ratio": 1.1, "temperature": 0.0,
                              "words": [_word(0.1, 0.5, "hello"),
                                        _word(0.6, 1.2, "world")]}]}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["files"] = kwargs.get("files")
        return Resp()

    ctx = mock.MagicMock()
    ctx.__enter__.return_value.post = fake_post
    monkeypatch.setattr("httpx.Client", lambda *a, **k: ctx)
    data = _m.post_asr("http://asr:9000", wav,
                       query=_m.build_query(language="en", word_timestamps=True,
                                            vad_filter=True), timeout=5.0)
    assert seen["url"].startswith("http://asr:9000/asr?")
    assert "audio_file" in seen["files"]
    t = parse(data, duration_s=2.0, require_words=True, server="s")
    assert [w.word for w in t.segments[0].words] == ["hello", "world"]
    assert t.segments[0].words[0].start == 0.1
    assert t.segments[0].text == "hello world"


def test_vision_required_before_queuing(onboarded, monkeypatch):
    """V9 needs image judging too: writer+transcription alone must not queue."""
    from yt2wiki.web import app as webapp
    from yt2wiki.web import inference as inf

    _app, settings, client, csrf = onboarded
    monkeypatch.setattr(webapp.Service, "preview_url", lambda self, url: {"ok": True})
    configure_ready(settings, writer=True, asr=True)
    # Drop the vision role: writer + transcription are Ready, images are not.
    cfg = inf.load(settings.paths)
    cfg["roles"].pop("vision", None)
    inf._write_json(settings.paths, "config", cfg)
    assert client.get("/api/settings/readiness").json()["vision"]["state"] == "Needs setup"
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert "judging" in r.json()["detail"]
    # Assigned but unqualified vision also blocks.
    cfg["roles"]["vision"] = "llm"
    inf._write_json(settings.paths, "config", cfg)
    st = inf.load_status(settings.paths)
    st["llm"] = {**st.get("llm", {}), "qualified_for_vision": False}
    inf._write_json(settings.paths, "status", st)
    assert client.get("/api/settings/readiness").json()["vision"]["state"] == "Failed"
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409


def test_env_asr_url_alone_is_not_ready(onboarded, monkeypatch):
    """An environment ASR URL is not Ready and admits no job until validated."""
    from yt2wiki.web import app as webapp
    from yt2wiki.web import inference as inf

    _app, settings, client, csrf = onboarded
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://asr:9000")
    monkeypatch.setattr(webapp.Service, "preview_url", lambda self, url: {"ok": True})
    ready = client.get("/api/settings/readiness").json()
    assert ready["asr"]["state"] == "Needs setup"
    r = client.post("/api/sources/url", json={"url": "https://youtu.be/abc"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    # A stored, identity-matched qualification for that exact URL counts —
    # cached validation, no probe on page load.
    inf.save(settings.paths, {"providers": [{"name": "whisper",
                                              "url": "http://asr:9000",
                                              "model": "large-v3",
                                              "api_key_env": ""}],
                              "roles": {}})
    inf._write_json(settings.paths, "status",
                    {"whisper": {"ok": True, "url": "http://asr:9000",
                                 "model": "large-v3", "api_key_env": "",
                                 "qualified_for_asr": True,
                                 "checked_at": 1700000000.0}})
    ready = client.get("/api/settings/readiness").json()
    assert ready["asr"]["state"] == "Ready"
    assert "validated test" in ready["asr"]["detail"]
    # Changing the environment URL invalidates the match.
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://other:9000")
    assert client.get("/api/settings/readiness").json()["asr"]["state"] == "Needs setup"


def test_save_test_rejects_concurrent_identity_change(onboarded, monkeypatch):
    """A mid-test endpoint edit fails loudly (409); the stale PASS is dropped."""
    from yt2wiki.web import inference as inf

    _app, settings, client, csrf = onboarded

    def _racing_test(url, api_key_env="", *, model="", timeout=6.0):
        # Simulate a concurrent edit landing while the test was in flight.
        inf.save_one(settings.paths, {"name": "hal", "url": "http://other:9999/v1",
                                      "model": "m", "api_key_env": ""})
        return {"ok": True, "kind": "llm", "models": ["m"], "latency_ms": 1,
                "checked_at": 1700000000.0, "url": url,
                "qualified_for_writer": True, "qualified_for_vision": True,
                "qualified_for_asr": False}

    monkeypatch.setattr(inf, "test_endpoint", _racing_test)
    r = post_json(client, csrf, "/api/settings/inference/save-test",
                  {"name": "hal", "url": "http://hal:8731/v1", "model": "m",
                   "api_key_env": ""})
    assert r.status_code == 409
    assert "changed while the test" in r.json()["detail"]
    # The concurrent edit won the record, but no roles came from the stale test.
    assert inf.load(settings.paths)["providers"]["hal"]["url"] == "http://other:9999/v1"
    assert inf.load(settings.paths)["roles"] == {}
    assert inf.load_status(settings.paths).get("hal") is None
