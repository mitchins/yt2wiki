"""Stage execution context and resumable job runner."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import Settings
from ..db.schema import Database, json_dumps, json_loads
from ..gpu import cuda_available, current_vram_gb, nvidia_smi_gpus, peak_vram_gb
from ..paths import Paths

log = logging.getLogger("yt2wiki.pipeline")

# Which tuning knobs actually influence each stage.  Anything outside a stage's
# set must not invalidate its cached output, otherwise a change to the
# contradiction prompt would retranscribe every video in the library.
STAGE_TUNING_KEYS: dict[str, tuple[str, ...]] = {
    "acquisition": (),
    "inspect": ("frame_width",),
    "transcribe": ("asr_language", "asr_word_timestamps", "asr_vad_filter",
                   "asr_condition_on_previous_text"),
    "frame_candidates": ("scene_threshold", "periodic_interval_s", "frame_min_gap_s",
                         "keyframe_budget", "frame_width", "frame_quality",
                         "screen_change_detection", "transcript_anchors"),
    "visual_interpret": ("vision_image_tokens", "vision_max_new_tokens", "vision_batch",
                         "vision_temperature", "vision_dense_text_pass"),
    "evidence": ("text_window_s", "text_max_tokens", "text_temperature",
                 "enable_contradiction_pass", "evidence_use_frames"),
    "procedures": ("text_max_tokens", "text_temperature", "procedure_digest_limit"),
    "compose": ("text_max_tokens", "text_temperature", "artifact_hashes"),
    "verify": ("verify_max_tokens", "enable_llm_verification",
               "enable_rule_verification"),
    "publish": (),
}

STAGES: tuple[str, ...] = (
    "acquisition",
    "inspect",
    "transcribe",
    "frame_candidates",
    "visual_interpret",
    "evidence",
    "procedures",
    "compose",
    "verify",
    "publish",
)


class CancelRequested(RuntimeError):
    pass


@dataclass
class StageContext:
    job_id: str
    source_id: str
    settings: Settings
    db: Database
    paths: Paths
    fingerprint: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    _cancelled: Callable[[], bool] | None = None

    # ------------------------------------------------------------------
    @property
    def source_dir(self) -> Path:
        return self.paths.source_dir(self.source_id)

    @property
    def frames_dir(self) -> Path:
        return self.paths.source_frames(self.source_id)

    def video_path(self) -> Path | None:
        p = self.paths.source_video(self.source_id)
        return p if p.exists() else None

    def audio_path(self) -> Path | None:
        p = self.paths.source_audio(self.source_id)
        return p if p.exists() else None

    def work_dir(self, stage: str) -> Path:
        d = self.paths.job_stage_dir(self.job_id, stage)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def meta(self) -> dict[str, Any]:
        row = self.db.query_one("SELECT meta_json FROM sources WHERE source_id=?", (self.source_id,))
        return json_loads(row["meta_json"] if row else None, {}) or {}

    def update_meta(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT meta_json FROM sources WHERE source_id=?", (self.source_id,)
            ).fetchone()
            data = json_loads(row["meta_json"] if row else None, {}) or {}
            data.update(patch)
            conn.execute(
                "UPDATE sources SET meta_json=? WHERE source_id=?", (json_dumps(data), self.source_id)
            )
        return data

    def config(self, key: str, default: Any = None) -> Any:
        return self.artifacts.get(key, default)

    def stage_fingerprint(self, stage: str, extra: dict[str, Any] | None = None) -> str:
        """Cache key for a stage: upstream artifacts + relevant config."""
        tuning = self.settings.tuning.fingerprint_dict()
        keys = [k for k in STAGE_TUNING_KEYS.get(stage, ()) if k != "artifact_hashes"]
        relevant = {k: tuning[k] for k in keys if k in tuning}
        artifact_dependent = "artifact_hashes" in STAGE_TUNING_KEYS.get(stage, ())
        payload = {
            "stage": stage,
            "profile": self.settings.profile,
            "models": {r: self.settings.model(r).repo + "@" + self.settings.model(r).revision
                       + ":" + json.dumps(self.settings.model(r).variant(self.settings.profile),
                                          sort_keys=True, default=str)
                       for r in ("asr", "vision", "text", "verify")},
            "tuning": relevant,
            "extra": extra or {},
            "inputs": self.artifacts.get("_input_hashes", {}),
            "artifacts": self.artifacts.get("_artifact_hashes", {}) if artifact_dependent else {},
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def record_artifact_hash(self, key: str, path: Path | str | None) -> str:
        """Record the content hash of a stage's own primary artifact.

        This is what makes a downstream stage's fingerprint depend on upstream
        *content* rather than just on configuration: without it, regenerating a
        procedure leaves compose believing its cached article is still valid.
        """
        hashes = self.artifacts.setdefault("_artifact_hashes", {})
        if path is None:
            hashes.pop(key, None)
            return ""
        p = Path(path)
        if p.is_file():
            from ..paths import file_hash

            digest = file_hash(p)
        else:
            digest = "missing"
        hashes[key] = digest
        return digest

    def record_input_hash(self, key: str, path: Path | str | None) -> str:
        hashes = self.artifacts.setdefault("_input_hashes", {})
        if path is None:
            hashes.pop(key, None)
            return ""
        p = Path(path)
        if p.is_file():
            from ..paths import file_hash

            h = file_hash(p)
        else:
            h = "missing"
        hashes[key] = h
        return h

    # ------------------------------------------------------------------
    def check_cancel(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise CancelRequested("job cancelled by user")

    def progress(self, message: str, *, fraction: float | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET stage=?, updated_at=? WHERE job_id=?",
                (self.current_stage, time.time(), self.job_id),
            )
        self.db.log("info", message, job_id=self.job_id, source_id=self.source_id)

    current_stage = ""


@dataclass
class StageResult:
    ok: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)


class Stage:
    """Base class: subclasses implement :meth:`run`."""

    name: str = "?"
    index: int = 0

    def run(self, ctx: StageContext) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fingerprint_extra(self, ctx: StageContext) -> dict[str, Any]:
        """Extra inputs that must invalidate this stage's cache."""
        return {}

    def cached(self, ctx: StageContext) -> bool:
        """Return True when the stage output already exists and is valid."""
        return False


