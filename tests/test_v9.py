"""V9 unit tests (no models, no network)."""

from __future__ import annotations

from yt2wiki.v9.writer import plan_sections, section_transcript


def _segs(n=20, step=30.0):
    return [{"idx": i, "start": i * step, "end": (i + 1) * step,
             "text": f"step {i} do the thing with sudo example-{i}"} for i in range(n)]


def test_plan_covers_whole_transcript_without_chapters():
    plan = plan_sections(_segs(), None, 600.0)
    assert plan[0]["t_start"] <= 1.0
    assert plan[-1]["t_end"] >= 599.0
    # contiguous, stable ids
    ids = [p["section_id"] for p in plan]
    assert len(set(ids)) == len(ids)
    for a, b in zip(plan, plan[1:]):
        assert b["t_start"] >= a["t_start"]


def test_plan_uses_chapters_and_merges_short_ones():
    segs = _segs(n=30, step=20.0)  # 600s
    chapters = [{"start_time": 0, "end_time": 200, "title": "Setup"},
                {"start_time": 200, "end_time": 222, "title": "Tiny"},
                {"start_time": 230, "end_time": 600, "title": "Build"}]
    plan = plan_sections(segs, chapters, 600.0)
    assert plan[0]["section_id"].endswith("setup")
    assert plan[-1]["t_end"] >= 599.0
    assert all("tiny" not in p["section_id"] for p in plan)


def test_section_transcript_bounds():
    segs = _segs()
    text = section_transcript(segs, 60.0, 120.0)
    assert "[60.0" in text and "[150.0" not in text


def test_candidates_spread_and_limit():
    from yt2wiki.v9.select import candidates_for_section
    frames = [{"frame_id": f"f-{i}", "t": float(i * 10), "path": f"/tmp/{i}.jpg",
               "kind": "scene" if i % 3 == 0 else "periodic"} for i in range(40)]
    cands = candidates_for_section(frames, 100.0, 200.0, limit=6)
    assert len(cands) <= 6
    assert all(80.0 <= c["t"] <= 220.0 for c in cands)
    assert cands == sorted(cands, key=lambda c: c["t"])


def test_assemble_markdown_places_images_with_provenance():
    from yt2wiki.v9.publish import assemble_markdown
    sections = [{"section_id": "sec-01-setup", "heading": "Setup",
                 "body": "Do X.", "t_start": 0.0, "t_end": 60.0}]
    picks = {"sec-01-setup": [{"frame_id": "f-1", "t": 12.5, "path": "/tmp/x.jpg",
                               "caption": "Terminal showing setup"}]}
    md, assets = assemble_markdown("T", "S", sections, picks,
                                   {"url": "https://www.youtube.com/watch?v=abc", "uploader": "U"})
    assert "](assets/f-1.jpg)" in md
    assert "t=12" in md
    assert assets and assets[0]["frame_id"] == "f-1"


def test_normalize_timestamps_ranges_none_targets_and_past_eof():
    from yt2wiki.v9.publish import normalize_timestamps
    md = ("See [13:00-20:00](x) and [450:00](None) and [4:500](y) and [159:0](z) "
          "and [20:00](https://youtube.com/watch?v=A&t=1) and ![a](assets/f.jpg).")
    out = normalize_timestamps(md, "https://youtube.com/watch?v=A", duration_s=524.0)
    assert "[0:13-0:20]" in out
    assert "(None)" not in out
    assert "4:500" in out and "t=4500" not in out  # ambiguous: no fabricated link
    assert "[2:39](https://youtube.com/watch?v=A&t=159)" in out
    assert "20:00" in out and "t=1200" not in out  # past EOF: plain text, no link
    assert "assets/f.jpg" in out


