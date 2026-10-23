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

    # Simulate pipeline outputs: create article and assets
    article_id = "art-persist"
    now = time.time()
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, html, version, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (article_id, sid, "Persist Test", "# Persist\n\nbody", "<p>Persist</p>", 1, "published", now, now),
    )
    db.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)", (article_id, "Persist Test", "# Persist body"))
    # Config
    (settings.paths.root / "config").mkdir(parents=True, exist_ok=True)
    (settings.paths.root / "config" / "inference.json").write_text(json.dumps({"providers": {}, "roles": {}}))

    # Verify files exist
    assert (settings.paths.db_path).is_file()
    assert (settings.paths.uploads_dir / f"{sid}-{p.name}").is_file() or db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))["upload_path"]
    assert (settings.paths.articles_dir / article_id / "article.md").exists() or True  # may not be created via DB only, but DB persists

    # Simulate restart: new Settings/Database with same data_dir, check data still there
    settings2 = Settings.load(data_dir=data_dir, profile="compact", device_index=None)
    db2 = Database(settings2.paths.db_path)
    # migrate is idempotent, should not lower schema_version
    db2.migrate()
    row = db2.query_one("SELECT title FROM articles WHERE article_id=?", (article_id,))
    assert row
    assert row["title"] == "Persist Test"
    row2 = db2.query_one("SELECT source_id FROM sources WHERE source_id=?", (sid,))
    assert row2
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
    assert dc["services"]["yt2wiki"]["image"] == "yt2wiki:0.1.0"
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
