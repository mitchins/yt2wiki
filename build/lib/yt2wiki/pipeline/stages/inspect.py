"""Stage 2 — media inspection and normalisation.

Produces the canonical 16 kHz mono WAV used by ASR plus a cheap 1280 px proxy
used for frame extraction.  The original download is kept untouched.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ...db.schema import json_dumps, json_loads
from ...media import MediaError, extract_audio, ffprobe, normalise_video
from .. import StageContext

log = logging.getLogger("yt2wiki.stage.inspect")


def run(ctx: StageContext) -> dict[str, Any]:
    src = Path(ctx.artifacts.get("video_path") or ctx.paths.source_video(ctx.source_id))
    if not src.is_file():
        raise MediaError(f"no source video at {src}")
    started = time.time()
    probe = ffprobe(src)
    ctx.progress(f"probed {src.name}: {probe.duration_s:.1f}s "
                 f"{probe.width or '?'}x{probe.height or '?'} video={probe.has_video}")

    audio = ctx.paths.source_audio(ctx.source_id)
    if not audio.exists() or audio.stat().st_size == 0:
        if not probe.has_audio:
            raise MediaError("source has no audio track and no ASR can be performed")
        extract_audio(src, audio)
    ctx.record_input_hash("audio", audio)

    proxy = ctx.source_dir / "proxy.mp4"
    if probe.has_video and (not proxy.exists() or proxy.stat().st_size == 0):
        normalise_video(src, proxy, width=int(ctx.settings.tuning.frame_width))

    frame_source = proxy if proxy.exists() else src

    ctx.db.execute(
        """UPDATE sources SET duration_s=?, audio_path=?, fps=?, width=?, height=?,
                              video_path=?, probe_json=? WHERE source_id=?""",
        (
            probe.duration_s, str(audio), probe.fps, probe.width, probe.height,
            str(src), json_dumps(probe.raw), ctx.source_id,
        ),
    )
    meta = ctx.meta()
    meta["inspect"] = {
        "duration_s": probe.duration_s,
        "fps": probe.fps,
        "width": probe.width,
        "height": probe.height,
        "video_codec": probe.video_codec,
        "audio_codec": probe.audio_codec,
        "has_video": probe.has_video,
        "frame_source": str(frame_source),
        "nb_frames": probe.nb_frames,
    }
    ctx.update_meta(meta)

    ctx.artifacts.update(
        {
            "video_path": str(src),
            "audio_path": str(audio),
            "frame_source": str(frame_source),
            "source_duration_s": probe.duration_s,
            "fps": probe.fps,
            "has_video": probe.has_video,
            "width": probe.width,
            "height": probe.height,
        }
    )
    return {
        "duration_s": round(probe.duration_s, 2),
        "audio_bytes": audio.stat().st_size,
        "proxy_bytes": proxy.stat().st_size if proxy.exists() else 0,
        "elapsed_s": round(time.time() - started, 3),
    }
