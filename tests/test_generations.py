"""Provenance tests: v3 migration, immutable generations, restore, secrets."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from yt2wiki.db.schema import SCHEMA, SCHEMA_VERSION, Database
from yt2wiki.generations import (
    RECIPE_VERSION,
    build_info,
    record_generation,
    restore_generation,
    sanitize_snapshot,
)
from yt2wiki.paths import Paths


def _v3_schema_sql() -> str:
    """Reconstruct the v3 DDL: current SCHEMA minus the v4 generations block."""
    start = SCHEMA.index("-- v4: immutable per-publication generation records")
    end = SCHEMA.index("CREATE INDEX IF NOT EXISTS generations_article")
    end = SCHEMA.index(";", end) + 1
    return SCHEMA[:start] + SCHEMA[end:]


def _make_v3_db(path: Path) -> None:
    """A real v3-shaped library: schema v3 DDL, stamp 3, legacy content."""
    assert "generations" not in _v3_schema_sql()
    conn = sqlite3.connect(str(path))
    conn.executescript(_v3_schema_sql())
    t = 1700000000.0
    conn.execute("INSERT INTO sources(source_id, kind, title, content_hash, created_at)"
                 " VALUES('yt-abc', 'youtube', 'Old Title', 'ch-1', ?)", (t,))
    conn.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state,"
                 " config_json, created_at, updated_at)"
                 " VALUES('job-1', 'yt-abc', 'compact', 'fp', 'done', '{}', ?, ?)", (t, t))
    conn.execute("INSERT INTO articles(article_id, source_id, job_id, title, markdown,"
                 " html, version, status, created_at, updated_at)"
                 " VALUES('art9-yt-abc', 'yt-abc', 'job-1', 'Old Title', '# Old',"
                 " '<h1>Old</h1>', 2, 'published', ?, ?)", (t, t))
    conn.execute("INSERT INTO article_revisions(revision_id, article_id, version, markdown,"
                 " html, edit_note, origin, created_at)"
                 " VALUES('rev-1', 'art9-yt-abc', 1, '# Older', '<h1>Older</h1>',"
                 " 'kept before republishing', 'generated', ?)", (t,))
    conn.execute("INSERT INTO article_revisions(revision_id, article_id, version, markdown,"
                 " html, edit_note, origin, created_at)"
                 " VALUES('rev-2', 'art9-yt-abc', 2, '# Old', '<h1>Old</h1>',"
                 " 'edited by user', 'user', ?)", (t,))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '3')")
    conn.commit()
    conn.close()


def _article_files(paths: Paths, article_id: str, *, body: str, assets: dict[str, bytes]) -> None:
    adir = paths.article_dir(article_id)
    (adir / "assets").mkdir(parents=True, exist_ok=True)
    (adir / "article.md").write_text(body, encoding="utf-8")
    (adir / "article.html").write_text(f"<html>{body}</html>", encoding="utf-8")
    for name, data in assets.items():
        (adir / "assets" / name).write_bytes(data)


def test_v3_fixture_migrates_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    _make_v3_db(db_path)
    # Sanity: the fixture really is v3-shaped.
    raw = sqlite3.connect(str(db_path))
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    assert raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='generations'").fetchone()[0] == 0
    raw.close()

    db = Database(db_path)
    assert db.query_one(
        "SELECT value FROM meta WHERE key='schema_version'")["value"] == str(SCHEMA_VERSION)
    assert db.query_one(
        "SELECT COUNT(*) c FROM sqlite_master WHERE name='generations'")["c"] == 1
    # Legacy content is untouched and no generations are fabricated for it.
    assert db.query_one("SELECT title FROM articles")["title"] == "Old Title"
    assert db.query_one("SELECT COUNT(*) c FROM article_revisions")["c"] == 2
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0
    cols = {r["name"] for r in db.query("PRAGMA table_info(articles)")}
    assert "current_generation_id" in cols
    assert db.query_one(
        "SELECT current_generation_id FROM articles")["current_generation_id"] is None
    # Idempotent and safe on restart.
    db.migrate()
    again = Database(db_path)
    assert again.query_one("SELECT COUNT(*) c FROM article_revisions")["c"] == 2


def test_future_schema_is_refused(tmp_path):
    db = Database(tmp_path / "f.sqlite3")
    db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(RuntimeError, match="newer than this build supports"):
        Database(tmp_path / "f.sqlite3")


def _seed_source_job(db, *, source_id="yt-abc", job_id="job-1",
                     inference: dict | None = None) -> None:
    t = 1700000000.0
    db.execute("INSERT INTO sources(source_id, kind, title, content_hash, created_at)"
               " VALUES(?, 'youtube', 'T', 'ch-9', ?)", (source_id, t))
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state,"
               " config_json, created_at, updated_at) VALUES(?, ?, 'compact', 'fp',"
               " 'done', ?, ?, ?)",
               (job_id, source_id, json.dumps({"inference": inference} if inference else {}), t, t))


def _seed_article(db, article_id="art9-yt-abc", source_id="yt-abc") -> None:
    """The publisher inserts the article row before recording a generation."""
    db.execute("INSERT INTO articles(article_id, source_id, job_id, title, markdown,"
               " html, version, status, created_at, updated_at)"
               " VALUES(?, ?, 'job-1', 'T', '# T', '', 1, 'published', 1.0, 1.0)",
               (article_id, source_id))


_JOB_SNAPSHOT = {    "writer": {"provider": "hal", "url": "http://hal:8731/v1", "model": "m-w",
               "qualified_for_writer": True, "reachable": True,
               "api_key_env": "YT2WIKI_HAL_KEY", "api_key": "sekrit-should-never-store"},
    "vision": {"provider": "hal", "url": "http://hal:8731/v1", "model": "m-v"},
    "asr": {"provider": "whisper-svc", "url": "http://asr:9000", "model": "large-v3",
            "qualified_for_asr": True},
}


def test_two_generations_lineage_assets_and_no_secrets(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One",
                   assets={"f-1.jpg": b"\xff\xd8one"})

    g1 = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                           job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION,
                           settings_fingerprint="fp", profile="compact",
                           frame_ids=["f-1"])
    assert g1["operation"] == "initial" and g1["parent_generation_id"] is None

    _article_files(paths, "art9-yt-abc", body="# Two",
                   assets={"f-1.jpg": b"\xff\xd8one", "f-2.jpg": b"\xff\xd8two"})
    # A non-file entry in assets/ must be skipped, not snapshotted.
    (paths.article_assets("art9-yt-abc") / "thumb").mkdir()
    g2 = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                           job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION,
                           settings_fingerprint="fp", profile="compact",
                           frame_ids=["f-1", "f-2"])
    assert g2["operation"] == "regenerate"
    assert g2["parent_generation_id"] == g1["generation_id"]

    # The parent generation's outputs survive the child.
    g1md = paths.article_dir("art9-yt-abc") / "generations" / g1["generation_id"] / "article.md"
    assert g1md.read_text() == "# One"
    assert (g1md.parent / "assets" / "f-1.jpg").read_bytes() == b"\xff\xd8one"
    assert not (g1md.parent / "assets" / "f-2.jpg").exists()
    # Current paths track the latest generation.
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# Two"
    assert db.query_one("SELECT current_generation_id FROM articles") is not None

    row = db.query_one("SELECT * FROM generations WHERE generation_id=?",
                       (g2["generation_id"],))
    assert row["recipe"] == "v9" and int(row["recipe_version"]) == RECIPE_VERSION
    writer = json.loads(row["writer_json"])
    assert writer["provider"] == "hal" and writer["model"] == "m-w"
    assert writer["revision"] == "unknown"  # HTTP never reports a model revision
    asr = json.loads(row["asr_json"])
    assert asr["url"] == "http://asr:9000" and asr["model"] == "large-v3"
    prompts = json.loads(row["prompts_json"])
    assert set(prompts) >= {"recipe_version", "writer_section", "judge_rubric",
                            "selection_policy"}

    # No credential material anywhere in the stored rows or manifest files.
    blob = json.dumps(dict(row)) + (g1md.parent / "manifest.json").read_text()
    assert "sekrit-should-never-store" not in blob
    assert "api_key" not in blob and "YT2WIKI_HAL_KEY" not in blob
    assert row["app_version"] and row["git_commit"] and row["image_digest"]


def test_restore_generation_and_user_revisions_stay_distinct(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={"f-1.jpg": b"\xff\xd8one"})
    g1 = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                           job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION,
                           frame_ids=["f-1"])
    _article_files(paths, "art9-yt-abc", body="# Two", assets={"f-2.jpg": b"\xff\xd8two"})
    record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                      job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION,
                      frame_ids=["f-2"])

    # Restore the original generation: files come back, generations untouched.
    out = restore_generation(db, paths, g1["generation_id"])
    assert out["markdown"] == "# One"
    assert (paths.article_dir("art9-yt-abc") / "assets" / "f-1.jpg").read_bytes() == b"\xff\xd8one"
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 2

    # A human edit is a revision with origin 'user', not a generation.
    db.execute("INSERT INTO article_revisions(revision_id, article_id, version, markdown,"
               " html, edit_note, origin, created_at) VALUES('rev-u', 'art9-yt-abc', 3,"
               " ?, '', 'edited by user', 'user', ?)", (out["markdown"] + "\nedit", 1.0))
    origins = [r["origin"] for r in db.query(
        "SELECT origin FROM article_revisions WHERE article_id='art9-yt-abc'")]
    assert origins == ["user"]
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 2

    with pytest.raises(KeyError):
        restore_generation(db, paths, "gen-does-not-exist")


def test_restore_detects_missing_or_corrupted_snapshot(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    g1 = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                           job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    gdir = paths.article_dir("art9-yt-abc") / "generations" / g1["generation_id"]
    (gdir / "article.md").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="corrupted"):
        restore_generation(db, paths, g1["generation_id"])
    (gdir / "article.md").unlink()
    with pytest.raises(FileNotFoundError):
        restore_generation(db, paths, g1["generation_id"])


def test_sanitize_snapshot_drops_secrets():
    clean = sanitize_snapshot(_JOB_SNAPSHOT)
    blob = json.dumps(clean)
    assert "sekrit" not in blob and "api_key" not in blob and "YT2WIKI_HAL_KEY" not in blob
    assert clean["writer"]["provider"] == "hal"
    assert clean["asr"]["qualified_for_asr"] is True
    assert sanitize_snapshot(None) == {}
    assert sanitize_snapshot({"writer": {"api_key_env": "X"}}) == {}


def test_build_info_never_fabricates():
    info = build_info()
    assert set(info) == {"app_version", "git_commit", "image_digest"}
    assert all(isinstance(v, str) and v for v in info.values())


def test_v9_persist_records_generation(tmp_path):
    from yt2wiki.v9 import publish as v9publish

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    frame_src = tmp_path / "frame.jpg"
    frame_src.write_bytes(b"\xff\xd8fakejpg")
    picks = {"s1": [{"frame_id": "f-1", "path": str(frame_src), "t": 4.0,
                     "caption": "c", "alt_text": "a"}]}
    sections = [{"section_id": "s1", "heading": "H", "t_start": 0.0, "t_end": 10.0,
                 "body": "Do it [0:04](http://x)."}]
    settings = SimpleNamespace(paths=paths, fingerprint=lambda: "fp")
    out = v9publish.persist(
        settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
        title="T", markdown="# T\n\nDo it.", sections=sections, picks=picks,
        provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
        report={}, inference_snapshot=_JOB_SNAPSHOT)
    assert out["generation_operation"] == "initial"
    row = db.query_one("SELECT * FROM generations WHERE article_id='art9-yt-abc'")
    assert row is not None and row["recipe"] == "v9"
    assert json.loads(row["writer_json"])["provider"] == "hal"
    # Republishing keeps the parent and links the child.
    out2 = v9publish.persist(
        settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
        title="T", markdown="# T2", sections=sections, picks=picks,
        provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
        report={}, inference_snapshot=_JOB_SNAPSHOT)
    assert out2["generation_operation"] == "regenerate"
    assert db.query_one("SELECT parent_generation_id FROM generations WHERE generation_id=?",
                        (out2["generation_id"],))["parent_generation_id"] == out["generation_id"]


def test_classic_store_records_generation(tmp_path):
    from yt2wiki.article import Article, Section
    from yt2wiki.pipeline.stages import compose as _compose

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db, article_id="art-yt-abc")
    frame_src = tmp_path / "frames" / "f-9.jpg"
    frame_src.parent.mkdir(parents=True)
    frame_src.write_bytes(b"\xff\xd8fakejpg")
    db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at)"
               " VALUES('f-9', 'yt-abc', 1.0, ?, 'scene', 1.0)", (str(frame_src),))
    article = Article(source_id="yt-abc", title="T",
                      sections=[Section(heading="H", body=["see ![](assets/f-9.jpg)"])],
                      provenance={"profile": "compact", "fingerprint": "fp"})
    ctx = SimpleNamespace(db=db, paths=paths, source_id="yt-abc", job_id="job-1",
                          fingerprint="fp",
                          settings=SimpleNamespace(profile="compact",
                                                   fingerprint=lambda: "fp"),
                          inference_snapshot=None, artifacts={},
                          record_artifact_hash=lambda *a: None)
    _compose._store(ctx, article, "art-yt-abc", "see ![](assets/f-9.jpg)")
    row = db.query_one("SELECT * FROM generations WHERE article_id='art-yt-abc'")
    assert row is not None and row["recipe"] == "classic"
    assert row["operation"] == "initial"
    assert (paths.article_dir("art-yt-abc") / "generations" /
            row["generation_id"] / "article.md").read_text() == "see ![](assets/f-9.jpg)"


def test_classic_regenerate_new_image_both_generations_restore(tmp_path):
    """Regenerating with a different image preserves both snapshots exactly."""
    from yt2wiki.article import Article, Section
    from yt2wiki.generations import restore_generation
    from yt2wiki.pipeline.stages import compose as _compose

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db, article_id="art-yt-abc")
    frames = tmp_path / "frames"
    frames.mkdir(parents=True)
    (frames / "f-9.jpg").write_bytes(b"\xff\xd8nine")
    (frames / "f-10.jpg").write_bytes(b"\xff\xd8ten")
    for fid in ("f-9", "f-10"):
        db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at)"
                   " VALUES(?, 'yt-abc', 1.0, ?, 'scene', 1.0)",
                   (fid, str(frames / f"{fid}.jpg")))

    def _ctx():
        return SimpleNamespace(
            db=db, paths=paths, source_id="yt-abc", job_id="job-1",
            fingerprint="fp",
            settings=SimpleNamespace(profile="compact", fingerprint=lambda: "fp"),
            inference_snapshot=None, artifacts={},
            record_artifact_hash=lambda *a: None)

    _compose._store(
        _ctx(), Article(source_id="yt-abc", title="T",
                        sections=[Section(heading="H", body=["see ![](assets/f-9.jpg)"])],
                        provenance={"profile": "compact", "fingerprint": "fp"}),
        "art-yt-abc", "see ![](assets/f-9.jpg)")
    g1 = db.query_one(
        "SELECT generation_id FROM generations WHERE article_id='art-yt-abc'")["generation_id"]
    _compose._store(
        _ctx(), Article(source_id="yt-abc", title="T",
                        sections=[Section(heading="H", body=["see ![](assets/f-10.jpg)"])],
                        provenance={"profile": "compact", "fingerprint": "fp"}),
        "art-yt-abc", "see ![](assets/f-10.jpg)")
    rows = db.query("SELECT generation_id, parent_generation_id, operation FROM generations "
                    "WHERE article_id='art-yt-abc' ORDER BY created_at")
    assert [r["operation"] for r in rows] == ["initial", "regenerate"]
    assert rows[1]["parent_generation_id"] == g1
    g2 = rows[1]["generation_id"]
    # The new image is installed and its row recorded in the same transaction.
    # Classic keeps previously installed files (no prune); rows and files stay
    # mutually consistent.
    assert (paths.article_assets("art-yt-abc") / "f-10.jpg").read_bytes() == b"\xff\xd8ten"
    assert (paths.article_assets("art-yt-abc") / "f-9.jpg").read_bytes() == b"\xff\xd8nine"
    assert sorted(r["frame_id"] for r in db.query(
        "SELECT frame_id FROM article_assets WHERE article_id='art-yt-abc'")) == ["f-10", "f-9"]
    # Each generation restores with its own correct image and hash.
    out1 = restore_generation(db, paths, g1)
    assert out1["markdown"] == "see ![](assets/f-9.jpg)"
    assert (paths.article_assets("art-yt-abc") / "f-9.jpg").read_bytes() == b"\xff\xd8nine"
    gens = paths.article_dir("art-yt-abc") / "generations"
    assert (gens / g1 / "assets" / "f-9.jpg").read_bytes() == b"\xff\xd8nine"
    assert not (gens / g1 / "assets" / "f-10.jpg").exists()
    out2 = restore_generation(db, paths, g2)
    assert out2["markdown"] == "see ![](assets/f-10.jpg)"
    assert (paths.article_assets("art-yt-abc") / "f-10.jpg").read_bytes() == b"\xff\xd8ten"
    assert (gens / g2 / "assets" / "f-10.jpg").read_bytes() == b"\xff\xd8ten"
    assert not (gens / g2 / "assets" / "f-9.jpg").exists()


def test_build_info_fallbacks_and_digest_env(tmp_path, monkeypatch):
    import importlib.metadata as _md

    from yt2wiki import generations as _g

    monkeypatch.setattr(_md, "version", lambda *a: (_ for _ in ()).throw(Exception("no dist")))
    info = _g.build_info()
    assert info["app_version"] == "0.1.0"  # falls back to package __version__
    monkeypatch.delattr("yt2wiki.__version__", raising=False)
    monkeypatch.setattr(_md, "version", lambda *a: (_ for _ in ()).throw(Exception("no dist")))
    assert _g.build_info()["app_version"] == "unknown"
    import subprocess as _sp
    monkeypatch.setattr(_sp, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("no git")))
    assert _g.build_info()["git_commit"] == "unknown"
    monkeypatch.setenv("YT2WIKI_IMAGE_DIGEST", "sha256:abc")
    assert _g.build_info()["image_digest"] == "sha256:abc"


def test_job_snapshot_tolerates_bad_config_and_db_errors(tmp_path):
    from yt2wiki import generations as _g

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    db.execute("UPDATE jobs SET config_json='{broken' WHERE job_id='job-1'")
    assert _g._job_snapshot(db, "job-1") is None
    assert _g._job_snapshot(db, None) is None
    assert _g._job_snapshot(db, "no-such-job") is None
    broken = SimpleNamespace(query=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert _g._recorded_asr_identity(broken, "yt-abc") is None


def test_recorded_asr_identity_modes(tmp_path):
    from yt2wiki import generations as _g

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    t = 1700000000.0
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-1', 'transcribe', 'done', 'not-json', ?)", (t,))
    assert _g._recorded_asr_identity(db, "yt-abc") is None
    db.execute("UPDATE stages SET metrics_json=? WHERE job_id='job-1'",
               (json.dumps({"mode": "captions"}),))
    assert _g._recorded_asr_identity(db, "yt-abc") == {"mode": "captions"}
    db.execute("UPDATE stages SET metrics_json=? WHERE job_id='job-1'",
               (json.dumps({"mode": "asr",
                            "asr_identity": {"url": "http://asr:9000", "model": "large-v3"}}),))
    ident = _g._recorded_asr_identity(db, "yt-abc")
    assert ident["url"] == "http://asr:9000" and ident["revision"] == "unknown"


def test_asr_server_identity_wins_over_empty_snapshot_entry(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference={"asr": {"provider": "x"}})
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    t = 1700000000.0
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-1', 'transcribe', 'done', ?, ?)",
               (json.dumps({"mode": "asr",
                            "asr_identity": {"url": "http://asr:9000", "model": "large-v3"}}), t))
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    row = db.query_one("SELECT asr_json FROM generations WHERE generation_id=?",
                       (g["generation_id"],))
    assert json.loads(row["asr_json"])["url"] == "http://asr:9000"


def test_record_generation_without_job_uses_snapshot_and_hash_failure(tmp_path, monkeypatch):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id=None, recipe="v9", recipe_version=RECIPE_VERSION,
                          snapshot=_JOB_SNAPSHOT)
    assert g["operation"] == "initial"
    assert json.loads(db.query_one(
        "SELECT writer_json FROM generations WHERE generation_id=?",
        (g["generation_id"],))["writer_json"])["provider"] == "hal"
    # A sources-table failure degrades to unknown instead of failing publish.
    orig = Database.query_one
    def _boom(self, sql, params=()):
        if "FROM sources" in sql:
            raise RuntimeError("db")
        return orig(self, sql, params)
    monkeypatch.setattr(Database, "query_one", _boom)
    g2 = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                           job_id=None, recipe="classic",
                           recipe_version=RECIPE_VERSION, snapshot=_JOB_SNAPSHOT)
    assert db.query_one(
        "SELECT source_content_hash FROM generations WHERE generation_id=?",
        (g2["generation_id"],))["source_content_hash"] == "unknown"


def test_snapshot_copy_oserror_aborts_generation(tmp_path, monkeypatch):
    """A snapshot-copy OSError fails the publication with nothing recorded."""
    import shutil as _sh

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={"f-1.jpg": b"one"})
    real_copy = _sh.copy2
    calls = []
    def _boom(src, dst, *a, **k):
        calls.append(str(src))
        if len(calls) == 1:
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)
    monkeypatch.setattr(_sh, "copy2", _boom)
    with pytest.raises(OSError, match="disk full"):
        record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9",
                          recipe_version=RECIPE_VERSION)
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0
    assert list((paths.article_dir("art9-yt-abc") / "generations").iterdir()) == []
    # Canonical outputs are untouched by the failed snapshot.
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# One"


def test_prompt_hash_failure_degrades_manifest(tmp_path, monkeypatch):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    monkeypatch.setattr("yt2wiki.v9.writer.TITLE_PROMPT", None)
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    assert json.loads(db.query_one(
        "SELECT prompts_json FROM generations WHERE generation_id=?",
        (g["generation_id"],))["prompts_json"])["error"] == "prompt hash unavailable"


def test_restore_missing_and_unreadable_manifest(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION,
                          source_content_hash="pinned-hash")
    row = db.query_one("SELECT source_content_hash FROM generations WHERE generation_id=?",
                       (g["generation_id"],))
    assert row["source_content_hash"] == "pinned-hash"
    gdir = paths.article_dir("art9-yt-abc") / "generations" / g["generation_id"]
    (gdir / "manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        restore_generation(db, paths, g["generation_id"])
    (gdir / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        restore_generation(db, paths, g["generation_id"])


def test_sanitize_drops_non_dict_roles():
    assert sanitize_snapshot({"writer": "notadict", "vision": None}) == {}


def test_schema_version_garbage_and_remigrate(tmp_path):
    import sqlite3 as _s3

    from yt2wiki.db import schema as _s

    db = Database(tmp_path / "g.sqlite3")
    db.execute("UPDATE meta SET value='abc' WHERE key='schema_version'")
    reopened = Database(tmp_path / "g.sqlite3")
    assert reopened.query_one(
        "SELECT value FROM meta WHERE key='schema_version'")["value"] == str(_s.SCHEMA_VERSION)
    # Direct white-box: already-current and already-migrated paths.
    conn = reopened.connection()
    _s._migrate(conn)
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    _s._migrate(conn)  # column exists: ALTER skipped, version restamped
    assert reopened.query_one(
        "SELECT value FROM meta WHERE key='schema_version'")["value"] == str(_s.SCHEMA_VERSION)
    closed = _s3.connect(":memory:")
    closed.close()
    assert _s._table_columns(closed, "articles") == set()


def test_build_info_reads_baked_build_file(tmp_path, monkeypatch):
    from pathlib import Path as _P

    from yt2wiki import generations as _g

    orig_isfile = _P.is_file
    orig_read = _P.read_text
    monkeypatch.setattr(_P, "is_file",
                        lambda self: True if self.name == "BUILD" else orig_isfile(self))
    monkeypatch.setattr(_P, "read_text",
                        lambda self, *a, **k: "deadbee\n" if self.name == "BUILD"
                        else orig_read(self, *a, **k))
    assert _g.build_info()["git_commit"] == "deadbee"


def test_build_info_survives_unreadable_build_file(tmp_path, monkeypatch):
    import subprocess as _sp
    from pathlib import Path as _P

    from yt2wiki import generations as _g

    orig_isfile = _P.is_file
    monkeypatch.setattr(_P, "is_file",
                        lambda self: True if self.name == "BUILD" else orig_isfile(self))
    monkeypatch.setattr(_P, "read_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(_sp, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("no git")))
    assert _g.build_info()["git_commit"] == "unknown"


def test_recorded_asr_identity_multi_row_modes(tmp_path):
    from yt2wiki import generations as _g

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    t = 1700000000.0
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state,"
               " config_json, created_at, updated_at)"
               " VALUES('job-2', 'yt-abc', 'compact', 'fp', 'done', '{}', ?, ?)", (t, t))
    # Newest row is unusable (asr mode without an identity dict): it must be
    # skipped, and the older captions row decides.
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-2', 'transcribe', 'done', ?, ?)",
               (json.dumps({"mode": "asr"}), t + 1))
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-1', 'transcribe', 'done', ?, ?)",
               (json.dumps({"mode": "captions"}), t))
    assert _g._recorded_asr_identity(db, "yt-abc") == {"mode": "captions"}


def test_snapshot_asr_url_kept_when_recorded_matches_nothing(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    snap = {"asr": {"provider": "other", "url": "http://other:9000", "model": "m"}}
    _seed_source_job(db, inference=snap)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    t = 1700000000.0
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-1', 'transcribe', 'done', ?, ?)",
               (json.dumps({"mode": "asr",
                            "asr_identity": {"url": "http://asr:9000", "model": "large-v3"}}), t))
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    # The job-captured URL differs from the recorded one: keep the snapshot.
    assert json.loads(db.query_one(
        "SELECT asr_json FROM generations WHERE generation_id=?",
        (g["generation_id"],))["asr_json"])["url"] == "http://other:9000"


def test_record_generation_without_assets_dir(tmp_path):
    import shutil as _sh

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    _sh.rmtree(paths.article_assets("art9-yt-abc"))
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    assert g["artifacts"] == 2  # md + html only
    out = restore_generation(db, paths, g["generation_id"])
    assert out["artifacts"] == ["article.html", "article.md"]


def test_restore_list_manifest_is_unreadable(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    gdir = paths.article_dir("art9-yt-abc") / "generations" / g["generation_id"]
    (gdir / "manifest.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        restore_generation(db, paths, g["generation_id"])


def _v9_old_article(paths, article_id="art9-yt-abc"):
    """An existing published article: old markdown, HTML and one image."""
    adir = paths.article_dir(article_id)
    (adir / "assets").mkdir(parents=True, exist_ok=True)
    (adir / "article.md").write_text("# Old\n\nOld body.", encoding="utf-8")
    (adir / "article.html").write_text("<html>old</html>", encoding="utf-8")
    (adir / "assets" / "f-old.jpg").write_bytes(b"\xff\xd8old")


def _v9_new_picks(tmp_path):
    frame_src = tmp_path / "frame.jpg"
    frame_src.write_bytes(b"\xff\xd8fakejpg")
    picks = {"s1": [{"frame_id": "f-1", "path": str(frame_src),
                     "caption": "c", "alt_text": "a"}]}
    sections = [{"section_id": "s1", "heading": "H", "t_start": 0.0, "t_end": 10.0,
                 "body": "Do it."}]
    return picks, sections


def test_v9_publish_is_atomic_when_generation_fails(tmp_path, monkeypatch):
    """A generation failure must not replace current files or DB state."""
    from yt2wiki.v9 import publish as v9publish

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _v9_old_article(paths)
    picks, sections = _v9_new_picks(tmp_path)
    settings = SimpleNamespace(paths=paths, fingerprint=lambda: "fp")

    def _boom(conn, manifest, fp, prof):
        raise RuntimeError("generation insert failed")

    monkeypatch.setattr(v9publish, "insert_generation_row", _boom)
    with pytest.raises(RuntimeError, match="generation insert failed"):
        v9publish.persist(
            settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
            title="T", markdown="# T", sections=sections, picks=picks,
            provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
            report={}, inference_snapshot=_JOB_SNAPSHOT)
    # Old database and old files are both intact and mutually consistent.
    assert db.query_one(
        "SELECT markdown FROM articles WHERE article_id='art9-yt-abc'")["markdown"] == "# T"
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0
    assert db.query_one("SELECT COUNT(*) c FROM article_revisions")["c"] == 0
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# Old\n\nOld body."
    assert (paths.article_dir("art9-yt-abc") / "article.html").read_text() == "<html>old</html>"
    assert (paths.article_dir("art9-yt-abc") / "assets" / "f-old.jpg").read_bytes() == b"\xff\xd8old"
    assert not (paths.article_dir("art9-yt-abc") / "assets" / "f-1.jpg").exists()
    gdir = paths.article_dir("art9-yt-abc") / "generations"
    assert not gdir.is_dir() or list(gdir.iterdir()) == []
    assert not (paths.article_dir("art9-yt-abc") / ".staging").exists()


def test_v9_manifest_write_failure_keeps_old_state(tmp_path, monkeypatch):
    """A manifest-write failure is pre-commit: old files and rows survive."""
    from yt2wiki import generations as _g
    from yt2wiki.v9 import publish as v9publish

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _v9_old_article(paths)
    picks, sections = _v9_new_picks(tmp_path)
    settings = SimpleNamespace(paths=paths, fingerprint=lambda: "fp")
    monkeypatch.setattr(_g, "write_manifest",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")))
    with pytest.raises(OSError, match="no space"):
        v9publish.persist(
            settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
            title="T", markdown="# T", sections=sections, picks=picks,
            provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
            report={}, inference_snapshot=_JOB_SNAPSHOT)
    assert db.query_one(
        "SELECT markdown FROM articles WHERE article_id='art9-yt-abc'")["markdown"] == "# T"
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# Old\n\nOld body."
    assert (paths.article_dir("art9-yt-abc") / "assets" / "f-old.jpg").read_bytes() == b"\xff\xd8old"
    gdir = paths.article_dir("art9-yt-abc") / "generations"
    assert not gdir.is_dir() or list(gdir.iterdir()) == []


def test_v9_promote_failure_rolls_back_files(tmp_path, monkeypatch):
    """A promotion failure restores canonical files; the DB never commits."""
    import shutil as _sh

    from yt2wiki.v9 import publish as v9publish

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _v9_old_article(paths)
    picks, sections = _v9_new_picks(tmp_path)
    settings = SimpleNamespace(paths=paths, fingerprint=lambda: "fp")
    real_move = _sh.move
    calls = []
    def _flaky(src, dst, *a, **k):
        calls.append(str(dst))
        if len(calls) == 2:
            raise OSError("promote failed")
        return real_move(src, dst, *a, **k)
    monkeypatch.setattr(_sh, "move", _flaky)
    with pytest.raises(OSError, match="promote failed"):
        v9publish.persist(
            settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
            title="T", markdown="# T", sections=sections, picks=picks,
            provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
            report={}, inference_snapshot=_JOB_SNAPSHOT)
    # New files were moved aside; old files are back; DB untouched.
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# Old\n\nOld body."
    assert (paths.article_dir("art9-yt-abc") / "article.html").read_text() == "<html>old</html>"
    assert (paths.article_dir("art9-yt-abc") / "assets" / "f-old.jpg").read_bytes() == b"\xff\xd8old"
    assert db.query_one(
        "SELECT markdown FROM articles WHERE article_id='art9-yt-abc'")["markdown"] == "# T"
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0


def test_v9_publish_success_supersedes_old_files(tmp_path):
    """Control case: a clean publish replaces files, rows and generations."""
    from yt2wiki.v9 import publish as v9publish

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db, inference=_JOB_SNAPSHOT)
    _seed_article(db)
    _v9_old_article(paths)
    picks, sections = _v9_new_picks(tmp_path)
    settings = SimpleNamespace(paths=paths, fingerprint=lambda: "fp")
    out = v9publish.persist(
        settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-1",
        title="T", markdown="# T", sections=sections, picks=picks,
        provenance={"url": "http://x", "fingerprint": "fp", "profile": "compact"},
        report={}, inference_snapshot=_JOB_SNAPSHOT)
    assert out["generation_operation"] == "initial"
    assert (paths.article_dir("art9-yt-abc") / "article.md").read_text() == "# T"
    assert (paths.article_dir("art9-yt-abc") / "assets" / "f-1.jpg").is_file()
    assert not (paths.article_dir("art9-yt-abc") / "assets" / "f-old.jpg").exists()
    row = db.query_one(
        "SELECT markdown, current_generation_id FROM articles WHERE article_id='art9-yt-abc'")
    assert row["markdown"] == "# T"
    assert row["current_generation_id"] == out["generation_id"]
    assert db.query_one(
        "SELECT COUNT(*) c FROM article_assets WHERE article_id='art9-yt-abc'")["c"] == 1


def test_wrapper_cleans_snapshot_when_row_insert_fails(tmp_path, monkeypatch):
    from yt2wiki import generations as _g

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    monkeypatch.setattr(_g, "insert_generation_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no row")))
    with pytest.raises(RuntimeError):
        _g.record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                             job_id="job-1", recipe="v9",
                             recipe_version=RECIPE_VERSION)
    assert db.query_one("SELECT COUNT(*) c FROM generations")["c"] == 0
    assert list((paths.article_dir("art9-yt-abc") / "generations").iterdir()) == []


def test_recorded_asr_identity_is_allowlisted(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    _seed_source_job(db)  # no snapshot: recorded identity decides
    _seed_article(db)
    _article_files(paths, "art9-yt-abc", body="# One", assets={})
    t = 1700000000.0
    db.execute("INSERT INTO stages(job_id, stage, state, metrics_json, finished_at)"
               " VALUES('job-1', 'transcribe', 'done', ?, ?)",
               (json.dumps({"mode": "asr", "asr_identity": {
                   "url": "http://asr:9000", "model": "large-v3",
                   "api_key": "sekrit", "token": "sekrit2"}}), t))
    g = record_generation(db, paths, article_id="art9-yt-abc", source_id="yt-abc",
                          job_id="job-1", recipe="v9", recipe_version=RECIPE_VERSION)
    asr = json.loads(db.query_one(
        "SELECT asr_json FROM generations WHERE generation_id=?",
        (g["generation_id"],))["asr_json"])
    assert asr == {"url": "http://asr:9000", "model": "large-v3", "revision": "unknown"}
    blob = (paths.article_dir("art9-yt-abc") / "generations" /
            g["generation_id"] / "manifest.json").read_text()
    assert "sekrit" not in blob


def test_repo_commit_guard(tmp_path):
    from yt2wiki import generations as _g

    assert _g._repo_commit(tmp_path) == "unknown"  # no .git: never foreign HEAD
    repo_root = Path(__file__).resolve().parents[1]
    if (repo_root / ".git").is_dir():
        import subprocess as _sp
        expected = _sp.check_output(["git", "rev-parse", "--short", "HEAD"],
                                    cwd=repo_root, text=True).strip()
        assert _g._repo_commit(repo_root) == expected


def test_peek_refusal_leaves_newer_file_untouched(tmp_path):
    import sqlite3 as _s3

    p = tmp_path / "newer.sqlite3"
    conn = _s3.connect(str(p))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '999')")
    conn.commit()
    before = p.stat().st_size
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this build supports"):
        Database(p)
    assert p.stat().st_size == before
    check = _s3.connect(str(p))
    try:
        tables = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        check.close()
    assert tables == {"meta"}  # no empty v4 tables were created


def test_generation_pointer_race_is_tolerated(tmp_path, monkeypatch):
    from yt2wiki.db import schema as _s

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    assert "current_generation_id" in _s._table_columns(db.connection(), "articles")
    # Concurrent migrator won the race: duplicate column reads as done.
    monkeypatch.setattr(_s, "_table_columns", lambda conn, table: set())
    _s._ensure_generation_pointer(db.connection())  # must not raise
    # Any other OperationalError still propagates.
    import sqlite3 as _s3

    class _BadConn:
        def execute(self, *a, **k):
            raise _s3.OperationalError("disk I/O error")

    with pytest.raises(_s3.OperationalError):
        _s._ensure_generation_pointer(_BadConn())


def test_peek_version_edge_cases(tmp_path):
    import sqlite3 as _s3

    from yt2wiki.db import schema as _s

    assert _s._peek_schema_version(tmp_path / "missing.sqlite3") == 0
    no_meta = tmp_path / "empty.sqlite3"
    _s3.connect(str(no_meta)).close()
    assert _s._peek_schema_version(no_meta) == 0
    garbage = tmp_path / "garbage.sqlite3"
    conn = _s3.connect(str(garbage))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', 'abc')")
    conn.commit()
    conn.close()
    assert _s._peek_schema_version(garbage) == 0
    # Direct migration on a newer stamp still refuses (defence in depth).
    newer = _s3.connect(":memory:")
    newer.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    newer.execute("INSERT INTO meta(key, value) VALUES('schema_version', '999')")
    import pytest as _pt
    with _pt.raises(RuntimeError, match="newer than this build supports"):
        _s._migrate(newer)


def test_read_build_commit_lookup_is_isolated(tmp_path):
    from yt2wiki import generations as _g

    assert _g._read_build_commit([tmp_path / "missing"]) == "unknown"
    build = tmp_path / "BUILD"
    build.write_text("deadbee\n", encoding="utf-8")
    assert _g._read_build_commit([tmp_path / "missing", build]) == "deadbee"


def test_promoted_stage_caller_failure_restores_pruned_and_replaced(tmp_path):
    """Rollback covers replaced files and pruned stale assets alike."""
    from yt2wiki.generations import promoted_stage

    adir = tmp_path / "art"
    stage = tmp_path / "stage"
    (adir / "assets").mkdir(parents=True)
    (adir / "article.md").write_text("OLD")
    (adir / "assets" / "stale.jpg").write_bytes(b"stale")
    # Stale backup dir from an unclean run is cleared, not followed.
    (adir / ".backup-g9" / "nested").mkdir(parents=True)
    (stage / "assets").mkdir(parents=True)
    (stage / "article.md").write_text("NEW")
    (stage / "assets" / "f-1.jpg").write_bytes(b"new")
    with pytest.raises(RuntimeError, match="caller failed"), \
            promoted_stage(adir, stage, "g9", prune_assets=True):
        raise RuntimeError("caller failed")
    assert (adir / "article.md").read_text() == "OLD"
    assert (adir / "assets" / "stale.jpg").read_bytes() == b"stale"
    assert not (adir / "assets" / "f-1.jpg").exists()
    assert not (adir / ".backup-g9").exists()
    assert not stage.exists()


def test_promoted_stage_success_prunes_and_cleans_up(tmp_path):
    from yt2wiki.generations import promoted_stage

    adir = tmp_path / "art"
    stage = tmp_path / "stage"
    (adir / "assets").mkdir(parents=True)
    (adir / "article.md").write_text("OLD")
    (adir / "assets" / "stale.jpg").write_bytes(b"stale")
    (stage / "assets").mkdir(parents=True)
    (stage / "article.md").write_text("NEW")
    with promoted_stage(adir, stage, "g9", prune_assets=True):
        pass
    assert (adir / "article.md").read_text() == "NEW"
    assert not (adir / "assets" / "stale.jpg").exists()
    assert not stage.exists()
    assert not (adir / ".backup-g9").exists()


def test_promoted_stage_without_canonical_assets(tmp_path):
    from yt2wiki.generations import promoted_stage

    adir = tmp_path / "art"
    stage = tmp_path / "stage"
    adir.mkdir(parents=True)
    (adir / "article.md").write_text("OLD")
    (stage / "assets").mkdir(parents=True)
    (stage / "article.md").write_text("NEW")
    (stage / "assets" / "f-1.jpg").write_bytes(b"new")
    with promoted_stage(adir, stage, "g9", prune_assets=True):
        pass
    assert (adir / "assets" / "f-1.jpg").read_bytes() == b"new"
    # No assets anywhere: the prune scan is skipped entirely.
    adir2 = tmp_path / "art2"
    stage2 = tmp_path / "stage2"
    adir2.mkdir(parents=True)
    stage2.mkdir(parents=True)
    (adir2 / "article.md").write_text("OLD")
    (stage2 / "article.md").write_text("NEW")
    with promoted_stage(adir2, stage2, "g9", prune_assets=True):
        pass
    assert (adir2 / "article.md").read_text() == "NEW"


def test_promoted_stage_rollback_failure_is_logged(tmp_path, monkeypatch):
    """A failing restore is logged, never masks the original error."""
    import shutil as _sh
    from yt2wiki.generations import promoted_stage

    adir = tmp_path / "art"
    stage = tmp_path / "stage"
    adir.mkdir(parents=True)
    stage.mkdir(parents=True)
    (adir / "article.md").write_text("OLD")
    (stage / "article.md").write_text("NEW")
    real_move = _sh.move
    def _flaky(src, dst, *a, **k):
        if ".backup-" in str(src):
            raise OSError("restore failed")
        return real_move(src, dst, *a, **k)
    monkeypatch.setattr(_sh, "move", _flaky)
    with pytest.raises(RuntimeError, match="caller failed"), promoted_stage(adir, stage, "g9"):
        raise RuntimeError("caller failed")
    # Original error propagates; the unrestorable backup is kept and logged.
    assert (adir / ".backup-g9" / "article.md").is_file()
