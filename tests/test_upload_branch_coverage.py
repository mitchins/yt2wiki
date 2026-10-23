"""Upload registration integrity (B1): idempotent, no orphans, no broken references.

These tests force collisions at the database insert boundary (gated INSERTs,
pre-seeded stale rows, hostile rows, injected filesystem races) — not merely
two threads starting together. Every test asserts the directory contains
exactly the registered canonical files, not merely that no `.tmp.*` remain.
"""

from __future__ import annotations

import errno
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.paths import file_hash, source_id_for_file
from yt2wiki.services import Service


def _svc(tmp_path, name):
    settings = Settings.load(data_dir=tmp_path / name, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return Service(settings, db), settings, db


def _canonical_set(db):
    return {r["upload_path"] for r in db.query("SELECT upload_path FROM sources WHERE kind='upload'")}


def _dir_files(settings):
    return {str(p) for p in settings.paths.uploads_dir.iterdir() if p.is_file()}


def assert_exact_canonical(settings, db):
    assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []
    assert _dir_files(settings) == _canonical_set(db)


def _seed_row(db, sid, upload_path, title="seed"):
    db.execute(
        """INSERT INTO sources(source_id, kind, upload_path, created_at, title, meta_json)
           VALUES(?,?,?,?,?,?)""",
        (sid, "upload", upload_path, time.time(), title, "{}"),
    )


def test_youtube_integrity_error(tmp_path, monkeypatch):
    svc, _settings, db = _svc(tmp_path, "ytb2")
    url = "https://youtu.be/dQw4w9WgXcQ"
    sid1 = svc.create_youtube_source(url)
    assert svc.create_youtube_source(url) == sid1
    orig = db.execute

    def fake(sql, params=()):
        if "INSERT INTO sources" in sql and "youtube" in sql:
            raise sqlite3.IntegrityError("UNIQUE")
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", fake)
    assert svc.create_youtube_source(url) == sid1


def test_is_valid_upload_oserror(tmp_path, monkeypatch):
    svc, _settings, _db = _svc(tmp_path, "valid-exc")
    p = tmp_path / "x.mp4"
    p.write_bytes(b"hello")
    import yt2wiki.services as mod

    monkeypatch.setattr(
        mod, "file_hash", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("boom"))
    )
    assert svc._is_valid_upload(p, "deadbeef") is False
    assert svc._is_valid_upload(Path(tmp_path / "nope.mp4"), "abc") is False


def test_file_not_found(tmp_path):
    svc, _settings, _db = _svc(tmp_path, "notfound2")
    with pytest.raises(FileNotFoundError):
        svc.create_upload_source(Path(tmp_path / "nope.mp4"))


def test_existing_valid_dedup(tmp_path):
    svc, settings, db = _svc(tmp_path, "exist2")
    p = tmp_path / "a.mp4"
    p.write_bytes(b"valid-content")
    sid = svc.create_upload_source(p)
    p2 = tmp_path / "b.mp4"
    p2.write_bytes(b"valid-content")
    assert svc.create_upload_source(p2) == sid
    assert_exact_canonical(settings, db)


def test_same_bytes_different_filenames_single_canonical(tmp_path):
    """Identical bytes under different filenames collapse to one canonical file."""
    svc, settings, db = _svc(tmp_path, "diffname")
    content = b"same-bytes-" + b"s" * 512
    pa = tmp_path / "alpha.mp4"
    pb = tmp_path / "beta.mp4"
    pa.write_bytes(content)
    pb.write_bytes(content)
    sid_a = svc.create_upload_source(pa)
    row_a = db.query_one("SELECT upload_path, title FROM sources WHERE source_id=?", (sid_a,))
    assert svc.create_upload_source(pb) == sid_a
    row = db.query_one("SELECT upload_path, title FROM sources WHERE source_id=?", (sid_a,))
    assert row["upload_path"] == row_a["upload_path"]
    assert row["title"] == "alpha.mp4"  # first-seen filename preserved
    assert Path(row["upload_path"]).read_bytes() == content
    assert_exact_canonical(settings, db)


def test_stale_row_missing_file_repaired(tmp_path):
    """A row pointing at a missing file is repaired, never returned broken."""
    svc, settings, db = _svc(tmp_path, "stale-missing")
    p = tmp_path / "clip.mp4"
    content = b"stale-missing-" + b"m" * 256
    p.write_bytes(content)
    sid = source_id_for_file(p)
    bad = str(settings.paths.uploads_dir / f"{sid}-ghost.mp4")
    _seed_row(db, sid, bad, title="ghost")
    assert svc.create_upload_source(p) == sid
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"] != bad
    assert Path(row["upload_path"]).is_file()
    assert Path(row["upload_path"]).read_bytes() == content
    assert_exact_canonical(settings, db)


