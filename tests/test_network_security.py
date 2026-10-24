"""B6: operator authentication over Docker forwarding.

Behavioral: TestClient with an explicit gateway peer address proves that
mutations require a password-backed session plus CSRF (regardless of peer
or forged forwarding headers), that sessions survive a restart, and that
health/library stay public.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import TEST_OPERATOR_PASSWORD, login_client  # noqa: E402
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402


def _app(tmp_path, name):
    settings = Settings.load(data_dir=tmp_path / name, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return create_app(settings), settings


def test_unauthenticated_mutations_denied_from_gateway(tmp_path, monkeypatch):
    """Jobs and article edits from a Docker-forwarded browser need login."""

    monkeypatch.delenv("YT2WIKI_OPERATOR_PASSWORD", raising=False)
    app, _ = _app(tmp_path, "net1")
    gw = TestClient(app, client=("172.18.0.1", 50000))
    # No password configured: fail closed with setup instructions.
    r = gw.post("/api/settings/inference", json={"providers": [], "roles": {}})
    assert r.status_code == 403
    assert "YT2WIKI_OPERATOR_PASSWORD" in r.json()["detail"]
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    # Password configured but no session: login required (401, not 403).
    for method, url, kwargs in [
        ("post", "/api/settings/inference", {"json": {"providers": [], "roles": {}}}),
        ("post", "/api/settings/inference/test", {"json": {"url": "http://x.test/v1"}}),
        ("post", "/api/sources/url", {"json": {"url": "https://youtu.be/x"}}),
        ("post", "/jobs/job-1/cancel", {}),
        ("post", "/api/articles/a1", {"json": {"markdown": "# T\n\ntext here"}}),
    ]:
        r = getattr(gw, method)(url, **kwargs)
        assert r.status_code == 401, (url, r.status_code)


def test_forged_forwarding_headers_cannot_bypass_auth(tmp_path, monkeypatch):
    """Spoofed X-Forwarded-For grants nothing; only the session cookie does."""
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    app, _ = _app(tmp_path, "net2")
    gw = TestClient(app, client=("172.18.0.1", 50000))
    forged = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    r = gw.post("/api/settings/inference", headers=forged,
                json={"providers": [], "roles": {}})
    assert r.status_code == 401
    # Same forged headers WITH a session cookie and CSRF: allowed.
    cookie_client = TestClient(app, client=("172.18.0.1", 50000))
    login = cookie_client.post("/api/auth/login", json={"password": TEST_OPERATOR_PASSWORD})
    assert login.status_code == 200
    csrf = cookie_client.get("/api/auth/state").json()["csrf_token"]
    r = cookie_client.post("/api/settings/inference", headers={**forged, "X-CSRF-Token": csrf},
                           json={"providers": [], "roles": {}})
    assert r.status_code == 200, r.text


def test_csrf_required_even_with_session(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    app, _ = _app(tmp_path, "net3")
    client, csrf = login_client(app)
    # Wrong token.
    r = client.post("/api/settings/inference", headers={"X-CSRF-Token": "wrong"},
                    json={"providers": [], "roles": {}})
    assert r.status_code == 403
    # Missing token.
    bare = TestClient(app)
    bare.post("/api/auth/login", json={"password": TEST_OPERATOR_PASSWORD})
    r = bare.post("/api/settings/inference", json={"providers": [], "roles": {}})
    assert r.status_code == 403
    # Correct token works; wrong password does not log in.
    r = client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert csrf


def test_session_survives_restart(tmp_path, monkeypatch):
    """Sessions persist in the config dir, so a restart keeps the operator logged in."""

    from yt2wiki.web import auth as _auth

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    app, settings = _app(tmp_path, "net4")
    client = TestClient(app)
    token_before = client.post("/api/auth/login",
                              json={"password": TEST_OPERATOR_PASSWORD}).cookies.get("yt2wiki_op")
    assert token_before
    csrf_before = client.get("/api/auth/state").json()["csrf_token"]
    # Fresh app instance over the same data dir (simulates a restart).
    app2 = create_app(settings)
    client2 = TestClient(app2)
    client2.cookies.set("yt2wiki_op", token_before)
    state = client2.get("/api/auth/state").json()
    assert state["authenticated"] is True
    assert state["csrf_token"] == csrf_before
    r = client2.post("/api/settings/inference", headers={"X-CSRF-Token": csrf_before},
                     json={"providers": [], "roles": {}})
    assert r.status_code == 200, r.text
    # Logout revokes: the same cookie stops working.
    assert client2.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_before}).status_code == 200
    assert client2.get("/api/auth/state").json()["authenticated"] is False
    assert _auth._read_sessions(settings.paths) == {}


def test_health_and_library_accessible_without_login(tmp_path):
    """Health and library stay public; settings page renders logged-out state."""
    app, _ = _app(tmp_path, "net5")
    gw = TestClient(app, client=("172.18.0.1", 50000))

    assert gw.get("/health").status_code == 200
    assert gw.get("/library").status_code == 200
    assert gw.get("/api/search?q=test").status_code == 200
    page = gw.get("/settings")
    assert page.status_code == 200
    assert "Operator login" in page.text or "operator password" in page.text


def test_auth_edge_cases_unit(tmp_path, monkeypatch):
    """Direct unit coverage: empty secrets, unknown/expired tokens, guards."""
    import time as _time

    from yt2wiki.web import auth as _auth

    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)
    assert _auth.verify_password("") is False
    assert _auth.verify_password("wrong") is False
    assert _auth.verify_password(TEST_OPERATOR_PASSWORD) is True
    monkeypatch.delenv("YT2WIKI_OPERATOR_PASSWORD")
    assert _auth.verify_password(TEST_OPERATOR_PASSWORD) is False
    monkeypatch.setenv("YT2WIKI_OPERATOR_PASSWORD", TEST_OPERATOR_PASSWORD)

    class _P:
        root = tmp_path

    assert _auth.get_session(_P(), None) is None
    assert _auth.get_session(_P(), "nope") is None
    assert _auth.revoke_session(_P(), None) is None
    assert _auth.revoke_session(_P(), "nope") is None
    # Expired sessions are rejected.
    token, _csrf = _auth.create_session(_P())
    sessions = _auth._read_sessions(_P())
    for entry in sessions.values():
        entry["expires_at"] = _time.time() - 1
    _auth._write_sessions(_P(), sessions)
    assert _auth.get_session(_P(), token) is None
    # CSRF with no expected token never passes.
    assert _auth.csrf_valid({}, None) is False
    # Sessions persist to the fixed config filename only.
    token2, _ = _auth.create_session(_P())
    assert (_auth._sessions_path(_P())).name == "operator_sessions.json"
    assert _auth.get_session(_P(), token2) is not None
