"""Pipeline, database and service-layer tests (no models, no network)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from yt2wiki.db.schema import Database, json_loads
from yt2wiki.paths import source_id_for_youtube
from yt2wiki.pipeline import STAGES, StageContext, STAGE_TUNING_KEYS, run_job
from yt2wiki.services import Service

from .fixtures import SAMPLE_TRANSCRIPT, make_test_video


@pytest.fixture()
def env(tmp_path):
    from yt2wiki.config import Settings

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(tmp_path / "db" / "yt2wiki.sqlite3")
    db.migrate()
    return settings, db, Service(settings, db)


def _source(settings, db, sid="yt-test00001"):
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, "youtube", "https://youtu.be/test00001", "Test video", "chan", 10.0, time.time()),
    )
    return sid


def test_settings_and_fingerprint(tmp_path):
    from yt2wiki.config import Settings

    a = Settings.load(data_dir=tmp_path, profile="compact")
    b = Settings.load(data_dir=tmp_path, profile="compact")
    c = Settings.load(data_dir=tmp_path, profile="standard")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_fingerprint_changes_with_model(tmp_path):
    from yt2wiki.config import Settings

    base = Settings.load(data_dir=tmp_path, profile="standard")
    changed = Settings.load(data_dir=tmp_path, profile="standard",
                            model_overrides={"vision": "vl-2b"})
    assert base.fingerprint() != changed.fingerprint()
    assert changed.model("vision").repo == "Qwen/Qwen3-VL-2B-Instruct"


def test_model_override_role_validation(tmp_path):
    from yt2wiki.config import ConfigError, Settings

    with pytest.raises(ConfigError):
        Settings.load(data_dir=tmp_path, profile="standard",
                      model_overrides={"vision": "text-4b"})
    with pytest.raises(ConfigError):
        Settings.load(data_dir=tmp_path, profile="standard",
                      model_overrides={"nonsense": "text-4b"})


def test_stage_tuning_keys_reference_real_knobs(tmp_path):
    from yt2wiki.config import Settings, Tuning

    settings = Settings.load(data_dir=tmp_path, profile="standard")
    available = set(Tuning().fingerprint_dict())
    for stage, keys in STAGE_TUNING_KEYS.items():
        assert stage in STAGES
        for key in keys:
            if key == "artifact_hashes":
                continue  # marker: this stage depends on upstream artifact hashes
            assert key in available, f"{stage} references unknown tuning key {key}"


def test_schema_creates_all_objects(tmp_path):
    db = Database(tmp_path / "db" / "x.sqlite3")
    db.migrate()
    tables = {r[0] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("sources", "jobs", "stages", "transcript_segments", "frames",
                     "frame_interpretations", "evidence", "procedures", "procedure_steps",
                     "articles", "article_revisions", "findings", "model_loads",
                     "articles_fts", "evidence_fts", "transcript_fts"):
        assert expected in tables, expected


def test_fts5_search_round_trip(env):
    _, db, _ = env
    sid = _source(env[0], db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO articles(article_id, source_id, title, markdown, version, status, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("a1", sid, "Install nginx", "Install nginx 1.24.0 with apt", 1, "published",
             time.time(), time.time()),
        )
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
                     ("a1", "Install nginx", "Install nginx 1.24.0 with apt"))
    rows = db.query("SELECT article_id FROM articles_fts WHERE articles_fts MATCH ?",
                    ('nginx AND "1.24.0"',))
    assert rows and rows[0]["article_id"] == "a1"


def test_json_dumps_handles_dataclasses_and_paths(tmp_path):
    from dataclasses import dataclass

    from yt2wiki.db.schema import json_dumps
    from yt2wiki.literals import Literal

    @dataclass
    class Holder:
        path: Path
        lit: Literal

    blob = json_dumps(Holder(Path("/tmp/x"), Literal(kind="version", literal="1.2.3")))
    parsed = json.loads(blob)
    assert parsed["path"] == "/tmp/x"
    assert parsed["lit"]["literal"] == "1.2.3"


def test_service_creates_source_and_job(env):
    settings, db, service = env
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    sid = service.create_youtube_source(url)
    assert sid == source_id_for_youtube(url)
    assert service.create_youtube_source(url) == sid
    job = service.create_job(sid, profile="compact")
    row = db.query_one("SELECT * FROM jobs WHERE job_id=?", (job,))
    assert row["profile"] == "compact"
    assert row["state"] == "queued"
    assert service.get_job(job)["source_title"] == "pending acquisition"


def test_service_upload_source_is_content_addressed(env, tmp_path):
    settings, db, service = env
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes-1")
    sid1 = service.create_upload_source(f)
    sid2 = service.create_upload_source(f)
    assert sid1 == sid2
    stored = db.query_one("SELECT upload_path FROM sources WHERE source_id=?", (sid1,))
    assert Path(stored["upload_path"]).is_file()


def test_cancel_flag(env, tmp_path):
    _, db, service = env
    sid = _source(env[0], db)
    job = service.create_job(sid)
    assert not service.cancel_check(job)
    assert service.request_cancel(job) is False
    db.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job,))
    assert service.request_cancel(job)
    assert service.cancel_check(job)


# ----------------------------------------------------------------------
# stage behaviour


def _context(tmp_path, sid="yt-test00001"):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(tmp_path / "db" / "yt2wiki.sqlite3")
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, "youtube", "https://youtu.be/test00001", "Test video", "chan", 10.0, time.time()),
    )
    ctx = StageContext(job_id="job-test", source_id=sid, settings=settings, db=db,
                       paths=settings.paths, fingerprint="fp")
    return ctx, db


def test_transcript_stage_uses_supplied_manual_captions(tmp_path):
    ctx, db = _context(tmp_path)
    # stage manual captions
    caps = [
        {"start": s["start"], "end": s["end"], "text": s["text"]} for s in SAMPLE_TRANSCRIPT
    ]
    ctx.source_dir.mkdir(parents=True, exist_ok=True)
    ctx.source_dir.mkdir(parents=True, exist_ok=True)
    (ctx.source_dir / "captions.srt.json").write_text(json.dumps(caps), encoding="utf-8")
    meta = ctx.meta()
    meta["subtitle"] = {"lang": "en", "auto": False, "captions": len(caps)}
    ctx.update_meta(meta)
    # synthesise the normalised audio so the stage can run
    video = make_test_video(ctx.paths.source_video(ctx.source_id))
    ctx.artifacts["video_path"] = str(video)
    ctx.artifacts["source_duration_s"] = 6.0
    ctx.artifacts["audio_path"] = str(ctx.paths.source_audio(ctx.source_id))

    from yt2wiki.pipeline.stages import inspect, transcribe

    ctx.current_stage = "inspect"
    inspect.run(ctx)
    ctx.current_stage = "transcribe"
    transcribe.run(ctx)

    decision = ctx.artifacts["transcript_source"]
    assert decision["mode"] == "captions"
    assert "manual captions" in decision["reason"]
    rows = db.query("SELECT * FROM transcript_segments WHERE source_id=? ORDER BY idx",
                    (ctx.source_id,))
    assert len(rows) == len(SAMPLE_TRANSCRIPT)
    assert rows[0]["text"].startswith("In this tutorial")


def test_transcript_stage_records_asr_decision_when_only_auto_captions(tmp_path):
    ctx, db = _context(tmp_path, sid="yt-test00002")
    meta = ctx.meta()
    meta["subtitle"] = {"lang": "en", "auto": True, "captions": 3}
    ctx.update_meta(meta)
    ctx.artifacts["source_duration_s"] = 20.0
    ctx.source_dir.mkdir(parents=True, exist_ok=True)
    ctx.source_dir.mkdir(parents=True, exist_ok=True)
    (ctx.source_dir / "captions.srt.json").write_text("[]", encoding="utf-8")
    from yt2wiki.pipeline.stages.transcribe import _decide

    decision = _decide({"mode": "none"}, 20.0)
    assert decision["mode"] == "asr"


def test_frame_candidates_is_bounded_and_deterministic(tmp_path):
    ctx, db = _context(tmp_path, sid="yt-test00003")
    video = make_test_video(tmp_path / "v.mp4")
    ctx.source_dir.mkdir(parents=True, exist_ok=True)
    video.replace(ctx.paths.source_video(ctx.source_id))
    ctx.artifacts["video_path"] = str(video)
    ctx.artifacts["source_duration_s"] = 6.0
    for i, s in enumerate(SAMPLE_TRANSCRIPT):
        db.execute(
            "INSERT INTO transcript_segments VALUES(?,?,?,?,?,?,?,?,?)",
            (ctx.source_id, i, s["start"], s["end"], s["text"], 0.0, 0.0, 0.0, "model"),
        )
    from yt2wiki.pipeline.stages import frame_candidates

    ctx.current_stage = "frame_candidates"
    m1 = frame_candidates.run(ctx)
    db.execute("DELETE FROM frames WHERE source_id=?", (ctx.source_id,))
    m2 = frame_candidates.run(ctx)
    assert m1["frames_extracted"] == m2["frames_extracted"]
    assert m1["frames_extracted"] <= int(ctx.settings.tuning.keyframe_budget)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM frames WHERE source_id=?",
                                        (ctx.source_id,))}
    assert "scene" in kinds
    rows = db.query("SELECT frame_id, path, t FROM frames WHERE source_id=?", (ctx.source_id,))
    for r in rows:
        assert Path(r["path"]).is_file()


def test_run_job_fails_loudly_and_records_stage_state(env):
    settings, db, service = env
    sid = _source(settings, db, sid="yt-broken001")
    job = service.create_job(sid)
    result = run_job(job, db, settings, only={"acquisition"},
                     cancel_check=lambda: False)
    assert result["ok"] is False
    stage = db.query_one("SELECT * FROM stages WHERE job_id=? AND stage=?", (job, "acquisition"))
    assert stage["state"] == "failed"
    assert stage["error"]
    assert db.query_one("SELECT * FROM jobs WHERE job_id=?", (job,))["state"] == "failed"


def test_run_job_resumes_completed_stages(env):
    settings, db, service = env
    sid = _source(settings, db, sid="yt-resume001")
    job = service.create_job(sid)
    # mark a stage complete with matching artifacts
    ctx = StageContext(job_id=job, source_id=sid, settings=settings, db=db,
                       paths=settings.paths, fingerprint=settings.fingerprint())
    fp = ctx.stage_fingerprint("acquisition")
    db.execute(
        "INSERT INTO stages(job_id, stage, state, artifacts_json, fingerprint, duration_s) "
        "VALUES(?,?, 'done', ?,?,0.1)",
        (job, "acquisition", json.dumps({"_input_hashes": {}}), fp),
    )
    result = run_job(job, db, settings, only={"acquisition"}, cancel_check=lambda: False)
    assert result["stages"]["acquisition"]["state"] == "cached"


def test_run_job_honours_stage_subset(env):
    settings, db, service = env
    sid = _source(settings, db, sid="yt-subset01")
    job = service.create_job(sid)
    result = run_job(job, db, settings, only={"inspect"}, cancel_check=lambda: False)
    assert set(result["stages"]) == {"inspect"}


# ----------------------------------------------------------------------
# GPU headroom planning


def test_headroom_prefers_a_card_that_fits(tmp_path, monkeypatch):
    from yt2wiki.backends.headroom import plan
    from yt2wiki.config import Settings
    from yt2wiki.gpu import GpuSample
    import yt2wiki.backends.headroom as hr

    settings = Settings.load(data_dir=tmp_path, profile="standard")
    gpus = [
        GpuSample(index=0, name="small-8GB", total_gb=8.0, used_gb=1.0, free_gb=7.0),
        GpuSample(index=1, name="big-16GB", total_gb=16.0, used_gb=1.0, free_gb=15.0),
    ]
    monkeypatch.setattr(hr, "cuda_available", lambda: True)
    monkeypatch.setattr(hr, "nvidia_smi_gpus", lambda: gpus)
    info = plan(settings, "vision")
    assert info.device_index == 1
    assert info.ok
    assert info.reason == "ok"

    settings_compact = Settings.load(data_dir=tmp_path, profile="compact")
    tiny = plan(settings_compact, "asr")
    # Default transcription is HTTP (zero local footprint), so headroom is
    # moot for it — the loader bypasses planning for asr-http entirely.
    assert tiny.device_index == 1
    # The explicit local model still prefers the card that fits.
    settings_local = Settings.load(data_dir=tmp_path, profile="compact",
                                   model_overrides={"asr": "whisper-large-v3"})
    local = plan(settings_local, "asr")
    assert local.device_index == 0


def test_headroom_caps_when_nothing_fits(tmp_path, monkeypatch):
    from yt2wiki.backends.headroom import plan, vram_budget_bytes
    from yt2wiki.config import Settings
    from yt2wiki.gpu import GpuSample
    import yt2wiki.backends.headroom as hr

    settings = Settings.load(data_dir=tmp_path, profile="enhanced")
    gpus = [GpuSample(index=0, name="busy-16GB", total_gb=16.0, used_gb=12.0, free_gb=4.0)]
    monkeypatch.setattr(hr, "cuda_available", lambda: True)
    monkeypatch.setattr(hr, "nvidia_smi_gpus", lambda: gpus)
    info = plan(settings, "text")
    assert info.capped_gb <= 4.0
    assert "offload" in info.reason or "capping" in info.reason
    assert vram_budget_bytes(3.5) > 0


def test_loader_reports_headroom_for_role(tmp_path, monkeypatch):
    from yt2wiki.backends.loader import ModelManager
    from yt2wiki.config import Settings
    from yt2wiki.gpu import GpuSample
    import yt2wiki.backends.headroom as hr

    gpus = [GpuSample(index=0, name="a", total_gb=8.0, used_gb=0.0, free_gb=8.0)]
    monkeypatch.setattr("yt2wiki.backends.loader.pick_device", lambda *a, **kw: ("cuda", 0))
    monkeypatch.setattr(hr, "cuda_available", lambda: True)
    monkeypatch.setattr(hr, "nvidia_smi_gpus", lambda: gpus)
    settings = Settings.load(data_dir=tmp_path, profile="compact")
    manager = ModelManager(settings)
    info = manager.headroom_for("vision")
    assert info.capped_gb > 0
    assert manager.headroom_log == []


# ----------------------------------------------------------------------
# fingerprint determinism


def test_stage_fingerprint_is_deterministic_and_input_sensitive(tmp_path):
    from yt2wiki.pipeline import StageContext

    ctx = _context(tmp_path, sid="yt-fp0000001")[0]
    a = ctx.stage_fingerprint("inspect")
    b = ctx.stage_fingerprint("inspect")
    assert a == b

    ctx.artifacts["_input_hashes"] = {"source_video": "abc123"}
    c = ctx.stage_fingerprint("inspect")
    assert c != a, "adding an input hash must change the fingerprint"

    ctx.artifacts["_input_hashes"] = {"source_video": "abc123", "audio": "def456"}
    d = ctx.stage_fingerprint("inspect")
    assert d != c


def test_stage_fingerprint_ignores_irrelevant_tuning(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db" / "t.sqlite3")
    db.migrate()
    ctx = StageContext(job_id="j", source_id="s", settings=settings, db=db,
                       paths=settings.paths, fingerprint="fp")

    before = ctx.stage_fingerprint("transcribe")
    ctx.settings.tuning.enable_contradiction_pass = False   # not a transcribe knob
    assert ctx.stage_fingerprint("transcribe") == before
    ctx.settings.tuning.asr_vad_filter = False              # a transcribe knob
    assert ctx.stage_fingerprint("transcribe") != before


def test_visual_stage_fingerprint_ignores_evidence_knobs(tmp_path):
    from yt2wiki.pipeline import StageContext

    ctx = _context(tmp_path, sid="yt-fp0000002")[0]
    before = ctx.stage_fingerprint("visual_interpret")
    ctx.settings.tuning.evidence_use_frames = False
    assert ctx.stage_fingerprint("visual_interpret") == before
    ctx.settings.tuning.vision_image_tokens = 256
    assert ctx.stage_fingerprint("visual_interpret") != before


def test_artifact_hashes_flow_into_downstream_fingerprints(tmp_path):
    """Changing a procedure must invalidate compose, not leave a stale article."""
    from yt2wiki.pipeline import StageContext

    ctx = _context(tmp_path, sid="yt-art00001")[0]
    art = ctx.work_dir("procedures") / "procedure.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text('{"steps": []}', encoding="utf-8")
    ctx.record_artifact_hash("procedure", art)
    before = ctx.stage_fingerprint("compose")
    art.write_text('{"steps": [{"text": "do the thing"}]}', encoding="utf-8")
    ctx.record_artifact_hash("procedure", art)
    after = ctx.stage_fingerprint("compose")
    assert before != after, "compose must re-run when the procedure changes"


def test_transcribe_fingerprint_ignores_procedure_artifact(tmp_path):
    from yt2wiki.pipeline import StageContext

    ctx = _context(tmp_path, sid="yt-art00002")[0]
    before = ctx.stage_fingerprint("transcribe")
    ctx.artifacts["_artifact_hashes"] = {"procedure": "abc123"}
    assert ctx.stage_fingerprint("transcribe") == before


def test_loader_retries_on_cuda_oom(monkeypatch, tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.backends import loader as L
    from yt2wiki.backends.base import BaseBackend
    from yt2wiki.backends.headroom import Headroom
    from yt2wiki.config import ModelSpec

    attempts = []

    class _Fake(BaseBackend):
        def __init__(self, tag):
            self.tag = tag
            super().__init__(spec, {}, "cuda", 0)

        def load(self):
            attempts.append(self.tag)
            if len(attempts) == 1:
                raise RuntimeError("CUDA out of memory. Tried to allocate 68 MiB")
            from yt2wiki.gpu import BackendReport

            self.report = BackendReport(
                model_key="m", repo="r", revision="main", engine="transformers",
                variant={}, device="cuda", load_seconds=1.0, peak_vram_gb=1.0,
            )
            self.model = object()
            return self.report

    spec = ModelSpec(key="fake-text", role="text", engine="transformers", repo="r")
    settings = Settings.load(data_dir=tmp_path, profile="compact")
    manager = L.ModelManager(settings)
    monkeypatch.setattr(manager, "build", lambda role, s: _Fake(f"{len(attempts)}"))
    monkeypatch.setattr(manager, "headroom_for",
                        lambda role, exclude=(): Headroom(True, int(role is not None) + 0,
                                                          1.0, 8.0, 1.0, 1.0, "ok"))
    monkeypatch.setattr(L, "free_cuda_memory", lambda: None)

    with manager.acquire("text") as backend:
        assert backend.model is not None
    assert len(attempts) == 2
    assert any(e.get("error", "").startswith("OOM on this card") for e in manager.load_log)


def test_headroom_plan_can_exclude_a_device(tmp_path, monkeypatch):
    from yt2wiki.backends.headroom import Headroom, plan
    from yt2wiki.config import Settings
    from yt2wiki.gpu import GpuSample
    import yt2wiki.backends.headroom as hr

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    gpus = [
        GpuSample(index=0, name="a", total_gb=8.0, used_gb=0.0, free_gb=8.0),
        GpuSample(index=1, name="b", total_gb=16.0, used_gb=0.0, free_gb=16.0),
    ]
    monkeypatch.setattr(hr, "cuda_available", lambda: True)
    monkeypatch.setattr(hr, "nvidia_smi_gpus", lambda: gpus)
    info = plan(settings, "vision", exclude=[0])
    assert info.device_index == 1
    info = plan(settings, "vision", exclude=[0, 1])
    assert info.ok