def test_image_remove_drops_caption_and_refreshes_exports(tmp_path):
    import json as _json
    from fastapi.testclient import TestClient
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app
    import time as _time

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("yt-test00001", "youtube", "https://youtu.be/test00001", "T", "U", 10.0, _time.time()))
    md = ("# T\n\npara\n\n![cap one](assets/f-1.jpg)\n\n*cap one*\n\n"
          "para2\n\n![cap two](assets/f-2.jpg)\n\n*cap two*\n")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, "
            "status, fingerprint, outline_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)",
            ("a9", "yt-test00001", "j", "T", md, "<p>x</p>", 1, "fp", "[]",
             _time.time(), _time.time()))
        conn.execute(
            "INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption, alt_text, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("asset-f-1", "a9", "f-1", str(tmp_path / "x.jpg"), "cap one", "", _time.time()))
    (settings.paths.article_assets("a9")).mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGB", (8, 8)).save(settings.paths.article_assets("a9") / "f-1.jpg")
    client = TestClient(create_app(settings))
    r = client.post("/api/articles/a9/images/remove", json={"frame_id": "f-1"})
    assert r.status_code == 200 and r.json()["ok"]
    row = db.query_one("SELECT markdown FROM articles WHERE article_id='a9'")
    assert "f-1.jpg" not in row["markdown"]
    assert "cap one" not in row["markdown"]
    assert "cap two" in row["markdown"]
    assert (settings.paths.article_dir("a9") / "article.zip").is_file()
    assert (settings.paths.article_dir("a9") / "article.standalone.html").is_file()


def test_v9_sections_endpoint_lists_stable_ids(tmp_path):
    import json as _json
    from fastapi.testclient import TestClient
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app
    import time as _time

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("yt-test00001", "youtube", "https://youtu.be/test00001", "T", "U", 10.0, _time.time()))
    outline = _json.dumps([{"section_id": "sec-01-intro", "heading": "Intro",
                            "t_start": 0, "t_end": 60}])
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, "
            "status, fingerprint, outline_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)",
            ("a9", "yt-test00001", "j", "T", "md", "<p>x</p>", 1, "fp", outline,
             _time.time(), _time.time()))
    client = TestClient(create_app(settings))
    r = client.get("/api/v9/articles/a9/sections")
    assert r.status_code == 200
    assert r.json()[0]["section_id"] == "sec-01-intro"


def test_text_model_class_is_importable():
    """Regression: the compact text model must resolve without optional-vision deps.

    A broken torchvision in site-packages once made Qwen3.5 unimportable; the
    local shim in .venv shadows it. This fails fast instead of mid-pipeline.
    """
    import pytest
    m = pytest.importorskip("transformers.models.qwen3_5.modeling_qwen3_5")

    assert m.Qwen3_5ForCausalLM is not None


def test_edit_refreshes_html_zip_and_standalone(tmp_path):
    from fastapi.testclient import TestClient
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app
    import time as _time

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("yt-test00001", "youtube", "https://youtu.be/test00001", "T", "U", 10.0, _time.time()))
    md = "# T\n\npara\n\n![old](assets/f-1.jpg)\n\n*old cap*\n"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, "
            "status, fingerprint, outline_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)",
            ("a9", "yt-test00001", "j", "T", md, "<p>x</p>", 1, "fp", "[]",
             _time.time(), _time.time()))
    (settings.paths.article_assets("a9")).mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGB", (8, 8)).save(settings.paths.article_assets("a9") / "f-1.jpg")
    client = TestClient(create_app(settings))
    r = client.post("/api/articles/a9", json={"markdown": "# T\n\nnew para with enough text here\n"})
    assert r.status_code == 200 and r.json()["ok"]
    html = (settings.paths.article_dir("a9") / "article.html").read_text()
    assert "new para" in html
    assert (settings.paths.article_dir("a9") / "article.zip").is_file()
    assert (settings.paths.article_dir("a9") / "article.standalone.html").is_file()


def test_replace_without_caption_neutralises_alt_and_caption(tmp_path):
    from fastapi.testclient import TestClient
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app
    import time as _time

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("yt-test00001", "youtube", "https://youtu.be/test00001", "T", "U", 10.0, _time.time()))
    md = "# T\n\n![old alt](assets/f-1.jpg)\n\n*old cap*\n"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, "
            "status, fingerprint, outline_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)",
            ("a9", "yt-test00001", "j", "T", md, "<p>x</p>", 1, "fp", "[]",
             _time.time(), _time.time()))
        conn.execute(
            "INSERT INTO frames(frame_id, source_id, t, path, kind, reason, created_at) VALUES(?,?,?,?,?,?,?)",
            ("f-2", "yt-test00001", 1.0, str(tmp_path / "f2.jpg"), "scene", "t", _time.time()))
    from PIL import Image
    (settings.paths.article_assets("a9")).mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(tmp_path / "f2.jpg")
    Image.new("RGB", (8, 8)).save(settings.paths.article_assets("a9") / "f-1.jpg")
    client = TestClient(create_app(settings))
    r = client.post("/api/articles/a9/images/replace",
                    json={"old_frame_id": "f-1", "new_frame_id": "f-2"})
    assert r.status_code == 200 and r.json()["ok"]
    row = db.query_one("SELECT markdown FROM articles WHERE article_id='a9'")
    assert "old alt" not in row["markdown"] and "old cap" not in row["markdown"]
    assert "Source frame" in row["markdown"]
