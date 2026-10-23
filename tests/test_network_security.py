"""B6: network security — localhost-only guard and forwarded-header handling.

Behavioral: TestClient with an explicit gateway peer address proves the
settings API refuses Docker-gateway/LAN/tunneled browsers, ignores
forwarding headers, and keeps health/library public.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.web.app import create_app


def _app(tmp_path, name):
    settings = Settings.load(data_dir=tmp_path / name, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return create_app(settings)


def test_inference_settings_requires_localhost(tmp_path):
    app = _app(tmp_path, "net1")
    local = TestClient(app)

    # Default client is "testclient" — allowed
    r = local.post(
        "/api/settings/inference",
        json={"providers": [{"name": "a", "url": "http://a.test/v1", "api_key_env": "YT2WIKI_A"}], "roles": {}},
    )
    assert r.status_code == 200, r.text

    # Docker gateway / LAN / tunneled peers are refused, even with loopback
    # forwarding headers (an SSH tunnel does not make this a localhost peer).
    for host in ("172.18.0.1", "192.168.1.14", "10.0.0.5"):
        gw = TestClient(app, client=(host, 50000))
        r = gw.post(
            "/api/settings/inference",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
            json={"providers": [{"name": "b", "url": "http://b.test/v1"}], "roles": {}},
        )
        assert r.status_code == 403, host
        r = gw.post("/api/settings/inference/test",
                    json={"url": "http://b.test/v1", "name": "b", "model": ""})
        assert r.status_code == 403, host

    # Guard does not trust forwarding headers and checks the real peer
    import pathlib

    app_src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "yt2wiki" / "web" / "app.py").read_text()
    assert "X-Forwarded-For" not in app_src, "guard must not trust X-Forwarded-For"
    assert "X-Real-IP" not in app_src, "guard must not trust X-Real-IP"


def test_health_and_library_accessible_without_localhost(tmp_path):
    """Health and library must be accessible from any host, but inference config not."""
    app = _app(tmp_path, "net2")
    gw = TestClient(app, client=("172.18.0.1", 50000))

    assert gw.get("/health").status_code == 200
    assert gw.get("/library").status_code == 200
    assert gw.get("/api/search?q=test").status_code == 200
    assert gw.get("/settings").status_code == 200  # page renders, save/test refused
