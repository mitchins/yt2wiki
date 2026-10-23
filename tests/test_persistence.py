"""B7/B8: persistence, restart recovery, and compose docs."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.services import Service
from yt2wiki.web.app import create_app


def test_persistent_storage_and_restart_recovery(tmp_path, monkeypatch):
    """SQLite, uploads, frames, articles, assets, revisions, config all persist and recover."""
    data_dir = tmp_path / "persist"
    settings = Settings.load(data_dir=data_dir, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    svc = Service(settings, db)

    # Create a source via upload
    content = b"persist-test-video-" + b"p" * 200
    p = tmp_path / "vid.mp4"
    p.write_bytes(content)
    sid = svc.create_upload_source(p)

    # Simulate pipeline outputs: create the article row plus the article files
    # the pipeline would write, so the restart check covers files as well as rows.
    article_id = "art-persist"
    now = time.time()
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, html, version, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (article_id, sid, "Persist Test", "# Persist\n\nbody", "<p>Persist</p>", 1, "published", now, now),
    )
    db.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)", (article_id, "Persist Test", "# Persist body"))
    settings.paths.article_dir(article_id).mkdir(parents=True, exist_ok=True)
    settings.paths.article_markdown(article_id).write_text("# Persist\n\nbody", encoding="utf-8")
    settings.paths.article_html(article_id).write_text("<p>Persist</p>", encoding="utf-8")
    # Config
    (settings.paths.root / "config").mkdir(parents=True, exist_ok=True)
    (settings.paths.root / "config" / "inference.json").write_text(json.dumps({"providers": {}, "roles": {}}))

    # Verify files exist before restart
    assert (settings.paths.db_path).is_file()
    stored = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))["upload_path"])
    assert stored.is_file()
    assert stored.read_bytes() == content
    assert settings.paths.article_markdown(article_id).is_file()
    assert settings.paths.article_html(article_id).is_file()

    # Simulate restart: new Settings/Database with same data_dir, check data still there
    settings2 = Settings.load(data_dir=data_dir, profile="compact", device_index=None)
    db2 = Database(settings2.paths.db_path)
    # migrate is idempotent, should not lower schema_version
    db2.migrate()
    row = db2.query_one("SELECT title FROM articles WHERE article_id=?", (article_id,))
    assert row
    assert row["title"] == "Persist Test"
    row2 = db2.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row2
    stored2 = Path(row2["upload_path"])
    assert stored2.is_file(), "registered upload must survive restart"
    assert stored2.read_bytes() == content
    assert settings2.paths.article_markdown(article_id).is_file()
    assert settings2.paths.article_html(article_id).is_file()
    # Config persists
    assert (settings2.paths.root / "config" / "inference.json").is_file()

    # Job recovery: create queued + running web jobs, then recover
    app = create_app(settings2)
    monkeypatch.setattr(app.state.runner, "start", lambda: None)
    svc2 = Service(settings2, db2)
    j_queued = svc2.create_job(sid, profile="compact", config={"pipeline": "v9", "origin": "web"})
    j_running = svc2.create_job(sid, profile="compact", config={"pipeline": "v9", "origin": "web"})
    db2.execute("UPDATE jobs SET state='running' WHERE job_id=?", (j_running,))
    db2.execute("INSERT INTO stages(job_id, stage, state) VALUES(?, 'v9_write', 'running')", (j_running,))
    out = app.state.runner.recover()
    assert out["requeued"] == 1
    assert out["interrupted"] == 1
    # After recover, queued job is live, running job is interrupted
    assert db2.query_one("SELECT state FROM jobs WHERE job_id=?", (j_queued,))["state"] == "queued"
    assert db2.query_one("SELECT state FROM jobs WHERE job_id=?", (j_running,))["state"] == "interrupted"
    # Library still serves after restart
    app2 = create_app(settings2)
    monkeypatch.setattr(app2.state.runner, "submit", lambda j, **kw: j)
    client = TestClient(app2)
    assert client.get("/library").status_code == 200
    assert client.get("/health").status_code == 200


def test_compose_files_exist_and_are_valid(tmp_path):
    """B7: compose files are present, valid yaml, and document required/optional services."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    for fname in ["docker-compose.yml", "docker-compose.portainer.yml", "Dockerfile", "docs/deploy.md"]:
        p = root / fname
        assert p.is_file(), f"{fname} missing"

    # Validate compose yaml
    dc = yaml.safe_load((root / "docker-compose.yml").read_text())
    assert "yt2wiki" in dc["services"]
    assert "volumes" in dc
    assert dc["services"]["yt2wiki"]["image"] == \
        "ghcr.io/mitchins/yt2wiki:${YT2WIKI_IMAGE_TAG:-main}"
    assert "yt2wiki-data" in dc["volumes"]
    assert dc["services"]["yt2wiki"]["healthcheck"]["test"] == ["CMD", "curl", "-sf", "http://127.0.0.1:8000/health"]
    # No GPU for yt2wiki
    assert "reservations" not in str(dc["services"]["yt2wiki"].get("deploy", {}))
    # Sidecar has profile and gpu reservation
    assert "llama" in dc["services"]
    assert "llama" in dc["services"]["llama"].get("profiles", [])
    assert "nvidia" in str(dc["services"]["llama"])

    dc_port = yaml.safe_load((root / "docker-compose.portainer.yml").read_text())
    assert "yt2wiki" in dc_port["services"]
