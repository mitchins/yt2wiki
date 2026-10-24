"""Shared fixtures for web/operator-auth tests (no models, no network)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

TEST_OPERATOR_PASSWORD = "test-operator-password"


@pytest.fixture
def operator_password(monkeypatch):
    """Set the operator password for the test process."""
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    return TEST_OPERATOR_PASSWORD


def login_client(application, password: str = TEST_OPERATOR_PASSWORD, **kwargs):
    """Log in and return (client, csrf_token) with session cookie stored."""
    client = TestClient(application, **kwargs)
    r = client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    state = client.get("/api/auth/state").json()
    assert state["authenticated"] is True
    assert state["csrf_token"]
    return client, state["csrf_token"]


def post_json(client, csrf, url, payload):
    return client.post(url, json=payload, headers={"X-CSRF-Token": csrf})


def configure_ready(settings, *, writer=True, asr=True):
    """Seed a fully qualified inference setup (providers + matching status)."""
    import time as _time

    from yt2wiki.web import inference as _inf

    providers: dict = {}
    roles: dict = {}
    status: dict = {}
    if writer:
        providers["llm"] = {"url": "http://llm:8000/v1", "model": "m", "api_key_env": ""}
        roles["writer"] = "llm"
        roles["vision"] = "llm"
        status["llm"] = {"ok": True, "url": "http://llm:8000/v1", "model": "m",
                         "api_key_env": "", "qualified_for_writer": True,
                         "qualified_for_vision": True, "checked_at": _time.time()}
    if asr:
        providers["stt"] = {"url": "http://asr:9000", "model": "large-v3", "api_key_env": ""}
        roles["asr"] = "stt"
        status["stt"] = {"ok": True, "url": "http://asr:9000", "model": "large-v3",
                         "api_key_env": "", "qualified_for_asr": True,
                         "checked_at": _time.time()}
    cfg = _inf.save(settings.paths, {"providers": [
        {"name": n, **p} for n, p in providers.items()], "roles": roles})
    _inf._write_json(settings.paths, "status", status)
    return cfg