def test_stale_row_corrupt_file_repaired(tmp_path):
    """A row pointing at corrupt bytes is repaired in place for the same filename."""
    svc, settings, db = _svc(tmp_path, "stale-corrupt")
    p = tmp_path / "orig.mp4"
    p.write_bytes(b"corrupt-test")
    sid = svc.create_upload_source(p)
    stored = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))["upload_path"])
    stored.write_bytes(b"corrupted")
    p2 = tmp_path / "orig.mp4"
    p2.write_bytes(b"corrupt-test")
    assert svc.create_upload_source(p2) == sid
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"] == str(stored)
    assert stored.read_bytes() == b"corrupt-test"
    assert_exact_canonical(settings, db)


def test_stale_row_different_filename_repaired(tmp_path):
    """A stale row under another filename is repaired to the new canonical path."""
    svc, settings, db = _svc(tmp_path, "stale-diffname")
    p = tmp_path / "newname.mp4"
    content = b"stale-diffname-" + b"d" * 256
    p.write_bytes(content)
    sid = source_id_for_file(p)
    bad = str(settings.paths.uploads_dir / f"{sid}-oldname.mp4")
    _seed_row(db, sid, bad, title="oldname.mp4")
    assert svc.create_upload_source(p) == sid
    row = db.query_one("SELECT upload_path, title FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"] != bad
    assert Path(row["upload_path"]).is_file()
    assert Path(row["upload_path"]).read_bytes() == content
    assert row["title"] == "oldname.mp4"  # repair keeps first-seen metadata
    assert_exact_canonical(settings, db)


def test_null_upload_path_row_proceeds(tmp_path):
    svc, settings, db = _svc(tmp_path, "nullpath")
    p = tmp_path / "n.mp4"
    p.write_bytes(b"null-path-content")
    sid = source_id_for_file(p)
    db.execute(
        "INSERT INTO sources(source_id, kind, upload_path, created_at, title) VALUES(?,?,?,?,?)",
        (sid, "upload", None, time.time(), "null"),
    )
    assert svc.create_upload_source(p) == sid
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"]
    assert Path(row["upload_path"]).is_file()
    assert_exact_canonical(settings, db)


def test_insert_conflict_reconciles_to_winner(tmp_path, monkeypatch):
    """Barrier-gated INSERTs force a genuine primary-key collision at the DB boundary."""
    svc, settings, db = _svc(tmp_path, "gated")
    content = b"gated-collision-" + b"g" * 1024
    pa = tmp_path / "one.mp4"
    pb = tmp_path / "two.mp4"
    pa.write_bytes(content)
    pb.write_bytes(content)
    barrier = threading.Barrier(2)
    orig = db.execute

    def gated(sql, params=()):
        if "INSERT INTO sources" in sql:
            barrier.wait(timeout=15)
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", gated)
    out: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker(name, path):
        try:
            out[name] = svc.create_upload_source(path)
        except BaseException as exc:  # keep both threads' failures visible
            errors.append(exc)

    ta = threading.Thread(target=worker, args=("a", pa))
    tb = threading.Thread(target=worker, args=("b", pb))
    ta.start()
    tb.start()
    ta.join(timeout=20)
    tb.join(timeout=20)
    assert not errors, f"errors: {errors}"
    assert out["a"] == out["b"]
    sid = str(out["a"])
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert Path(row["upload_path"]).read_bytes() == content
    assert_exact_canonical(settings, db)


def test_unrelated_integrity_error_propagates(tmp_path, monkeypatch):
    """An integrity error with no winning row must raise, not return success."""
    svc, settings, db = _svc(tmp_path, "unrelated")
    p = tmp_path / "u.mp4"
    p.write_bytes(b"unrelated-content")
    orig = db.execute

    def fake(sql, params=()):
        if "INSERT INTO sources" in sql:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: sources.source_id")
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", fake)
    with pytest.raises(sqlite3.IntegrityError):
        svc.create_upload_source(p)
    assert db.query_one("SELECT 1 FROM sources") is None
    assert_exact_canonical(settings, db)


def test_hostile_destination_owned_by_other_source(tmp_path):
    """A staged destination claimed by another source is never deleted."""
    svc, settings, db = _svc(tmp_path, "hostile")
    p = tmp_path / "victim.mp4"
    content = b"victim-content-" + b"v" * 128
    p.write_bytes(content)
    sid = source_id_for_file(p)
    stored = settings.paths.upload_path(sid, p.name)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"foreign-media")
    _seed_row(db, "file-" + "0" * 16, str(stored), title="foreign")
    with pytest.raises(OSError, match="owned by"):
        svc.create_upload_source(p)
    assert stored.read_bytes() == b"foreign-media"
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []


