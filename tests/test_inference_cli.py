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
from yt2wiki.web.app import create_app, is_local_peer, peer_host


def _app(tmp_path, name):
    settings = Settings.load(data_dir=tmp_path / name, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return create_app(settings), settings, db


def test_peer_helpers_classify_gateway_and_loopback():
    assert is_local_peer("127.0.0.1")
    assert is_local_peer("::1")
    assert is_local_peer("testclient")
    assert is_local_peer("localhost")
    assert not is_local_peer("172.18.0.1")
    assert not is_local_peer("192.168.1.14")
    assert not is_local_peer("")

    class FakeClient:
        host = "172.18.0.1"

    class FakeRequest:
        client = FakeClient()

    assert peer_host(FakeRequest()) == "172.18.0.1"  # type: ignore[arg-type]
    assert peer_host(None) == ""  # type: ignore[arg-type]


def test_gateway_save_and_test_refused_even_with_forwarding_headers(tmp_path):
    """Docker gateway peers get 403; X-Forwarded-For never grants access."""
    app, _settings, _db = _app(tmp_path, "gw")
    gateway = TestClient(app, client=("172.18.0.1", 50000))
    payload = {"providers": [{"name": "a", "url": "http://a.test/v1"}], "roles": {}}

    r = gateway.post("/api/settings/inference", json=payload)
    assert r.status_code == 403, r.text
    assert "operator console" in r.json()["detail"]
    assert "SSH" in r.json()["detail"]

    r = gateway.post(
        "/api/settings/inference",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        json=payload,
    )
    assert r.status_code == 403, "forwarding headers must not grant access"

    r = gateway.post("/api/settings/inference/test",
                     json={"url": "http://a.test/v1", "name": "a", "model": ""})
    assert r.status_code == 403

    # Localhost peer (default testclient) can still save
    local = TestClient(app)
    r = local.post("/api/settings/inference", json=payload)
    assert r.status_code == 200, r.text


def test_settings_page_marks_operator_only_for_gateway(tmp_path):
    app, _settings, _db = _app(tmp_path, "banner")
    gateway = TestClient(app, client=("172.18.0.1", 50000))
    r = gateway.get("/settings")
    assert r.status_code == 200
    assert "Operator only" in r.text
    assert "data-remote" in r.text
    assert "docker exec" in r.text
    assert "SSH tunnel does not make this a localhost peer" in r.text

    local = TestClient(app)
    r = local.get("/settings")
    assert r.status_code == 200
    assert "Operator only" not in r.text
    assert "data-remote" not in r.text


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

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("httpx.Client", patched_client)

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
