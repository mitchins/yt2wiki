"""Web API and rendering tests (no models, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    application = create_app(settings)
    return application, db, settings


def test_health_and_index(app):
    application, _, _ = app
    client = TestClient(application)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/")
    assert r.status_code == 200
    assert "yt2wiki" in r.text
    r = client.get("/library")
    assert r.status_code == 200


def test_registry_endpoint_reports_profiles(app):
    application, _, _ = app
    client = TestClient(application)
    data = client.get("/api/registry").json()
    assert set(data["profiles"]) == {"compact", "standard", "enhanced"}
    assert {"asr", "vision", "text", "verify"} <= set(data["assignments"]["compact"])
    assert data["current_profile"] == "compact"


def test_gpu_endpoint(app):
    application, _, _ = app
    client = TestClient(application)
    data = client.get("/api/gpu").json()
    assert "cuda" in data
    assert isinstance(data["gpus"], list)


def test_search_requires_query(app):
    application, _, _ = app
    client = TestClient(application)
    assert client.get("/api/search").json() == {"results": []}


def test_search_returns_articles(app):
    application, db, _ = app
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, title, created_at) VALUES('s1','youtube','T',0)"
        )
        conn.execute(
            "INSERT INTO articles(article_id, source_id, title, markdown, version, status, "
            "created_at, updated_at) VALUES('art-1','s1','Install nginx','Install nginx 1.24.0',"
            "1,'published',0,0)"
        )
        conn.execute(
            "INSERT INTO articles_fts(article_id, title, markdown) "
            "VALUES('art-1','Install nginx','Install nginx 1.24.0')"
        )
    client = TestClient(application)
    data = client.get("/api/search?q=nginx").json()
    assert data["results"]
    assert data["results"][0]["article_id"] == "art-1"
    none = client.get("/api/search?q=zzzznotpresent").json()
    assert none["results"] == []


def test_article_route_404_for_missing(app):
    application, _, _ = app
    client = TestClient(application)
    assert client.get("/articles/nope").status_code == 404


def test_article_route_rewrites_asset_paths(app, tmp_path):
    application, db, settings = app
    from PIL import Image

    assets_dir = settings.paths.article_assets("art-1")
    assets_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), "green").save(assets_dir / "f-1.jpg")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, title, origin_url, duration_s, created_at) "
            "VALUES('s1','youtube','T','https://youtu.be/x',10,0)"
        )
        conn.execute(
            "INSERT INTO articles(article_id, source_id, title, markdown, html, version, status, "
            "created_at, updated_at) VALUES('art-1','s1','T',?,"
            "'<figure><img alt=\"f\" src=\"assets/f-1.jpg\"></figure>',1,'draft',0,0)",
            ("# T\n\n![f](assets/f-1.jpg)",),
        )
    client = TestClient(application)
    r = client.get("/articles/art-1")
    assert r.status_code == 200
    assert 'src="/assets/art-1/f-1.jpg"' in r.text
    assert 'src="assets/f-1.jpg"' not in r.text
    got = client.get("/assets/art-1/f-1.jpg")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/")


def test_upload_and_job_creation_without_running(app, tmp_path, operator_password):
    from tests.conftest import configure_ready, login_client
    application, _, settings = app
    configure_ready(settings)
    client, csrf = login_client(application)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not-really-a-video")
    r = client.post(
        "/api/sources/upload",
        files={"file": ("clip.mp4", b"not-really-a-video", "video/mp4")},
        data={},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_id"].startswith("file-")
    assert body["job_id"].startswith("job-")


def test_add_url_rejects_unreachable_source(app, monkeypatch, operator_password):
    from tests.conftest import login_client, post_json
    application, _, _ = app

    def boom(self, _):
        return {"ok": False, "error": "unavailable"}

    monkeypatch.setattr("yt2wiki.web.app.Service.preview_url", boom)
    client, csrf = login_client(application)
    r = post_json(client, csrf, "/api/sources/url", {"url": "https://youtu.be/nope"})
    assert r.status_code == 400
    assert "unavailable" in r.json()["detail"]


def test_article_edit_creates_revision(app, operator_password):
    from tests.conftest import login_client, post_json
    application, db, _ = app
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, title, created_at) VALUES('s1','youtube','T',0)"
        )
        conn.execute(
            "INSERT INTO articles(article_id, source_id, title, markdown, html, version, status, "
            "created_at, updated_at) VALUES('art-1','s1','T','original text',"
            "'<p>original text</p>',1,'draft',0,0)"
        )
    from tests.conftest import login_client, post_json
    client, csrf = login_client(application)
    r = post_json(client, csrf, "/api/articles/art-1", {"markdown": "# New\n\nEdited body text."})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    row = db.query_one("SELECT version FROM articles WHERE article_id='art-1'")
    assert row["version"] == 2
    revs = client.get("/api/articles/art-1/revisions").json()
    assert revs and revs[0]["origin"] == "user"


def test_asset_route_rejects_traversal(app):
    application, _, settings = app
    client = TestClient(application)
    assert client.get("/assets/art-1/../../etc/passwd").status_code in (400, 404)
    assert client.get("/assets/art-1/nested/f-1.jpg").status_code in (400, 404)
    assert client.get("/assets/art-1/nope.jpg").status_code == 404


def test_frames_route_rejects_traversal(app):
    application, _, _ = app
    client = TestClient(application)
    assert client.get("/frames/src-1/..%2f..%2fetc%2fpasswd").status_code in (400, 404)


def test_article_page_renders_with_frames_and_evidence(app, tmp_path):
    application, db, _ = app
    from PIL import Image

    (settings_frames := tmp_path / "frames").mkdir(parents=True, exist_ok=True)
    frame = settings_frames / "f-1.jpg"
    Image.new("RGB", (4, 4), "red").save(frame)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, title, origin_url, duration_s, created_at) "
            "VALUES('s1','upload','T',NULL,10,0)"
        )
        conn.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, reason, created_at) "
                     "VALUES('f-1','s1',3.5,?,'scene','scene cut',0)", (str(frame),))
        conn.execute(
            "INSERT INTO frame_interpretations VALUES(?,?,?,?,?,?)",
            ("f-1", "vl-2b", "v3", "compact",
             '{"summary":"terminal","visible_text":["ls -al"],"frame_type":"terminal"}', 0.0),
        )
        conn.execute(
            "INSERT INTO evidence(evidence_id, source_id, kind, text, t_start, t_end, "
            "frame_ids_json, confidence, model_key, prompt_version, status, created_at) "
            "VALUES('ev-1','s1','command','ls -al',3.5,4.0,'[]',0.9,'m','v','active',0)"
        )
        conn.execute(
            "INSERT INTO articles(article_id, source_id, title, markdown, html, version, status, "
            "created_at, updated_at) VALUES('art-1','s1','T',?,'<h1>T</h1>',1,'published',0,0)",
            ("# T\n\n1. **Do it.**\n\n   *Source: 0:03 of the source video*",),
        )
    client = TestClient(application)
    r = client.get("/articles/art-1")
    assert r.status_code == 200, r.text
    assert "terminal" in r.text
    assert "ls -al" in r.text
    assert "Source: 0:03 of the source video" in r.text
    assert "/frames/s1/f-1.jpg" in r.text


def test_frames_and_evidence_endpoints(app, tmp_path):
    application, db, settings = app
    from PIL import Image

    frame = settings.paths.source_frames("s1") / "f-2.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), "blue").save(frame)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, created_at) VALUES('s1','youtube',0)"
        )
        conn.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) "
                     "VALUES('f-2','s1',1.0,?,'periodic',0)", (str(frame),))
        conn.execute(
            "INSERT INTO evidence(evidence_id, source_id, kind, text, t_start, t_end, "
            "frame_ids_json, confidence, model_key, prompt_version, status, created_at) "
            "VALUES('ev-2','s1','warning','be careful',1.0,2.0,'[]',0.9,'m','v','active',0)"
        )
    client = TestClient(application)
    frames = client.get("/api/sources/s1/frames").json()
    assert frames and frames[0]["kind"] == "periodic"
    ev = client.get("/api/sources/s1/evidence").json()
    assert ev[0]["kind"] == "warning"
    segs = client.get("/api/sources/s1/transcript").json()
    assert segs == []