def test_retire_loser_keeps_referenced_file(tmp_path):
    """The loser-cleanup guard never deletes another registered source's media."""
    svc, settings, db = _svc(tmp_path, "retire-guard")
    victim = settings.paths.uploads_dir / "victim.mp4"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"media")
    _seed_row(db, "file-" + "1" * 16, str(victim), title="victim")
    other = settings.paths.uploads_dir / "canonical.mp4"
    other.write_bytes(b"canonical")
    svc._retire_loser(victim, other)
    assert victim.is_file()
    svc._retire_loser(other, other)
    assert other.is_file()
    svc._retire_loser(tmp_path / "absent.mp4", other)  # missing path: no-op


def test_link_race_winner_valid_registers(tmp_path, monkeypatch):
    """A concurrent publication between check and link reconciles to the winner."""
    svc, settings, db = _svc(tmp_path, "link-race")
    content = b"link-race-" + b"r" * 256
    p = tmp_path / "race.mp4"
    p.write_bytes(content)

    def racy_link(a, b):
        Path(b).write_bytes(content)  # concurrent winner publishes identical bytes
        raise OSError(errno.EEXIST, "race")

    monkeypatch.setattr("yt2wiki.services.os.link", racy_link)
    sid = svc.create_upload_source(p)
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert Path(row["upload_path"]).read_bytes() == content
    assert_exact_canonical(settings, db)


def test_link_other_error_propagates(tmp_path, monkeypatch):
    """A non-race link failure propagates with no row and no orphan."""
    svc, settings, db = _svc(tmp_path, "link-err")
    p = tmp_path / "le.mp4"
    p.write_bytes(b"link-err-content")

    def fake_link(a, b):
        raise OSError(errno.EACCES, "perm")

    monkeypatch.setattr("yt2wiki.services.os.link", fake_link)
    with pytest.raises(OSError):
        svc.create_upload_source(p)
    sid = source_id_for_file(p)
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert_exact_canonical(settings, db)


def test_link_race_winner_invalid_fails(tmp_path, monkeypatch):
    """A concurrent publication of corrupt bytes fails explicitly; foreign file kept."""
    svc, settings, db = _svc(tmp_path, "link-race-bad")
    p = tmp_path / "racebad.mp4"
    p.write_bytes(b"link-race-bad-content")

    def racy_link(a, b):
        Path(b).write_bytes(b"concurrent-garbage")
        raise OSError(errno.EEXIST, "race")

    monkeypatch.setattr("yt2wiki.services.os.link", racy_link)
    with pytest.raises(OSError, match="failed to publish"):
        svc.create_upload_source(p)
    sid = source_id_for_file(p)
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    stored = settings.paths.upload_path(sid, p.name)
    assert stored.read_bytes() == b"concurrent-garbage"  # not ours: never deleted
    assert list(settings.paths.uploads_dir.glob(".tmp.*")) == []


