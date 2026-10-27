"""V9 frame discovery: cheap full-video candidate coverage.

Reuses the frames table produced by the existing frame_candidates stage.
Guarantees coverage across the whole duration: gaps without any candidate
get an extra extracted frame so late-video sections stay eligible.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("yt2wiki.v9.frames")

MAX_GAP_S = 150.0


def _fill_gap(settings, db, source_id: str, srow: Any, a: float, b: float,
              filled: list[dict[str, Any]], frames: list[dict[str, Any]]) -> None:
    """Extract one midpoint frame into a coverage gap and register it."""
    import hashlib

    t = round((a + b) / 2, 1)
    fid = "f-" + hashlib.sha1(f"{source_id}|{t}".encode()).hexdigest()[:12]
    out = Path(srow["video_path"]).parent / f"v9-fill-{fid}.jpg"
    video = srow["video_path"]
    if not video or not Path(video).is_file():
        video = str(settings.paths.source_video(source_id))
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t),
             "-i", video, "-frames:v", "1", "-vf", "scale=1280:-1", str(out)],
            check=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        log.warning("gap fill failed at %ss: %s", t, exc)
        return
    db.execute(
        "INSERT OR IGNORE INTO frames(frame_id, source_id, t, path, kind, reason) "
        "VALUES(?,?,?,?,?,?)",
        (fid, source_id, t, str(out), "v9_gapfill", "coverage gap fill"))
    filled.append({"frame_id": fid, "t": t, "path": str(out)})
    frames.append({"frame_id": fid, "t": t, "path": str(out), "kind": "v9_gapfill",
                   "reason": "coverage gap fill"})


def _fill_gaps(settings, db, source_id: str, srow: Any, frames: list[dict[str, Any]],
               duration: float) -> list[dict[str, Any]]:
    """Coverage: fill gaps > MAX_GAP_S with one extracted frame each."""
    filled: list[dict[str, Any]] = []
    bounds = [0.0] + [float(f["t"]) for f in frames] + [duration]
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= MAX_GAP_S:
            continue
        _fill_gap(settings, db, source_id, srow, a, b, filled, frames)
    frames.sort(key=lambda r: float(r["t"]))
    return filled


def discover(settings, db, source_id: str) -> dict[str, Any]:
    started = time.time()
    frames = [dict(r) for r in db.query(
        "SELECT frame_id, t, path, kind, reason FROM frames WHERE source_id=? ORDER BY t",
        (source_id,))]
    if not frames:
        raise RuntimeError("no candidate frames; run the frame_candidates stage first")
    srow = db.query_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    duration = float(srow["duration_s"] or frames[-1]["t"])
    have_phash = True

    filled = _fill_gaps(settings, db, source_id, srow, frames, duration)

    # Near-duplicate collapse (adjacent, same phash) for ranking hygiene.
    dupes = 0
    if have_phash:
        import numpy as _np
        from PIL import Image as _Image

        def _dh(im):
            import numpy as _nn
            a = _nn.asarray(im.convert("L").resize((9, 8)), dtype=_nn.float64)
            return ((_nn.packbits((a[:, 1:] > a[:, :-1]).ravel())).tobytes()).hex()
        from PIL import Image
        prev_h, prev_i = None, None
        drop = set()
        for i, f in enumerate(frames):
            try:
                h = _dh(Image.open(f["path"]).convert("RGB"))
            except Exception:  # noqa: BLE001
                continue
            if prev_h is not None and h == prev_h and abs(float(f["t"]) - float(frames[prev_i]["t"])) < 12:
                drop.add(f["frame_id"])
                dupes += 1
            else:
                prev_h, prev_i = h, i
        frames = [f for f in frames if f["frame_id"] not in drop]

    coverage = {
        "n_candidates": len(frames),
        "gap_fills": len(filled),
        "near_dupes_collapsed": dupes,
        "span_s": round(duration, 1),
        "max_gap_s": round(max((b - a for a, b in zip(
            [0.0] + [float(f["t"]) for f in frames] + [duration],
            [float(f["t"]) for f in frames] + [duration, duration])), default=0.0), 1),
    }
    return {"frames": frames, "coverage": coverage, "elapsed_s": round(time.time() - started, 3)}
