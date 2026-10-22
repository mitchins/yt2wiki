"""Stage 1 — acquisition.

Fetches the source video and its metadata.  Fully idempotent: an existing
``source.mp4`` with a matching content hash is never downloaded twice.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from ...acquisition import (
    AcquisitionError,
    VideoInfo,
    _best_track,
    download_video,
    probe_url,
)
from ...db.schema import json_dumps, json_loads
from ...paths import safe_slug
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.acquisition")


def run(ctx: StageContext) -> dict[str, Any]:
    row = ctx.db.query_one("SELECT * FROM sources WHERE source_id=?", (ctx.source_id,))
    if row is None:
        raise KeyError(f"source {ctx.source_id} missing")
    kind = row["kind"]
    dst = ctx.source_dir
    dst.mkdir(parents=True, exist_ok=True)

    metrics: dict[str, Any] = {"kind": kind}
    started = time.time()

    video = ctx.paths.source_video(ctx.source_id)
    if video.exists() and video.stat().st_size > 0:
        ctx.progress("source media already present; skipping download")
        metrics["downloaded"] = False
        metrics["bytes"] = video.stat().st_size
    else:
        if kind == "youtube":
            def progress(msg: str) -> None:
                ctx.progress(msg)

            url = row["origin_url"] or ""
            video, info = download_video(
                url, dst, progress=progress,
                timeout_s=int(ctx.settings.tuning.__dict__.get("acquisition_timeout_s", 3600)),
            )
            _record_info(ctx, info, video, metrics)
            metrics["downloaded"] = True
        elif kind in ("upload", "local"):
            src = Path(row["upload_path"] or "")
            if not src.is_file():
                raise AcquisitionError(f"uploaded file missing: {src}")
            shutil.move(str(src), str(video))
            from ...acquisition import probe_upload

            meta = probe_upload(video)
            meta["filename"] = Path(row["upload_path"] or "").name
            ctx.update_meta({"upload": meta, "title": meta["filename"]})
            metrics["downloaded"] = True
            metrics["bytes"] = video.stat().st_size
        else:
            raise AcquisitionError(f"unsupported source kind {kind!r}")

    # Captions from the previous run are already on disk in meta.json.
    ctx.record_input_hash("source_video", video)
    ctx.artifacts["video_path"] = str(video)
    ctx.artifacts["source_duration_s"] = _duration(ctx, video)
    metrics["duration_s"] = ctx.artifacts["source_duration_s"]
    metrics["elapsed_s"] = round(time.time() - started, 3)
    return metrics


def _duration(ctx: StageContext, video: Path) -> float:
    row = ctx.db.query_one("SELECT duration_s FROM sources WHERE source_id=?", (ctx.source_id,))
    if row and row["duration_s"]:
        return float(row["duration_s"])
    from ...media import ffprobe

    return ffprobe(video).duration_s


def _record_info(ctx: StageContext, info: VideoInfo, video: Path, metrics: dict[str, Any]) -> None:
    from ...media import ffprobe

    probe = ffprobe(video)
    prov = info.provenance()
    meta: dict[str, Any] = {
        "provenance": prov,
        "description": info.description[:8000],
        "chapters": info.chapters,
        "view_count": info.view_count,
        "ytdlp_raw": info.raw,
    }
    caps: list[dict[str, Any]] = []
    if info.subtitles:
        best = _best_track(info.subtitles)
        meta["subtitle"] = {
            "lang": best.lang,
            "auto": best.auto,
            "captions": len(best.captions),
        }
        caps = [c.to_dict() for c in best.captions]
        (ctx.source_dir / "captions.srt.json").write_text(
            json.dumps(caps, ensure_ascii=False), encoding="utf-8"
        )
    ctx.update_meta(meta)
    ctx.db.execute(
        """UPDATE sources SET title=?, uploader=?, duration_s=?, description=?,
                               chapters_json=?, video_path=?, retrieved_at=?,
                               fps=?, width=?, height=?, probe_json=?
           WHERE source_id=?""",
        (
            info.title, info.uploader, probe.duration_s or info.duration_s,
            info.description[:8000], json_dumps(info.chapters), str(video), time.time(),
            probe.fps, probe.width, probe.height, json_dumps(probe.raw), ctx.source_id,
        ),
    )
    if caps:
        ctx.artifacts["captions_path"] = str(ctx.source_dir / "captions.srt.json")
    metrics["title"] = info.title
    metrics["uploader"] = info.uploader
    metrics["chapters"] = len(info.chapters)
    metrics["captions"] = len(caps)
