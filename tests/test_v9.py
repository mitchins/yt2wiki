"""V9 unit tests (no models, no network)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from yt2wiki.v9.writer import plan_sections, section_transcript  # noqa: E402


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


def test_image_remove_drops_caption_and_refreshes_exports(tmp_path, operator_password):
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app

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
    from tests.conftest import login_client, post_json
    client, csrf = login_client(create_app(settings))
    r = post_json(client, csrf, "/api/articles/a9/images/remove", {"frame_id": "f-1"})
    assert r.status_code == 200 and r.json()["ok"]
    row = db.query_one("SELECT markdown FROM articles WHERE article_id='a9'")
    assert "f-1.jpg" not in row["markdown"]
    assert "cap one" not in row["markdown"]
    assert "cap two" in row["markdown"]
    assert (settings.paths.article_dir("a9") / "article.zip").is_file()
    assert (settings.paths.article_dir("a9") / "article.standalone.html").is_file()


def test_v9_sections_endpoint_lists_stable_ids(tmp_path):
    import json as _json
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app

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

    pytest.importorskip("transformers")
    import transformers.models.qwen3_5.modeling_qwen3_5 as m

    assert m.Qwen3_5ForCausalLM is not None


def test_edit_refreshes_html_zip_and_standalone(tmp_path, operator_password):
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app

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
    from tests.conftest import login_client, post_json
    client, csrf = login_client(create_app(settings))
    r = post_json(client, csrf, "/api/articles/a9", {"markdown": "# T\n\nnew para with enough text here\n"})
    assert r.status_code == 200 and r.json()["ok"]
    html = (settings.paths.article_dir("a9") / "article.html").read_text()
    assert "new para" in html
    assert (settings.paths.article_dir("a9") / "article.zip").is_file()
    assert (settings.paths.article_dir("a9") / "article.standalone.html").is_file()


def test_replace_without_caption_neutralises_alt_and_caption(tmp_path, operator_password):
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.web.app import create_app

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
    from tests.conftest import login_client, post_json
    client, csrf = login_client(create_app(settings))
    r = post_json(client, csrf, "/api/articles/a9/images/replace",
                  {"old_frame_id": "f-1", "new_frame_id": "f-2"})
    assert r.status_code == 200 and r.json()["ok"]
    row = db.query_one("SELECT markdown FROM articles WHERE article_id='a9'")
    assert "old alt" not in row["markdown"] and "old cap" not in row["markdown"]
    assert "Source frame" in row["markdown"]


def test_ensure_base_records_stage_metrics_and_artifacts(tmp_path):
    """_ensure_base must persist base-stage provenance (e.g. asr_server)."""
    import json as _json
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.v9 import run as v9run

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-v9base001"
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/v9base001", "T", "u", 10.0, _time.time()))
    job_id = "job-v9base"
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               (job_id, sid, "compact", settings.fingerprint(), "queued", "{}", _time.time(), _time.time()))

    class FakeMod:
        def __init__(self, out):
            self._out = out

        def run(self, ctx):
            ctx.artifacts["asr_server"] = "http://asr:9000 (model large-v3)"
            ctx.artifacts["asr_unsupported_options"] = ["initial_prompt"]
            return dict(self._out)

    import yt2wiki.pipeline.stages.acquisition as acq
    import yt2wiki.pipeline.stages.frame_candidates as fc
    import yt2wiki.pipeline.stages.inspect as insp
    import yt2wiki.pipeline.stages.transcribe as tr
    orig = (acq.run, insp.run, tr.run, fc.run) if hasattr(acq, "run") else None
    acq.run = lambda ctx: {"mode": "upload"}
    insp.run = lambda ctx: {"audio": "ok"}
    tr.run = FakeMod({"mode": "asr", "reason": "x"}).run
    fc.run = lambda ctx: {"frames": 3}
    try:
        from pathlib import Path as _P
        _P(settings.paths.source_video(sid)).parent.mkdir(parents=True, exist_ok=True)
        _P(settings.paths.source_video(sid)).write_bytes(b"fake")
        out = v9run._ensure_base(settings, db, None, sid, job_id=job_id)
    finally:
        acq.run, insp.run, tr.run, fc.run = orig
    assert out["reused"] is False
    row = db.query_one("SELECT metrics_json, artifacts_json FROM stages WHERE job_id=? AND stage='transcribe'",
                       (job_id,))
    metrics = _json.loads(row["metrics_json"])
    artifacts = _json.loads(row["artifacts_json"])
    assert metrics["mode"] == "asr"
    assert artifacts["asr_server"].startswith("http://asr:9000")
    assert "initial_prompt" in artifacts["asr_unsupported_options"]


def test_cite_rule_uses_real_url_or_plain_brackets():
    from yt2wiki.v9.writer import SECTION_PROMPT, cite_rule
    up = cite_rule(None)
    assert "[0:04]" in up and "no source URL" in up and "http" not in up
    yt = cite_rule("https://www.youtube.com/watch?v=abc&t=1")
    assert "watch?v=abc" in yt and "[2:39]" in yt
    # both render into the prompt without leftover placeholders
    assert "{cite_rule}" not in SECTION_PROMPT.format(
        video_title="T", heading="H", t_start=0.0, t_end=1.0,
        transcript="[0.0-1.0] hi", cite_rule=up)


def test_assemble_markdown_upload_has_no_source_link():
    from yt2wiki.v9.publish import assemble_markdown
    sections = [{"section_id": "sec-01-x", "heading": "X",
                 "body": "Do X [0:04].", "t_start": 0.0, "t_end": 60.0}]
    md, _ = assemble_markdown("T", "S", sections, {},
                              {"url": None, "uploader": None})
    assert "Source video" not in md
    assert "no source URL" in md


def test_live_snapshot_reads_all_three_roles(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.v9 import run as v9run
    from yt2wiki.web import inference as inf

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    inf.save(settings.paths, {"providers": [
        {"name": "h", "url": "http://lan:8000/v1", "model": "m", "api_key_env": ""},
        {"name": "a", "url": "http://asr:9000", "model": "large-v3", "api_key_env": ""}],
        "roles": {"writer": "h", "vision": "h", "asr": "a"}})
    inf.record_test(settings.paths, "h", {"ok": True, "qualified_for_writer": True,
                                         "qualified_for_vision": True})
    inf.record_test(settings.paths, "a", {"ok": True, "qualified_for_asr": True})
    snap = v9run._live_snapshot(settings)
    assert snap["writer"]["provider"] == "h"
    assert snap["vision"]["qualified_for_vision"] is True
    assert snap["asr"]["url"] == "http://asr:9000"


def test_write_article_uses_cite_rule(tmp_path):
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.v9 import writer as v9writer

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-cite001"
    import json as _json
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, meta_json, created_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               (sid, "youtube", None, "T", "u", 600.0,
                _json.dumps({"chapters": [{"start_time": 0, "end_time": 30.0, "title": "Setup"},
                                           {"start_time": 30.0, "end_time": 600.0, "title": "Later"}]}),
                _time.time()))
    db.execute("INSERT INTO transcript_segments VALUES(?,?,?,?,?,?,?,?,?)",
               (sid, 0, 0.0, 5.0, "do the thing now", 0.0, 0.0, 0.0, "captions"))

    seen = {}

    class StubBackend:
        def chat(self, msgs, **kw):
            seen["prompt"] = msgs[-1]["content"]
            if "Video title" in msgs[-1]["content"] or "First transcript" in msgs[-1]["content"]:
                return '{"title": "T", "summary": "S", "kind": "procedure"}'
            # Bodies below the usable-substance minimum are rejected, so the
            # stub returns a short but substantive section here.
            return ('{"heading": "H", "body": "Do it now following each step [0:04] carefully.", '
                    '"key_points": [], "warnings": []}')

    out = v9writer.write_article(settings, db, sid, backend=StubBackend())
    assert out["sections"][0]["body"] == "Do it now following each step [0:04] carefully."
    assert "no source URL" in seen["prompt"]


def test_asr_identity_tracks_effective_server(tmp_path, monkeypatch):
    from yt2wiki.config import Settings
    from yt2wiki.web import inference as inf

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    monkeypatch.delenv("YT2WIKI_ASR_URL", raising=False)
    inf.save(settings.paths, {"providers": [{"name": "a", "url": "http://asr:9000",
                                             "model": "large-v3", "api_key_env": ""}],
                              "roles": {"asr": "a", "writer": "", "vision": ""}})
    assert settings.asr_identity() == {"url": "http://asr:9000", "model": "large-v3"}
    fp_before = settings.fingerprint()
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://other:9000")
    inf.save(settings.paths, {"providers": [], "roles": {}})
    assert settings.asr_identity()["url"] == "http://other:9000"
    assert settings.fingerprint() != fp_before


def test_ensure_base_retranscribes_on_server_change(tmp_path):
    import json as _json
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.v9 import run as v9run

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-retrans001"
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/x", "T", "u", 60.0, _time.time()))
    db.execute("INSERT INTO transcript_segments VALUES(?,?,?,?,?,?,?,?,?)",
               (sid, 0, 0.0, 1.0, "old words", 0.0, 0.0, 0.0, "remote-asr"))
    import time as _t2
    db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) VALUES(?,?,?,?,?,?)",
               ("f-1", sid, 0.5, "/tmp/f-1.jpg", "scene", _t2.time()))
    job_old = "job-old"
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               (job_old, sid, "compact", "x", "done", "{}", _time.time(), _time.time()))
    db.execute("INSERT INTO stages(job_id, stage, state, started_at, finished_at, duration_s, metrics_json, error)"
               " VALUES(?,?,?,?,?,?,?,?)",
               (job_old, "transcribe", "done", _time.time(), _time.time(), 1.0,
                _json.dumps({"mode": "asr", "asr_server": "http://old:9000 (model large-v3)",
                             "asr_identity": {"url": "http://old:9000", "model": "large-v3"}}), None))
    # same server -> reuse, no rerun
    for jid in ("job-new1", "job-new2"):
        db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (jid, sid, "compact", "x", "queued", "{}", _time.time(), _time.time()))
    out = v9run._ensure_base(settings, db, None, sid, job_id="job-new1",
                             inference_snapshot={"asr": {"url": "http://old:9000", "model": "large-v3"}})
    assert out["reused"] is True
    # changed server -> transcript wiped and base rerun attempted
    import yt2wiki.pipeline.stages.acquisition as acq
    import yt2wiki.pipeline.stages.frame_candidates as fc
    import yt2wiki.pipeline.stages.inspect as insp
    import yt2wiki.pipeline.stages.transcribe as tr
    called = []
    orig = (tr.run, acq.run, insp.run, fc.run)
    tr.run = lambda ctx: called.append(ctx) or {"mode": "asr"}
    acq.run = lambda ctx: {"mode": "upload"}
    insp.run = lambda ctx: {"audio": "ok"}
    fc.run = lambda ctx: {"frames": 1}
    try:
        out = v9run._ensure_base(settings, db, None, sid, job_id="job-new2",
                                 inference_snapshot={"asr": {"url": "http://new:9000", "model": "large-v3"}})
    finally:
        tr.run, acq.run, insp.run, fc.run = orig
    assert called, "transcribe must rerun after an ASR server change"
    assert db.query_one("SELECT COUNT(*) c FROM transcript_segments WHERE source_id=?",
                        (sid,))["c"] == 0


def test_transcribe_uses_job_snapshot_not_live_config(tmp_path, monkeypatch):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-snap001"
    import time as _time
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/x", "T", "u", 10.0, _time.time()))
    wav = tmp_path / "a.wav"
    from yt2wiki.backends.asr_http import write_sine_wav
    write_sine_wav(wav, seconds=1.0)
    ctx = StageContext(job_id="j", source_id=sid, settings=settings, db=db,
                       paths=settings.paths, fingerprint="x",
                       inference_snapshot={"asr": {"provider": "s", "url": "http://snap:9000",
                                                 "model": "m", "reachable": True,
                                                 "qualified_for_asr": True}})
    ctx.artifacts["audio_path"] = str(wav)
    seen = {}

    class CapManager:
        def __init__(self, settings, recorder=None, inference_snapshot=None):
            seen["snapshot"] = inference_snapshot

        def acquire(self, role):
            raise AssertionError("no backend expected in this unit test")

    monkeypatch.setattr("yt2wiki.pipeline.stages.transcribe.ModelManager", CapManager)
    from yt2wiki.pipeline.stages import transcribe as stage
    with __import__("pytest").raises(AssertionError):
        stage._run_asr(ctx, wav, 1.0, {"mode": "asr"}, {"title": "t", "uploader": "u"})
    assert seen["snapshot"]["asr"]["url"] == "http://snap:9000"


def test_cite_rule_joins_bare_urls_with_question_mark():
    from yt2wiki.v9.writer import cite_rule
    assert "?t=SECONDS" in cite_rule("https://youtu.be/abc")
    assert "&t=SECONDS" in cite_rule("https://www.youtube.com/watch?v=abc")
    assert "youtu.be/abc?t=159" in cite_rule("https://youtu.be/abc")


def test_assemble_markdown_non_youtube_url_has_no_link_claim():
    from yt2wiki.v9.publish import assemble_markdown
    sections = [{"section_id": "sec-01-x", "heading": "X",
                 "body": "Do X [0:04].", "t_start": 0.0, "t_end": 60.0}]
    md, _ = assemble_markdown("T", "S", sections, {},
                              {"url": "https://cdn.example.com/v.mp4", "uploader": None})
    assert "Source video" in md
    assert "no source URL" in md
    assert "link each section" not in md


def test_recorded_asr_identity_modes(tmp_path):
    import json as _json
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.v9 import run as v9run

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-ident001"
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/x", "T", "u", 10.0, _time.time()))
    assert v9run._recorded_asr_identity(db, "missing") is None
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               ("j1", sid, "compact", "x", "done", "{}", _time.time(), _time.time()))
    db.execute("INSERT INTO stages(job_id, stage, state, started_at, finished_at, duration_s, metrics_json, error)"
               " VALUES(?,?,?,?,?,?,?,?)",
               ("j1", "transcribe", "done", _time.time(), _time.time(), 1.0, "not-json", None))
    assert v9run._recorded_asr_identity(db, sid) is None
    db.execute("INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, config_json, created_at, updated_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
               ("j2", sid, "compact", "x", "done", "{}", _time.time(), _time.time()))
    assert v9run._live_snapshot(settings) is None or isinstance(v9run._live_snapshot(settings), dict)
    from unittest import mock as _mock
    with _mock.patch("yt2wiki.web.inference.load", side_effect=RuntimeError("down")):
        assert v9run._live_snapshot(settings) is None
    # captions mode reuses without an identity
    db.execute("INSERT INTO stages(job_id, stage, state, started_at, finished_at, duration_s, metrics_json, error)"
               " VALUES(?,?,?,?,?,?,?,?)",
               ("j2", "transcribe", "done", _time.time() + 1, _time.time() + 1, 1.0,
                _json.dumps({"mode": "captions"}), None))
    assert v9run._recorded_asr_identity(db, sid) == {"mode": "captions"}


def test_effective_asr_identity_snapshot_without_url(tmp_path, monkeypatch):
    from yt2wiki.config import Settings
    from yt2wiki.v9 import run as v9run

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://env:9000")
    monkeypatch.setenv("YT2WIKI_ASR_MODEL", "large-v3")
    assert v9run._effective_asr_identity(settings, {"asr": {"provider": "x"}}) == {
        "url": "http://env:9000", "model": "large-v3"}


def test_recorded_asr_identity_db_error_returns_none():
    from yt2wiki.v9 import run as v9run

    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("db gone")

    assert v9run._recorded_asr_identity(Broken(), "s") is None


def test_asr_identity_survives_inference_load_failure(tmp_path, monkeypatch):
    from unittest import mock as _mock

    from yt2wiki.config import Settings

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://env:9000")
    with _mock.patch("yt2wiki.web.inference.load", side_effect=RuntimeError("down")):
        assert settings.asr_identity()["url"] == "http://env:9000"


def test_run_source_threads_snapshot_to_transcribe(tmp_path, monkeypatch):
    """The job snapshot must reach base transcription, not just V9 stages."""
    import time as _time

    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.services import Service
    from yt2wiki.v9 import run as v9run

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    svc = Service(settings, db)
    sid = "yt-snap-e2e"
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/x", "T", "u", 60.0, _time.time()))
    snap = {"writer": {"provider": "h", "url": "http://h:8000/v1", "model": "m",
                       "api_key_env": "", "reachable": True,
                       "qualified_for_writer": True, "qualified_for_vision": False},
            "asr": {"provider": "a", "url": "http://snap-asr:9000", "model": "large-v3",
                    "api_key_env": "", "reachable": True, "qualified_for_asr": True}}
    seen = {}
    from .fixtures import make_test_video
    make_test_video(settings.paths.source_video(sid), seconds=2.0)
    real_ensure = v9run._ensure_base

    def fake_ensure(settings_, db_, service_, source_id_, job_id=None, inference_snapshot=None):
        seen["ensure_snapshot"] = inference_snapshot
        return real_ensure(settings_, db_, service_, source_id_, job_id=job_id,
                           inference_snapshot=inference_snapshot)

    monkeypatch.setattr(v9run, "_ensure_base", fake_ensure)
    monkeypatch.setattr("yt2wiki.v9.writer.write_article", lambda *a, **k: {
        "title": "T", "summary": "S", "kind": "explainer",
        "sections": [{"section_id": "sec-01-x", "heading": "X", "body": "B",
                      "t_start": 0.0, "t_end": 60.0, "key_points": [], "warnings": []}],
        "coverage": {"admitted_windows": [], "represented_windows": [],
                     "n_sections": 1, "transcript_chars": 10}, "elapsed_s": 0.1})
    monkeypatch.setattr("yt2wiki.v9.frames.discover", lambda *a, **k: {
        "frames": [{"frame_id": "f-1", "t": 1.0, "path": "/tmp/f-1.jpg", "kind": "scene"}],
        "coverage": {}})
    def _fake_adjudicate(vb, sec, *a, **k):
        return [], {"section_id": sec["section_id"], "decision": "OMIT",
                    "judge_reason": "", "rank_reason": "", "chosen": [],
                    "candidates": [], "vlm_calls": 0, "frames_inspected": 0,
                    "elapsed_s": 0.0}

    monkeypatch.setattr(v9run, "adjudicate_section_with_judge", _fake_adjudicate)
    monkeypatch.setattr("yt2wiki.v9.publish.assemble_markdown",
                        lambda *a, **k: ("# T\n", []))
    monkeypatch.setattr("yt2wiki.v9.publish.persist", lambda *a, **k: {"ok": True})

    class FakeBackend:
        spec = type("S", (), {"key": "remote-h"})()
        report = type("R", (), {"variant": {}})()
        server = "http://snap-asr:9000 (model large-v3)"
        last_unsupported = []
        last_warnings = []

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def transcribe(self, *a, **k):
            from yt2wiki.backends.asr import Transcript
            return Transcript(language="en", language_probability=None,
                              duration_s=1.0, segments=[])

    class FakeManager:
        def __init__(self, settings_, recorder=None, inference_snapshot=None):
            seen.setdefault("snapshots", []).append(inference_snapshot)

        def acquire(self, role):
            from contextlib import contextmanager

            @contextmanager
            def _ctx():
                yield FakeBackend()

            return _ctx()

    # NB: transcribe.py binds ModelManager at its own module level, while
    # run_source imports it from the loader inside the function: patch both.
    monkeypatch.setattr("yt2wiki.pipeline.stages.transcribe.ModelManager", FakeManager)
    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager", FakeManager)
    out = v9run.run_source(settings, db, svc, sid, inference_snapshot=snap,
                           enable_captions=False)
    assert out["article_id"].startswith("art9-")
    assert seen["ensure_snapshot"] == snap
    assert any(s and s.get("asr", {}).get("url") == "http://snap-asr:9000"
               for s in seen["snapshots"])
    # No snapshot and no live config: the live-snapshot fallback runs.
    out2 = v9run.run_source(settings, db, svc, sid, enable_captions=False)
    assert out2["article_id"].startswith("art9-")


def test_timestamp_url_replaces_existing_t_param():
    from yt2wiki.article import timestamp_url, timestamp_url_pattern
    assert timestamp_url("https://youtu.be/abc?t=1", 159) == "https://youtu.be/abc?t=159"
    assert timestamp_url("https://www.youtube.com/watch?v=abc&t=1", 159) == \
        "https://www.youtube.com/watch?v=abc&t=159"
    assert timestamp_url("https://www.youtube.com/watch?v=abc&list=x&t=99", 5) == \
        "https://www.youtube.com/watch?v=abc&list=x&t=5"
    assert timestamp_url("https://youtu.be/abc", 159) == "https://youtu.be/abc?t=159"
    assert timestamp_url("https://example.com/v.mp4", 159) == "https://example.com/v.mp4"
    assert timestamp_url(None, 159) is None and timestamp_url("https://youtu.be/abc", None) is None
    assert timestamp_url_pattern("https://youtu.be/abc?t=1") == "https://youtu.be/abc?t=SECONDS"
    assert timestamp_url_pattern("https://youtu.be/abc?t=1").count("t=") == 1


def test_cite_rule_never_duplicates_t_param():
    from yt2wiki.v9.writer import cite_rule
    rule = cite_rule("https://www.youtube.com/watch?v=abc&t=1")
    assert rule.count("t=") == 2  # one pattern, one example — never t=1&t=159
    assert "?t=SECONDS" in cite_rule("https://youtu.be/abc")
    assert "&t=SECONDS" in cite_rule("https://www.youtube.com/watch?v=abc")


def test_normalize_replaces_existing_t_param():
    from yt2wiki.v9.publish import normalize_timestamps
    md = normalize_timestamps("See [2:39](https://youtu.be/abc?t=1).",
                              "https://youtu.be/abc?t=1", duration_s=524.0)
    assert md.count("t=") == 1 and "t=159" in md
