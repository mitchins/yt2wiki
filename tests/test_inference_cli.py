"""Operator-console inference management (B3/Docker): CLI works, browser is read-only.

- `yt2wiki inference show/save/test` manages providers from the operator
  console (docker exec), where the web API refuses non-localhost peers.
- The settings page served to a gateway peer (Docker published port, SSH
  tunnel, LAN) carries the operator-only notice and disables mutation;
  the localhost peer gets no banner and can save.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.paths import Paths
from yt2wiki.web import inference as inf
from yt2wiki.web.app import create_app


def _app(tmp_path, name):
    settings = Settings.load(data_dir=tmp_path / name, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return create_app(settings), settings, db


def test_unauthenticated_settings_mutations_denied(tmp_path, monkeypatch):
    """Mutations without an operator session are denied; login enables them."""
    from tests.conftest import TEST_OPERATOR_PASSWORD, login_client
    from yt2wiki.web import inference as _inf

    app, settings, _db = _app(tmp_path, "authgw")
    anon = TestClient(app)
    # Fail closed with no password configured.
    monkeypatch.delenv("YT2WIKI_OPERATOR_PASSWORD", raising=False)
    r = anon.post("/api/settings/inference", json={"providers": [], "roles": {}})
    assert r.status_code == 403
    assert "YT2WIKI_OPERATOR_PASSWORD" in r.json()["detail"]
    # Login required once a password exists.
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    r = anon.post("/api/settings/inference", json={"providers": [], "roles": {}})
    assert r.status_code == 401
    client, csrf = login_client(app)
    r = client.post("/api/settings/inference", headers={"X-CSRF-Token": csrf},
                    json={"providers": [{"name": "lan", "url": "http://10.0.0.2:8000/v1"}],
                          "roles": {}})
    assert r.status_code == 200, r.text
    assert _inf.load(settings.paths)["providers"]["lan"]["url"] == "http://10.0.0.2:8000/v1"


def test_gateway_save_and_test_need_session_not_peer(tmp_path, monkeypatch):
    """Gateway peers are treated exactly like anyone else: session decides."""
    from tests.conftest import TEST_OPERATOR_PASSWORD, login_client

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    app, _settings, _db = _app(tmp_path, "gw")
    gateway = TestClient(app, client=("172.18.0.1", 50000))
    payload = {"providers": [{"name": "a", "url": "http://a.test/v1"}], "roles": {}}

    # Forged loopback headers grant nothing without a session.
    r = gateway.post("/api/settings/inference",
                     headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
                     json=payload)
    assert r.status_code == 401, r.text
    # Same gateway peer with a session cookie + CSRF: allowed.
    client, csrf = login_client(app)
    gw_authed = TestClient(app, client=("172.18.0.1", 50000))
    gw_authed.cookies.update(client.cookies)
    r = gw_authed.post("/api/settings/inference", headers={"X-CSRF-Token": csrf}, json=payload)
    assert r.status_code == 200, r.text
    r = gw_authed.post("/api/settings/inference/test",
                       headers={"X-CSRF-Token": csrf},
                       json={"url": "http://127.0.0.1:9/v1", "name": "a", "model": ""})
    assert r.status_code == 200
    assert r.json()["ok"] is False  # unreachable, but authorized


def test_settings_page_shows_login_state(tmp_path, monkeypatch):
    """Logged-out viewers get a compact login box; logged-in viewers get forms."""
    from tests.conftest import TEST_OPERATOR_PASSWORD, login_client

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    app, _settings, _db = _app(tmp_path, "banner")
    gateway = TestClient(app, client=("172.18.0.1", 50000))
    r = gateway.get("/settings")
    assert r.status_code == 200
    assert "Operator login" in r.text
    assert "data-login" in r.text
    assert "Operator only" not in r.text
    assert "docker exec" not in r.text
    client, _csrf = login_client(app)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Logged in as operator" in r.text


def _run_cli(*argv):
    from yt2wiki.cli import main

    return main(list(argv))


def test_inference_cli_show_save_roundtrip(tmp_path, capsys):
    data = tmp_path / "cli-inf"
    assert _run_cli("--data-dir", str(data), "inference", "show") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["providers"] == {}
    assert out["roles"] == {}

    payload = {
        "providers": [
            {"name": "lan", "url": "http://192.168.1.14:8000/v1",
             "model": "qwen", "api_key_env": "YT2WIKI_LAN_KEY"}
        ],
        "roles": {"writer": "lan"},
    }
    (data / "config").mkdir(parents=True, exist_ok=True)
    payload_file = data / "config" / "payload.json"
    payload_file.write_text(json.dumps(payload))
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(payload_file)) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["providers"]["lan"]["url"] == "http://192.168.1.14:8000/v1"

    # Stored config keeps the env var name; the secret is never written
    raw = json.loads((data / "config" / "inference.json").read_text())
    assert raw["providers"]["lan"]["api_key_env"] == "YT2WIKI_LAN_KEY"
    assert "YT2WIKI_LAN_KEY" in json.dumps(raw)

    assert _run_cli("--data-dir", str(data), "inference", "show") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"]["writer"] == "lan"


def test_read_payload_file_validates(tmp_path):
    from yt2wiki.config import Settings as _Settings
    from yt2wiki.web import inference as inf2

    settings = _Settings.load(data_dir=tmp_path / "payload-home", profile="compact",
                              device_index=None)
    cfgdir = settings.paths.root / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    good = cfgdir / "good.json"
    good.write_text(json.dumps({"providers": [], "roles": {}}))
    assert inf2.read_payload_file(settings.paths, good) == {"providers": [], "roles": {}}
    with pytest.raises(Exception, match="regular file inside"):
        inf2.read_payload_file(settings.paths, cfgdir / "absent.json")
    bad = cfgdir / "bad.json"
    bad.write_text("not json{")
    with pytest.raises(Exception, match="cannot read payload file"):
        inf2.read_payload_file(settings.paths, bad)
    lst = cfgdir / "list.json"
    lst.write_text("[1, 2]")
    with pytest.raises(Exception, match="must be a JSON object"):
        inf2.read_payload_file(settings.paths, lst)
    with pytest.raises(Exception, match="regular file inside"):
        inf2.read_payload_file(settings.paths, tmp_path)  # outside the config dir
    # Symlink escapes are resolved and refused
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"providers": [], "roles": {}}))
    link = cfgdir / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(Exception, match="regular file inside"):
            inf2.read_payload_file(settings.paths, link)


def test_validate_rejects_malformed_shapes():
    from yt2wiki.web import inference as inf2

    for bad in (["providers"], "providers", 42, None):
        with pytest.raises(Exception, match="[Cc]onfiguration|providers"):
            inf2.validate(bad)  # type: ignore[arg-type]
    for providers in ("nope", 42):
        with pytest.raises(Exception, match="providers"):
            inf2.validate({"providers": providers, "roles": {}})
    with pytest.raises(Exception, match="[Ee]ntry|object"):
        inf2.validate({"providers": [["name"]], "roles": {}})
    with pytest.raises(Exception, match="[Ee]ntry|object"):
        inf2.validate({"providers": {"a": "not-a-dict"}, "roles": {}})
    for entry in ("name-only", None, 42, ["n"]):
        with pytest.raises(Exception, match="[Ee]ntry|object"):
            inf2.validate({"providers": [entry], "roles": {}})
    for roles in (["writer"], "mock", 42):
        with pytest.raises(Exception, match="[Rr]oles"):
            inf2.validate({"providers": [], "roles": roles})
    # Valid shapes still normalise (list and dict provider forms)
    good = inf2.validate({
        "providers": [{"name": "a", "url": "http://a.test/v1"}],
        "roles": {"writer": "a"},
    })
    assert good["roles"]["writer"] == "a"
    good = inf2.validate({
        "providers": {"b": {"url": "http://b.test/v1"}},
        "roles": {},
    })
    assert good["providers"]["b"]["url"] == "http://b.test/v1"


def test_inference_cli_save_rejects_invalid(tmp_path, capsys):
    data = tmp_path / "cli-bad"
    (data / "config").mkdir(parents=True, exist_ok=True)
    bad = data / "config" / "bad.json"
    bad.write_text(json.dumps({"providers": [{"name": "x", "url": "not-a-url"}], "roles": {}}))
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(bad)) == 2
    missing = data / "config" / "missing.json"
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(missing)) == 2
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"providers": [], "roles": {}}))
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(outside)) == 2
    assert _run_cli("--data-dir", str(data), "inference") == 2


def test_inference_cli_test_qualifies_and_records(tmp_path, capsys, monkeypatch):
    import httpx

    data = tmp_path / "cli-test"
    (data / "config").mkdir(parents=True, exist_ok=True)
    payload_file = data / "config" / "p.json"
    payload_file.write_text(json.dumps({
        "providers": [{"name": "mock", "url": "http://mock.test/v1",
                       "model": "m", "api_key_env": "YT2WIKI_MOCK_KEY"}],
        "roles": {},
    }))
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(payload_file)) == 0
    capsys.readouterr()
    monkeypatch.setenv("YT2WIKI_MOCK_KEY", "mock-secret-inside-container")

    seen: list[dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        body = json.loads(request.content.decode()) if request.content else {}
        if "image_url" in json.dumps(body):
            content = json.dumps(
                {"scores": [{"frame_id": "f1", "score": 8, "reason": "clear"},
                            {"frame_id": "f2", "score": 3, "reason": "blur"}],
                 "decision": "KEEP", "reason": "first is better"})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        content = json.dumps({"ok": True, "text": "hello"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    # NOTE: only httpx.get/post are patched. Patching httpx.Client too would
    # nest wrappers on a second patch within one test, pinning the first
    # transport (the CLI path under test uses get/post only).
    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_get(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr("httpx.get", patched_get)

    def patched_post(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr("httpx.post", patched_post)

    rc = _run_cli("--data-dir", str(data), "inference", "test", "--name", "mock")
    result = json.loads(capsys.readouterr().out)
    assert rc == 0, result
    assert result["ok"] is True
    assert result["qualified_for_writer"] is True
    assert seen.get("auth") == "Bearer mock-secret-inside-container"

    status = inf.load_status(Paths.resolve(data))
    assert status["mock"]["ok"] is True

    # Unknown provider is a usage error, not a qualification failure
    assert _run_cli("--data-dir", str(data), "inference", "test", "--name", "nope") == 2


def _patch_mock_provider(monkeypatch, handler):
    import httpx

    # NOTE: only httpx.get/post are patched. Patching httpx.Client too would
    # nest wrappers on a second patch within one test, pinning the first
    # transport (the CLI path under test uses get/post only).
    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_get(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr("httpx.get", patched_get)

    def patched_post(url, **kwargs):
        with orig_client(transport=transport) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr("httpx.post", patched_post)


def _save_provider(data, name, roles, capsys):
    (data / "config").mkdir(parents=True, exist_ok=True)
    payload_file = data / "config" / f"{name}.json"
    payload_file.write_text(json.dumps({
        "providers": [{"name": name, "url": "http://mock.test/v1",
                       "model": "m", "api_key_env": ""}],
        "roles": roles,
    }))
    assert _run_cli("--data-dir", str(data), "inference", "save",
                    "--file", str(payload_file)) == 0
    capsys.readouterr()


def _valid_rubric():
    return json.dumps(
        {"scores": [{"idx": 1, "relevance": 2, "clarity": 2,
                     "instructional": 2, "payoff": 2},
                    {"idx": 2, "relevance": 1, "clarity": 1,
                     "instructional": 0, "payoff": 0}],
         "decision": "KEEP", "winner": 1, "explanation": "first frame is clearer"})


def _mock_handler(*, models=200, writer="json", vision="rubric", asr="missing"):
    """Configurable OpenAI-compatible mock: models status, writer/vision bodies.

    writer: "json" (valid {"ok": true}), "prose" (non-JSON generation failure),
        "error" (HTTP 500).
    vision: "rubric" (valid strict V9 rubric), "invalid" (wrong schema),
        "error" (HTTP 500).
    asr: "missing" (404 on /asr — a pure LLM server), "unreachable"
        (connection refused everywhere), "ok" (valid transcription shape).
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            if models == "unreachable":
                raise httpx.ConnectError("refused")
            return httpx.Response(models, json={"data": [{"id": "m"}]})
        if request.url.path.endswith("/asr"):
            if asr == "unreachable":
                raise httpx.ConnectError("refused")
            if asr == "ok":
                return httpx.Response(200, json={
                    "language": "en",
                    "text": "hello world",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hello world",
                                  "words": [{"start": 0.0, "end": 0.5, "word": "hello"},
                                            {"start": 0.5, "end": 1.0, "word": "world"}]}]})
            return httpx.Response(404, json={"detail": "Not Found"})
        body = json.loads(request.content.decode()) if request.content else {}
        if "image_url" in json.dumps(body):
            if vision == "rubric":
                content = _valid_rubric()
            elif vision == "invalid":
                content = json.dumps({"nope": True})
            else:
                return httpx.Response(500, json={"error": "vision boom"})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        if writer == "json":
            content = json.dumps({"ok": True, "text": "hello"})
        elif writer == "prose":
            content = "Here is my essay about the topic at length."
        else:
            return httpx.Response(500, json={"error": "writer boom"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def _run_test(data, capsys, name):
    rc = _run_cli("--data-dir", str(data), "inference", "test", "--name", name)
    return rc, json.loads(capsys.readouterr().out)


def test_cli_test_exit_unreachable(tmp_path, capsys, monkeypatch):
    data = tmp_path / "exit-unreachable"
    _save_provider(data, "mock", {"writer": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models="unreachable", asr="unreachable"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1
    assert result["ok"] is False


def test_cli_test_exit_unauthorized(tmp_path, capsys, monkeypatch):
    data = tmp_path / "exit-401"
    _save_provider(data, "mock", {"writer": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=401))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1
    assert result["ok"] is False


def test_cli_test_exit_writer_probe_fails(tmp_path, capsys, monkeypatch):
    """/models 200 but the writer generation probe fails → nonzero for writer role."""
    data = tmp_path / "exit-writer"
    _save_provider(data, "mock", {"writer": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="prose"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1, result
    assert result["ok"] is True  # reachable, but not writer-qualified
    assert result["qualified_for_writer"] is False


def test_cli_test_exit_vision_probe_fails(tmp_path, capsys, monkeypatch):
    """/models 200 but the vision rubric probe fails → nonzero for vision role."""
    data = tmp_path / "exit-vision"
    _save_provider(data, "mock", {"vision": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, vision="invalid"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1, result
    assert result["ok"] is True
    assert result["qualified_for_vision"] is False


def test_cli_test_exit_both_roles_require_both(tmp_path, capsys, monkeypatch):
    data = tmp_path / "exit-both"
    _save_provider(data, "mock", {"writer": "mock", "vision": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="json", vision="rubric"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 0, result
    assert result["qualified_for_writer"] is True
    assert result["qualified_for_vision"] is True
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="json", vision="invalid"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1, result  # vision assigned but failing → nonzero


def test_cli_test_exit_writer_only_ignores_vision(tmp_path, capsys, monkeypatch):
    """A writer-only endpoint succeeds even when the vision probe would fail."""
    data = tmp_path / "exit-writer-only"
    _save_provider(data, "mock", {"writer": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="json", vision="invalid"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 0, result
    assert result["qualified_for_writer"] is True


def test_cli_test_exit_vision_only_ignores_writer(tmp_path, capsys, monkeypatch):
    data = tmp_path / "exit-vision-only"
    _save_provider(data, "mock", {"vision": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="prose", vision="rubric"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 0, result
    assert result["qualified_for_vision"] is True


def test_cli_test_exit_unassigned_is_connectivity_only(tmp_path, capsys, monkeypatch):
    """An unassigned provider's reachable test exits 0 without claiming a role."""
    data = tmp_path / "exit-unassigned"
    _save_provider(data, "mock", {}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="json", vision="rubric"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 0, result
    assert result["ok"] is True
    assert _run_cli("--data-dir", str(data), "inference", "show") == 0
    show = json.loads(capsys.readouterr().out)
    assert "mock" not in (show["roles"] or {}).values()


def test_cli_test_exit_asr_unqualified(tmp_path, capsys, monkeypatch):
    data = tmp_path / "exit-asr"
    _save_provider(data, "mock", {"asr": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models="unreachable", asr="unreachable"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1
    assert result["ok"] is False


def test_cli_test_exit_asr_protocol_ok_but_unassigned_ok(tmp_path, capsys, monkeypatch):
    # Provider speaks /asr but is not assigned any role: reachable, exit 0.
    data = tmp_path / "exit-asr-ok"
    _save_provider(data, "mock", {}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models="unreachable", asr="ok"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 0
    assert result["ok"] is True
    assert result["qualified_for_asr"] is True


def test_cli_test_exit_asr_assigned_to_llm_provider(tmp_path, capsys, monkeypatch):
    # Provider serves /models (LLM) but is assigned the asr role: the asr
    # check must fail loudly instead of passing on LLM qualification.
    data = tmp_path / "exit-asr-llm"
    _save_provider(data, "mock", {"asr": "mock"}, capsys)
    _patch_mock_provider(monkeypatch, _mock_handler(models=200, writer="json"))
    rc, result = _run_test(data, capsys, "mock")
    assert rc == 1
    assert result.get("qualified_for_asr") in (None, False)
