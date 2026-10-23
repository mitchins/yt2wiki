"""Regression for P0: shared upload directory deletion.

The old code did `shutil.rmtree(dest.parent)` where dest.parent is the
shared uploads/ directory. Because Service.create_upload_source stores videos
under content-addressed filenames, the condition `stored != dest` is true for
normal successful uploads, so the parent deletion would wipe all previously
registered source media.

The fix must delete only the temporary upload file (dest) and use try/finally
so failed registrations also clean their temp file without touching the shared
directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.web.app import create_app


@pytest.fixture()
def isolated_app(tmp_path):
    settings = Settings.load(data_dir=tmp_path / "data", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    application = create_app(settings)
    # Don't start the runner thread for these tests
    return application, db, settings


def _upload(client: TestClient, filename: str, content: bytes):
    return client.post(
        "/api/sources/upload",
        files={"file": (filename, content, "video/mp4")},
        data={"profile": "compact"},
    )


def test_upload_isolation_and_dedup(tmp_path, monkeypatch):
    """Steps 1-3,5,6 combined in isolated data dir."""
    # Use isolated_app fixture logic but manual to control sequence
    settings = Settings.load(data_dir=tmp_path / "iso1", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    app = create_app(settings)
    # Mock job scheduling — no GPU inference, just source registration
    monkeypatch.setattr(app.state.runner, "submit", lambda job, **kw: job)
    client = TestClient(app)

    # 1. Upload A
    content_a = b"video-a-content-" + b"x" * 100
    r = _upload(client, "video-a.mp4", content_a)
    assert r.status_code == 200, r.text
    sid_a = r.json()["source_id"]
    stored_a = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid_a,))["upload_path"])
    assert stored_a.is_file(), "A's source file must exist"
    assert stored_a.read_bytes() == content_a
    uploads_dir = settings.paths.uploads_dir
    assert uploads_dir.is_dir(), "uploads dir must persist after A"

    # 2. Upload B (different)
    content_b = b"video-b-different-" + b"y" * 100
    r2 = _upload(client, "video-b.mp4", content_b)
    assert r2.status_code == 200, r2.text
    sid_b = r2.json()["source_id"]
    assert sid_b != sid_a
    stored_b = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid_b,))["upload_path"])
    assert stored_b.is_file()
    assert stored_a.is_file(), "A must still exist after B"
    assert uploads_dir.is_dir()
    # ensure both files are present in uploads_dir
    files = {p.name for p in uploads_dir.iterdir() if p.is_file()}
    assert stored_a.name in files
    assert stored_b.name in files

    # 3. Upload A again (same bytes, different filename) -> dedup
    r3 = _upload(client, "copy-of-a.mp4", content_a)
    assert r3.status_code == 200, r3.text
    sid_a2 = r3.json()["source_id"]
    assert sid_a2 == sid_a, "deduplication must return existing source"
    # stored file for A must still exist and be readable
    assert stored_a.is_file()
    assert stored_a.read_bytes() == content_a
    assert uploads_dir.is_dir()
    # temp file for the third upload must be gone, but stored must remain
    # count files: should still be 2 (A and B), not 3
    files_after = {p.name for p in uploads_dir.iterdir() if p.is_file()}
    assert files_after == files, f"temp dedup file should be cleaned, got {files_after}"

    # 5. source media can still be read for subsequent processing
    assert stored_a.read_bytes() == content_a
    assert stored_b.read_bytes() == content_b

    # 6. upload directory persists
    assert uploads_dir.is_dir()
    assert uploads_dir.exists()


def test_upload_failure_cleans_temp_without_deleting_others(tmp_path, monkeypatch):
    """Step 4: registration failure cleans temp file only."""
    settings = Settings.load(data_dir=tmp_path / "iso2", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    app = create_app(settings)
    monkeypatch.setattr(app.state.runner, "submit", lambda job, **kw: job)
    client = TestClient(app, raise_server_exceptions=False)

    # Pre-populate with A and B
    content_a = b"pre-a-" + b"a" * 50
    r = _upload(client, "a.mp4", content_a)
    assert r.status_code == 200
    sid_a = r.json()["source_id"]
    stored_a = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid_a,))["upload_path"])

    content_b = b"pre-b-" + b"b" * 50
    r = _upload(client, "b.mp4", content_b)
    assert r.status_code == 200
    sid_b = r.json()["source_id"]
    stored_b = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid_b,))["upload_path"])

    uploads_dir = settings.paths.uploads_dir
    before = {p.name for p in uploads_dir.iterdir() if p.is_file()}
    assert stored_a.name in before
    assert stored_b.name in before

    # Monkeypatch Service.create_upload_source to fail
    from yt2wiki.services import Service

    orig = Service.create_upload_source

    def failing(self, path):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(Service, "create_upload_source", failing)

    # Attempt upload that will fail — must return error, not spurious 200 success
    r_fail = _upload(client, "fail.mp4", b"fail-content")
    assert r_fail.status_code >= 400, f"expected error for failed registration, got {r_fail.status_code}: {r_fail.text}"
    assert r_fail.status_code in (400, 500), r_fail.text
    # Upload dir must still exist and contain the original files
    assert uploads_dir.is_dir(), "uploads dir must persist after failure"
    after = {p.name for p in uploads_dir.iterdir() if p.is_file()}
    assert stored_a.name in after, "A must survive failure"
    assert stored_b.name in after, "B must survive failure"
    # No new file for the failed upload should remain (temp cleaned)
    # The failed content's hash would have been ... but it wasn't stored
    assert after == before, f"failed upload must not leave temp file, before {before} after {after}"

    # Restore and verify subsequent uploads still work
    monkeypatch.setattr(Service, "create_upload_source", orig)
    r_ok = _upload(client, "ok-after-fail.mp4", b"ok-after")
    assert r_ok.status_code == 200, r_ok.text
    sid_ok = r_ok.json()["source_id"]
    stored_ok = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid_ok,))["upload_path"])
    assert stored_ok.is_file()
    assert uploads_dir.is_dir()


def test_upload_directory_persists_after_success_and_failure(tmp_path, monkeypatch):
    """Explicitly test that dest.parent is never deleted."""
    settings = Settings.load(data_dir=tmp_path / "iso3", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    app = create_app(settings)
    monkeypatch.setattr(app.state.runner, "submit", lambda job, **kw: job)
    client = TestClient(app)

    uploads_dir = settings.paths.uploads_dir
    # Initially uploads dir may not exist; first upload creates it
    r = _upload(client, "first.mp4", b"first")
    assert r.status_code == 200
    assert uploads_dir.is_dir()

    r2 = _upload(client, "second.mp4", b"second")
    assert r2.status_code == 200
    # uploads dir must still be the same directory, not recreated
    assert uploads_dir.is_dir()
    # If the old bug were present (rmtree parent), the directory would have been
    # deleted and recreated, changing mtime or losing files — we assert it persists
    # and still contains both files
    files = list(uploads_dir.iterdir())
    assert len([p for p in files if p.is_file()]) >= 2