def test_exdev_fallback_copies(tmp_path, monkeypatch):
    svc, settings, db = _svc(tmp_path, "exdev-ok")
    content = b"exdev-content-" + b"e" * 256
    p = tmp_path / "xd.mp4"
    p.write_bytes(content)

    def fake_link(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr("yt2wiki.services.os.link", fake_link)
    sid = svc.create_upload_source(p)
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert Path(row["upload_path"]).read_bytes() == content
    assert_exact_canonical(settings, db)


def test_exdev_open_race_uses_winner(tmp_path, monkeypatch):
    svc, settings, db = _svc(tmp_path, "exdev-race")
    content = b"exdev-race-" + b"x" * 128
    p = tmp_path / "xr.mp4"
    p.write_bytes(content)
    sid = source_id_for_file(p)
    stored = settings.paths.upload_path(sid, p.name)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"stale-blocking")  # invalid: passes the pre-link check as not-valid

    def fake_link(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    def fake_open(path, *_a, **_kw):
        Path(path).write_bytes(content)  # concurrent winner publishes between open and validate
        raise OSError(errno.EEXIST, "won")

    monkeypatch.setattr("yt2wiki.services.os.link", fake_link)
    monkeypatch.setattr("yt2wiki.services.os.open", fake_open)
    assert svc.create_upload_source(p) == sid
    assert_exact_canonical(settings, db)


def test_exdev_open_error_propagates(tmp_path, monkeypatch):
    svc, settings, db = _svc(tmp_path, "exdev-err")
    p = tmp_path / "xe.mp4"
    p.write_bytes(b"exdev-err-content")

    def fake_link(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr("yt2wiki.services.os.link", fake_link)

    def fake_open(*_a, **_kw):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr("yt2wiki.services.os.open", fake_open)
    with pytest.raises(OSError):
        svc.create_upload_source(p)
    sid = source_id_for_file(p)
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert_exact_canonical(settings, db)


def test_exdev_copy_failure_cleans_partial(tmp_path, monkeypatch):
    svc, settings, db = _svc(tmp_path, "exdev-partial")
    p = tmp_path / "xp.mp4"
    p.write_bytes(b"exdev-partial-content")

    def fake_link(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr("yt2wiki.services.os.link", fake_link)
    import shutil as _shutil

    def fake_copy(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(_shutil, "copyfileobj", fake_copy)
    with pytest.raises(RuntimeError, match="boom"):
        svc.create_upload_source(p)
    sid = source_id_for_file(p)
    stored = settings.paths.upload_path(sid, p.name)
    assert not stored.exists()
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert_exact_canonical(settings, db)


def test_repair_race_concurrent_fix_wins(tmp_path, monkeypatch):
    """A concurrent repair during our UPDATE wins; our staged file is retired."""
    svc, settings, db = _svc(tmp_path, "repair-race")
    content = b"repair-race-" + b"c" * 256
    p = tmp_path / "rr.mp4"
    p.write_bytes(content)
    sid = source_id_for_file(p)
    expected = file_hash(p)
    bad = str(settings.paths.uploads_dir / f"{sid}-stale.mp4")
    _seed_row(db, sid, bad, title="stale")
    winner2 = settings.paths.uploads_dir / f"{sid}-concurrent.mp4"
    winner2.write_bytes(content)
    orig = db.execute

    def fake(sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE SOURCES SET UPLOAD_PATH"):
            # A concurrent registration repairs the row first; our UPDATE then
            # matches zero rows and we must reconcile to its winner.
            orig(
                "UPDATE sources SET upload_path=?, content_hash=? WHERE source_id=?",
                (str(winner2), expected, sid),
            )
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", fake)
    assert svc.create_upload_source(p) == sid
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"] == str(winner2)
    assert_exact_canonical(settings, db)


def test_repair_race_row_deleted_fails(tmp_path, monkeypatch):
    """A row deleted mid-repair fails explicitly with no orphan staged file."""
    svc, settings, db = _svc(tmp_path, "repair-del")
    p = tmp_path / "rd.mp4"
    p.write_bytes(b"repair-deleted-content")
    sid = source_id_for_file(p)
    bad = str(settings.paths.uploads_dir / f"{sid}-stale.mp4")
    _seed_row(db, sid, bad, title="stale")
    orig = db.execute

    def fake(sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE SOURCES SET UPLOAD_PATH"):
            orig("DELETE FROM sources WHERE source_id=?", (sid,))
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", fake)
    with pytest.raises(OSError, match="missing or corrupt"):
        svc.create_upload_source(p)
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert_exact_canonical(settings, db)


def test_tmp_hash_mismatch(tmp_path, monkeypatch):
    svc, settings, db = _svc(tmp_path, "mismatch2")
    p = tmp_path / "m.mp4"
    p.write_bytes(b"mismatch-content")
    import yt2wiki.services as mod

    orig = mod.file_hash

    def fake_hash(path, **kw):
        if ".tmp." in str(path):
            return "deadbeef"
        return orig(path, **kw)

    monkeypatch.setattr(mod, "file_hash", fake_hash)
    with pytest.raises(OSError, match="hash mismatch"):
        svc.create_upload_source(p)
    sid = source_id_for_file(p)
    assert db.query_one("SELECT 1 FROM sources WHERE source_id=?", (sid,)) is None
    assert_exact_canonical(settings, db)


def test_orphaned_valid_file_adopted(tmp_path):
    """A valid file at the canonical path with no row is adopted, not recopied."""
    svc, settings, db = _svc(tmp_path, "adopt")
    content = b"adopted-" + b"a" * 128
    p = tmp_path / "ad.mp4"
    p.write_bytes(content)
    sid = source_id_for_file(p)
    stored = settings.paths.upload_path(sid, p.name)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(content)  # crash leftover: published file, row never inserted
    assert svc.create_upload_source(p) == sid
    row = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))
    assert row["upload_path"] == str(stored)
    assert_exact_canonical(settings, db)


def test_conflict_with_valid_winner_row(tmp_path, monkeypatch):
    """A lost insert against a valid winner returns the winner's canonical path."""
    svc, settings, db = _svc(tmp_path, "stored-int")
    p = tmp_path / "v.mp4"
    p.write_bytes(b"stored-valid")
    sid = svc.create_upload_source(p)
    stored = Path(db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid,))["upload_path"])
    assert stored.is_file()
    orig = db.execute
    called = {"c": 0}

    def fake(sql, params=()):
        if "INSERT INTO sources" in sql and called["c"] == 0:
            called["c"] += 1
            raise sqlite3.IntegrityError("UNIQUE")
        return orig(sql, params)

    monkeypatch.setattr(db, "execute", fake)
    p2 = tmp_path / "v.mp4"
    p2.write_bytes(b"stored-valid")
    assert svc.create_upload_source(p2) == sid
    assert_exact_canonical(settings, db)