def load_stage_functions() -> dict[str, tuple[int, Callable[[StageContext], dict[str, Any]]]]:
    from . import stages as _stages

    order = list(STAGES)
    return {
        name: (i + 1, mod.run)
        for i, (name, mod) in enumerate(
            (name, _stages.STAGE_MODULES[name]) for name in order
        )
    }


def run_job(
    job_id: str,
    db: Database,
    settings: Settings,
    *,
    only: Iterable[str] | None = None,
    force: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute (or resume) a job.  Returns a summary dict."""

    row = db.query_one(
        "SELECT j.*, s.source_id FROM jobs j JOIN sources s ON s.source_id=j.source_id WHERE j.job_id=?",
        (job_id,),
    )
    if row is None:
        raise KeyError(f"job {job_id} not found")
    source_id = row["source_id"]
    config = json_loads(row["config_json"], {}) or {}
    fingerprint = row["fingerprint"]

    wanted = set(only) if only else set(STAGES)
    db.execute("UPDATE jobs SET state='running', updated_at=? WHERE job_id=?", (time.time(), job_id))
    db.log("info", "job started", job_id=job_id, source_id=source_id)

    ctx = StageContext(
        job_id=job_id,
        source_id=source_id,
        settings=settings,
        db=db,
        paths=settings.paths,
        fingerprint=fingerprint,
        _cancelled=cancel_check,
    )

    registry = load_stage_functions()
    summary: dict[str, Any] = {"job_id": job_id, "stages": {}}
    errors: list[str] = []
    start_all = time.time()

    for stage_name in STAGES:
        idx, fn = registry[stage_name]
        stage_row = db.query_one(
            "SELECT * FROM stages WHERE job_id=? AND stage=?", (job_id, stage_name)
        )
        cached = (
            not force
            and stage_row is not None
            and stage_row["state"] == "done"
            and stage_row["artifacts_json"]
            and stage_row["fingerprint"] == ctx.stage_fingerprint(stage_name)
        )
        if cached:
            # Load artifacts for *every* completed stage, not just the ones this
            # invocation was asked to run: the input-hash chain is what makes a
            # downstream stage's fingerprint stable across partial runs.
            ctx.artifacts.update(json_loads(stage_row["artifacts_json"], {}) or {})
        if stage_name not in wanted:
            continue
        if cached:
            summary["stages"][stage_name] = {
                "state": "cached",
                "metrics": json_loads(stage_row["metrics_json"], {}),
            }
            continue

        ctx.current_stage = stage_name
        db.execute(
            """INSERT INTO stages(job_id, stage, state, attempts, started_at, fingerprint)
               VALUES(?,?, 'running', COALESCE((SELECT attempts FROM stages WHERE job_id=? AND stage=?),0)+1, ?, ?)
               ON CONFLICT(job_id, stage) DO UPDATE SET
                 state='running', attempts=attempts+1, started_at=excluded.started_at,
                 finished_at=NULL, error=NULL, metrics_json=NULL,
                 fingerprint=excluded.fingerprint""",
            (job_id, stage_name, job_id, stage_name, time.time(), ctx.stage_fingerprint(stage_name)),
        )
        db.execute(
            "UPDATE jobs SET stage=?, stage_index=?, updated_at=? WHERE job_id=?",
            (stage_name, idx, time.time(), job_id),
        )
        started = time.time()
        try:
            ctx.check_cancel()
            from ..gpu import reset_peak

            reset_peak()
            metrics = fn(ctx) or {}
            metrics["stage_wall_s"] = round(time.time() - started, 3)
            metrics["peak_vram_gb"] = round(peak_vram_gb(), 3)
            metrics["vram_now_gb"] = round(current_vram_gb(), 3)
            metrics["stage_fingerprint"] = ctx.stage_fingerprint(stage_name)
            artifacts = {k: v for k, v in ctx.artifacts.items()}
            db.execute(
                """UPDATE stages SET state='done', finished_at=?, duration_s=?,
                          artifacts_json=?, metrics_json=?, error=NULL WHERE job_id=? AND stage=?""",
                (
                    time.time(), time.time() - started, json_dumps(artifacts),
                    json_dumps(metrics), job_id, stage_name,
                ),
            )
            db.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (time.time(), job_id))
            summary["stages"][stage_name] = {"state": "done", "metrics": metrics}
            db.log("info", f"stage {stage_name} done in {metrics['stage_wall_s']}s", job_id=job_id)
        except CancelRequested as exc:
            db.execute(
                "UPDATE stages SET state='failed', error=?, finished_at=? WHERE job_id=? AND stage=?",
                ("cancelled", time.time(), job_id, stage_name),
            )
            db.execute("UPDATE jobs SET state='cancelled', error=?, updated_at=? WHERE job_id=?",
                       (str(exc), time.time(), job_id))
            summary["stages"][stage_name] = {"state": "cancelled"}
            summary["cancelled"] = True
            return summary
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            db.execute(
                "UPDATE stages SET state='failed', error=?, finished_at=? WHERE job_id=? AND stage=?",
                (f"{type(exc).__name__}: {exc}", time.time(), job_id, stage_name),
            )
            db.execute(
                "UPDATE jobs SET state='failed', error=?, updated_at=? WHERE job_id=?",
                (f"{stage_name}: {type(exc).__name__}: {exc}", time.time(), job_id),
            )
            db.log("error", f"stage {stage_name} failed: {tb[-2500:]}", job_id=job_id, source_id=source_id)
            summary["stages"][stage_name] = {
                "state": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            errors.append(stage_name)
            break

    if errors:
        db.execute("UPDATE jobs SET state='failed', updated_at=? WHERE job_id=?", (time.time(), job_id))
        summary["ok"] = False
    elif not summary.get("cancelled"):
        db.execute("UPDATE jobs SET state='done', finished_at=?, updated_at=? WHERE job_id=?",
                   (time.time(), time.time(), job_id))
        summary["ok"] = True
    summary["wall_s"] = round(time.time() - start_all, 3)
    summary["gpu"] = cuda_available()
    gpus = nvidia_smi_gpus()
    if gpus and settings.device_index is not None:
        g = next((g for g in gpus if g.index == settings.device_index), None)
        if g:
            summary["gpu_used_gb"] = g.used_gb
            summary["gpu_name"] = g.name
    db.log("info", f"job finished ok={summary.get('ok')} in {summary['wall_s']}s", job_id=job_id)
    return summary
