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
