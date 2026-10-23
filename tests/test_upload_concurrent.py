"""B1: concurrent duplicate-upload registration (issue #10).

Deterministic barrier tests — not merely sequential dedup.
Required invariants:
- Two simultaneous uploads of identical bytes return same source ID.
- Final DB record points to existing correct media file.
- Neither deletes another's media.
- No shared final file partially overwritten.
- No orphaned temp file after success/failure.
- DB conflict does not return spurious 500.
- Different-content uploads remain independent.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.web.app import create_app


def _upload(client: TestClient, filename: str, content: bytes):
    return client.post(
        "/api/sources/upload",
        files={"file": (filename, content, "video/mp4")},
        data={"profile": "compact"},
    )


def test_concurrent_duplicate_via_service(tmp_path):
    """Two threads calling Service.create_upload_source for same bytes race to insert."""
    settings = Settings.load(data_dir=tmp_path / "conc-service", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    from yt2wiki.services import Service

    svc = Service(settings, db)

    content = b"concurrent-dup-content-" + b"z" * 2048
    # Two separate temp files with identical bytes (as web would create two dest temps)
    p1 = tmp_path / "a.mp4"
    p2 = tmp_path / "b.mp4"
    p1.write_bytes(content)
    p2.write_bytes(content)

    barrier = threading.Barrier(2)
    results: dict[str, str | Exception] = {}
    errors: list[Exception] = []

    def assert_exact():
        registered = {r["upload_path"] for r in db.query("SELECT upload_path FROM sources")}
        on_disk = {str(p) for p in settings.paths.uploads_dir.iterdir() if p.is_file()}
        assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []
        assert on_disk == registered, f"dir must hold exactly canonical files: {on_disk} vs {registered}"

    def worker(name: str, path: Path):
        try:
            barrier.wait(timeout=5)
            sid = svc.create_upload_source(path)
            results[name] = sid
        except Exception as exc:
            errors.append(exc)
            results[name] = exc  # type: ignore[assignment]

    t1 = threading.Thread(target=worker, args=("t1", p1))
    t2 = threading.Thread(target=worker, args=("t2", p2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"errors: {errors}"
    assert results["t1"] == results["t2"], f"sids differ: {results}"
    sid = str(results["t1"])
    assert sid.startswith("file-")

    row = db.query_one("SELECT source_id, upload_path FROM sources WHERE source_id=?", (sid,))
    assert row is not None
    stored = Path(row["upload_path"])
    assert stored.is_file(), "final DB record must point to existing file"
    assert stored.read_bytes() == content, "no partial overwrite"
    # No orphan tmp files
    tmps = list(settings.paths.uploads_dir.glob(".tmp.*"))
    assert tmps == [], f"orphaned temps: {tmps}"
    # Different-content control: ensure a different byte string gets different sid and independent file
    p3 = tmp_path / "c.mp4"
    p3.write_bytes(b"different-content-" + b"q" * 100)
    sid3 = svc.create_upload_source(p3)
    assert sid3 != sid
    row3 = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid3,))
    assert Path(row3["upload_path"]).is_file()
    assert Path(row3["upload_path"]).read_bytes() == b"different-content-" + b"q" * 100
    assert stored.is_file(), "original not deleted by different-content upload"
    assert_exact()


def test_concurrent_duplicate_via_api(tmp_path, monkeypatch):
    """Two simultaneous POST /api/sources/upload with identical bytes."""
    settings = Settings.load(data_dir=tmp_path / "conc-api", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    app = create_app(settings)
    monkeypatch.setattr(app.state.runner, "submit", lambda job, **kw: job)

    content = b"api-concurrent-" + b"x" * 4096
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    statuses: dict[str, int] = {}

    def worker(name: str):
        # Each thread needs its own TestClient (client is not thread-safe for concurrent use)
        # but they share the same app/state/db.
        client = TestClient(app, raise_server_exceptions=False)
        try:
            barrier.wait(timeout=5)
            r = client.post(
                "/api/sources/upload",
                files={"file": ("video.mp4", content, "video/mp4")},
                data={"profile": "compact"},
            )
            statuses[name] = r.status_code
            results[name] = r.json() if r.status_code == 200 else r.text
        except Exception as exc:
            statuses[name] = 999
            results[name] = str(exc)

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert statuses["t1"] == 200, f"t1 {statuses['t1']}: {results['t1']}"
    assert statuses["t2"] == 200, f"t2 {statuses['t2']}: {results['t2']} — must not be spurious 500 for legitimate duplicate"
    sid1 = results["t1"]["source_id"]  # type: ignore[index]
    sid2 = results["t2"]["source_id"]  # type: ignore[index]
    assert sid1 == sid2, f"identical bytes must dedup to same sid: {sid1} vs {sid2}"

    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid1,))
    assert row is not None
    stored = Path(row["upload_path"])
    assert stored.is_file()
    assert stored.read_bytes() == content

    # No orphaned temp files and uploads dir persists
    assert settings.paths.uploads_dir.is_dir()
    tmps = list(settings.paths.uploads_dir.glob(".tmp.*"))
    assert tmps == [], f"orphaned temps after concurrent API: {tmps}"
    # Also ensure no extra .tmp leftover from dest temps (upload_id temps)
    # The API handler creates dest as upload_id-*.tmp via upload_path, but should clean it.
    # After dedup, the winner's canonical file remains, loser's dest temp is cleaned.
    files = [p for p in settings.paths.uploads_dir.iterdir() if p.is_file()]
    # Should have exactly 1 canonical file for this content (plus maybe previous tests don't share dir)
    assert any(p == stored for p in files), f"stored not in uploads: {files}"
    # Different-content concurrent: ensure independence
    content2 = b"api-different-" + b"y" * 100
    c1 = TestClient(app)
    r1 = c1.post("/api/sources/upload", files={"file": ("a.mp4", content2, "video/mp4")}, data={"profile": "compact"})
    assert r1.status_code == 200
    assert r1.json()["source_id"] != sid1
    assert stored.is_file(), "original must not be deleted by different-content upload"
    registered = {r["upload_path"] for r in db.query("SELECT upload_path FROM sources")}
    on_disk = {str(p) for p in settings.paths.uploads_dir.iterdir() if p.is_file()}
    assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []
    assert on_disk == registered, f"dir must hold exactly canonical files: {on_disk} vs {registered}"


def test_concurrent_different_content_independent(tmp_path):
    """Two different byte strings concurrently must not interfere."""
    settings = Settings.load(data_dir=tmp_path / "conc-diff", profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    from yt2wiki.services import Service

    svc = Service(settings, db)
    content_a = b"content-A-" + b"a" * 1024
    content_b = b"content-B-" + b"b" * 1024
    pa = tmp_path / "pa.mp4"
    pb = tmp_path / "pb.mp4"
    pa.write_bytes(content_a)
    pb.write_bytes(content_b)

    barrier = threading.Barrier(2)
    out: dict[str, str] = {}

    def wa():
        barrier.wait(timeout=5)
        out["a"] = svc.create_upload_source(pa)

    def wb():
        barrier.wait(timeout=5)
        out["b"] = svc.create_upload_source(pb)

    ta = threading.Thread(target=wa)
    tb = threading.Thread(target=wb)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    assert out["a"] != out["b"]
    ra = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (out["a"],))
    rb = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (out["b"],))
    assert Path(ra["upload_path"]).read_bytes() == content_a
    assert Path(rb["upload_path"]).read_bytes() == content_b
    assert Path(ra["upload_path"]).is_file()
    assert Path(rb["upload_path"]).is_file()
    assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []
    registered = {r["upload_path"] for r in db.query("SELECT upload_path FROM sources")}
    on_disk = {str(p) for p in settings.paths.uploads_dir.iterdir() if p.is_file()}
    assert on_disk == registered, f"dir must hold exactly canonical files: {on_disk} vs {registered}"
