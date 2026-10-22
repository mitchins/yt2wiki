"""Security and provenance regressions: traversal, restored images, timestamps."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from PIL import Image

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.web.app import create_app


@pytest.fixture()
def app_env(tmp_path):
    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(settings.paths.db_path)
    db.migrate()
    return settings, db


def _seed_article(settings, db, article_id="a1", source_id="s1", markdown="# T\n\n![a](assets/f-1.jpg)\n\n*cap*\n", frame_ids=("f-1",)):
    now = time.time()
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) VALUES(?,?,?,?,?,?,?)",
                     (source_id, "youtube", "https://youtu.be/x", "T", "U", 10.0, now))
        conn.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) VALUES(?,?,?,?,?,?)",
                     ("f-1", source_id, 1.0, str(settings.paths.source_frames(source_id) / "f-1.jpg"), "scene", now))
        conn.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) VALUES(?,?,?,?,?,?)",
                     ("f-2", source_id, 2.0, str(settings.paths.source_frames(source_id) / "f-2.jpg"), "scene", now))
        # also create file for frames
        sf = settings.paths.source_frames(source_id)
        sf.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(sf / "f-1.jpg")
        Image.new("RGB", (8, 8)).save(sf / "f-2.jpg")
        conn.execute("INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, status, fingerprint, outline_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)",
                     (article_id, source_id, "j", "T", markdown, "<p>x</p>", 1, "fp", "[]", now, now))
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)", (article_id, "T", markdown))
        for fid in frame_ids:
            conn.execute("INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption, alt_text, created_at) VALUES(?,?,?,?,?,?,?)",
                         (f"{article_id}-asset-{fid}", article_id, fid, str(settings.paths.article_assets(article_id) / f"{fid}.jpg"), "cap", "", now))
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    for fid in frame_ids:
        Image.new("RGB", (8, 8)).save(assets / f"{fid}.jpg")
    return article_id


def test_traversal_frame_id_rejected(app_env):
    settings, db = app_env
    _seed_article(settings, db)
    client = TestClient(create_app(settings))
    sentinel = settings.paths.root / "outside.jpg"
    sentinel.write_bytes(b"sentinel")
    # Attempt traversal via old_frame_id (non-sensitive path, still traversal)
    r = client.post("/api/articles/a1/images/replace", json={"old_frame_id": "../../../outside", "new_frame_id": "f-2"})
    assert r.status_code == 400
    # also via remove with traversal that could resolve to sentinel
    r = client.post("/api/articles/a1/images/remove", json={"frame_id": "../../../outside"})
    assert r.status_code == 400
    # old_fid not in article
    r = client.post("/api/articles/a1/images/remove", json={"frame_id": "f-2"})
    # f-2 is not in article's markdown (only f-1), so should be 400
    assert r.status_code == 400
    # new_fid traversal
    r = client.post("/api/articles/a1/images/replace", json={"old_frame_id": "f-1", "new_frame_id": "../evil"})
    assert r.status_code == 400
    # Rejected requests must not touch the article or outside files
    assert db.query_one("SELECT version FROM articles WHERE article_id='a1'")["version"] == 1
    assert db.query("SELECT 1 FROM article_revisions WHERE article_id='a1'") == []
    assert (settings.paths.article_assets("a1") / "f-1.jpg").is_file()
    assert sentinel.is_file()


def test_missing_restored_image_after_remove(app_env):
    settings, db = app_env
    _seed_article(settings, db)
    client = TestClient(create_app(settings))
    # Remove image
    r = client.post("/api/articles/a1/images/remove", json={"frame_id": "f-1"})
    assert r.status_code == 200
    # File should still exist for history
    assert (settings.paths.article_assets("a1") / "f-1.jpg").is_file()
    # Markdown no longer has f-1
    md = db.query_one("SELECT markdown FROM articles WHERE article_id='a1'")["markdown"]
    assert "f-1.jpg" not in md
    # Restore via revision
    rev = db.query_one("SELECT revision_id FROM article_revisions WHERE article_id='a1'")["revision_id"]
    r = client.post("/api/articles/a1/revert", json={"revision_id": rev})
    assert r.status_code == 200
    md2 = db.query_one("SELECT markdown FROM articles WHERE article_id='a1'")["markdown"]
    assert "f-1.jpg" in md2
    assert "*cap*" in md2
    assert (settings.paths.article_assets("a1") / "f-1.jpg").is_file()
    # ZIP should contain the restored image
    import zipfile
    zpath = settings.paths.article_dir("a1") / "article.zip"
    assert zpath.is_file()
    names = zipfile.ZipFile(zpath).namelist()
    assert "assets/f-1.jpg" in names
    # Editor lists restored image with caption
    row = db.query_one("SELECT caption FROM article_assets WHERE article_id='a1' AND frame_id='f-1'")
    assert row is not None and row["caption"] == "cap"


def test_caption_restored_when_row_exists(app_env):
    settings, db = app_env
    _seed_article(settings, db, markdown="# T\n\n![a](assets/f-1.jpg)\n\n*cap*\n")
    client = TestClient(create_app(settings))
    # Edit caption via dedicated endpoint (updates markdown + asset row)
    r = client.post("/api/articles/a1/images/caption", json={"frame_id": "f-1", "caption": "new cap"})
    assert r.status_code == 200
    assert db.query_one("SELECT caption FROM article_assets WHERE article_id='a1' AND frame_id='f-1'")["caption"] == "new cap"
    # Get first revision (original)
    revs = db.query("SELECT revision_id, version FROM article_revisions WHERE article_id='a1' ORDER BY version")
    first_rev = revs[0]["revision_id"]
    r = client.post("/api/articles/a1/revert", json={"revision_id": first_rev})
    assert r.status_code == 200
    # Caption should be restored to original, alt_text preserved
    row = db.query_one("SELECT caption FROM article_assets WHERE article_id='a1' AND frame_id='f-1'")
    assert row["caption"] == "cap"
    md = db.query_one("SELECT markdown FROM articles WHERE article_id='a1'")["markdown"]
    assert "*cap*" in md


def test_ffmpeg_timestamp_preserved():
    from yt2wiki.v9.publish import _strip_inline_timestamp_links, assemble_markdown
    body = "Run `ffmpeg -ss 00:01:30 -i input.mp4 out.mp4` now."
    out = _strip_inline_timestamp_links(body)
    assert "00:01:30" in out
    # Also via assemble_markdown with code block
    sections = [{"section_id": "sec-01", "heading": "Do", "body": "Use ```bash\nffmpeg -ss 00:01:30 -i input.mp4 out.mp4\n``` done.", "t_start": 0, "t_end": 10}]
    md, _ = assemble_markdown("T", "S", sections, {}, {"url": "https://youtu.be/x"})
    assert "00:01:30" in md
    # Clock time outside code should also be preserved (plain)
    body2 = "The meeting is at 10:30 in the morning."
    out2 = _strip_inline_timestamp_links(body2)
    assert "10:30" in out2


def test_numerical_range_preserved():
    from yt2wiki.v9.publish import _strip_bare_float_ranges
    text = "The voltage is 3.3-5.0 V and the gap 0.5–1.5 mm."
    # All measurements are now preserved verbatim (heuristic removed)
    assert "3.3-5.0" in _strip_bare_float_ranges(text, 0, 10)
    assert "0.5–1.5" in _strip_bare_float_ranges(text, 0, 10)
    assert "3.3-5.0" in _strip_bare_float_ranges(text, 400.0, 410.0)
    ttext = "Span 340.0-349.4 covers the intro."
    # Bare float ranges are no longer stripped — preserved for safety
    assert "340.0-349.4" in _strip_bare_float_ranges(ttext, 340.0, 350.0)
    # Inline code preserved
    codetext = "Check `3.3-5.0` in code."
    assert "3.3-5.0" in _strip_bare_float_ranges(codetext, 340.0, 350.0)
    codetext2 = "```\n340.0-349.4\n```"
    assert "340.0-349.4" in _strip_bare_float_ranges(codetext2, 340.0, 350.0)


def test_inference_local_only(app_env, monkeypatch):
    settings, db = app_env
    from unittest import mock
    import httpx

    # Mock real network probe so test is offline and deterministic
    def _mock_get(*args, **kwargs):
        m = mock.Mock()
        m.status_code = 200
        m.json.return_value = {"data": [{"id": "m"}]}
        return m

    monkeypatch.setattr(httpx, "get", _mock_get)
    # Also mock post for qualification probes (writer/vision)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _mock_get())

    from fastapi.testclient import TestClient
    client = TestClient(create_app(settings))
    # Allowed from testclient (treated as local)
    r = client.post("/api/settings/inference/test", json={"url": "http://127.0.0.1:9/v1"})
    # Mocked probe returns ok, but qualification may still fail — we only care it's not 403
    assert r.status_code == 200  # not 403
    # Arbitrary env var without YT2WIKI_ prefix is blocked
    r = client.post("/api/settings/inference", json={"providers": [{"name": "x", "url": "http://1.1.1.1/v1", "api_key_env": "EVIL_KEY"}]})
    assert r.status_code == 400 and "YT2WIKI_" in r.json()["detail"]
