"""Stage 4 — visual candidate generation.

Covers the *whole* timeline economically:

* hard cuts from ffmpeg's scene score,
* screen-state changes from a 1 fps low-resolution signature pass (catches
  screencasts that never hard-cut),
* periodic coverage so silent stretches still contribute,
* transcript-anchored moments (the speaker points at, clicks, types, shows),
* long-speech-gap regions, which is where silently demonstrated actions live.

The result is a bounded, deduplicated set of JPEG frames with provenance.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ...db.schema import json_dumps
from ...media import MediaError, extract_frame, scene_change_times
from ...visual import (
    FrameSignature,
    dedupe_by_signature,
    hamming,
    screen_change_scores,
    stream_signatures,
)
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.frames")

# Cue phrases that imply something is being demonstrated on screen.
TRANSCRIPT_CUES = (
    "as you can see", "you can see", "look at", "here", "this is what",
    "click", "press", "type", "enter", "run this", "run the", "open up",
    "go to", "scroll", "select", "drag", "hold", "double-click", "right-click",
    "copy this", "paste", "remember to", "notice", "shown on screen",
    "on the screen", "over here", "under the", "at the top", "in the corner",
    "value", "field", "checkbox", "drop-down", "dropdown", "tab", "menu",
    "button", "dial", "knob", "wire", "solder", "terminal", "screen",
)


def run(ctx: StageContext) -> dict[str, Any]:
    t = ctx.settings.tuning
    src = Path(ctx.artifacts.get("frame_source") or ctx.paths.source_video(ctx.source_id))
    if not src.is_file():
        raise MediaError(f"no frame source at {src}")
    started = time.time()

    duration = float(ctx.artifacts.get("source_duration_s") or 0.0)
    sigs = _signatures(ctx, src)
    ctx.artifacts["signature_count"] = len(sigs)

    candidates: list[tuple[float, str, str]] = []  # (t, kind, reason)

    # 1. hard cuts
    scenes = scene_change_times(src, threshold=t.scene_threshold, min_gap_s=t.frame_min_gap_s)
    for s in scenes:
        candidates.append((s, "scene", f"scene score > {t.scene_threshold}"))
    ctx.progress(f"detected {len(scenes)} scene changes")

    # 2. screen-state changes (skipped in the "periodic screenshots" baseline)
    changes = screen_change_scores(sigs) if t.screen_change_detection else []
    threshold = _adaptive_threshold(changes)
    picked = [
        (tm, "screen", f"screen-state change {score:.2f}")
        for tm, score in changes
        if score >= threshold
    ]
    _thin_into(candidates, picked, t.frame_min_gap_s)
    ctx.progress(f"detected {len(picked)} screen-state changes")

    # 3. periodic coverage
    periodic = _periodic_times(duration, t.periodic_interval_s)
    for p in periodic:
        candidates.append((p, "periodic", f"periodic coverage every {t.periodic_interval_s}s"))
    ctx.progress(f"periodic coverage: {len(periodic)} slots")

    # 4. transcript-anchored
    anchored = _transcript_anchored(ctx, duration) if t.transcript_anchors else []
    for a in anchored[: max(8, t.keyframe_budget // 3)]:
        candidates.append((a[0], "transcript", a[1]))
    ctx.progress(f"transcript anchors: {len(anchored)}")

    # 5. silence / speech-gap regions
    gaps = _speech_gaps(ctx, duration)
    for g in gaps:
        candidates.append((g, "silent", "audio gap - possible silent demonstration"))

    # budget + dedupe
    budget = int(t.keyframe_budget)
    candidates.sort(key=lambda c: c[0])
    kept = _apply_min_gap(candidates, t.frame_min_gap_s)
    if len(kept) > budget:
        kept = _prioritise(kept, budget, duration)
    # Deduplicate near-identical *content* (not just near-in-time frames) using
    # the perceptual hashes gathered during the signature pass.
    kept_times = dedupe_by_signature([c[0] for c in kept], sigs, max_hamming=5, sample_fps=1.0)
    kinds: dict[float, tuple[str, str]] = {}
    for c in kept:
        kinds.setdefault(c[0], (c[1], c[2]))
    final = [(t, kinds[t][0], kinds[t][1]) for t in kept_times if t in kinds]
    for t in kept_times:
        if t not in kinds:
            final.append((t, "combined", "kept"))

    _write_frames(ctx, src, final, sigs)

    metrics = {
        "scene_changes": len(scenes),
        "screen_changes": len(picked),
        "periodic": len(periodic),
        "transcript_anchors": len(anchored),
        "speech_gaps": len(gaps),
        "candidates_before_budget": len(candidates),
        "frames_extracted": len(final),
        "frame_bytes": _total_bytes(ctx),
        "elapsed_s": round(time.time() - started, 3),
    }
    ctx.artifacts["frame_count"] = len(final)
    return metrics


# ----------------------------------------------------------------------
def _signatures(ctx: StageContext, src: Path) -> list[FrameSignature]:
    sigs: list[FrameSignature] = []

    def prog(t: float) -> None:
        ctx.progress(f"scanning timeline: {t:.0f}s")

    for s in stream_signatures(src, fps=1.0, on_progress=prog):
        ctx.check_cancel()
        if not s.blank:
            sigs.append(s)
    return sigs


def _adaptive_threshold(changes: list[tuple[float, float]]) -> float:
    if not changes:
        return 1.0
    vals = sorted(c[1] for c in changes)
    p95 = vals[int(len(vals) * 0.95)]
    return max(0.02, min(0.35, p95 * 0.55))


def _periodic_times(duration: float, interval: float) -> list[float]:
    if duration <= 0 or interval <= 0:
        return []
    out: list[float] = []
    t = interval
    while t < duration - 0.5:
        out.append(round(t, 3))
        t += interval
    return out


def _transcript_anchored(ctx: StageContext, duration: float) -> list[tuple[float, str]]:
    rows = ctx.db.query(
        "SELECT start, end, text FROM transcript_segments WHERE source_id=? ORDER BY start",
        (ctx.source_id,),
    )
    hits: list[tuple[float, str]] = []
    for r in rows:
        text = (r["text"] or "").lower()
        score = sum(1 for cue in TRANSCRIPT_CUES if cue in text)
        if score >= 2:
            snippet = r["text"].strip()[:90]
            hits.append((float(r["start"]) + 0.2, f"narration demonstrates on screen: {snippet}"))
    hits.sort()
    if hits:
        merged = [hits[0]]
        for h in hits[1:]:
            if h[0] - merged[-1][0] > 2.0:
                merged.append(h)
        return merged
    return hits


def _speech_gaps(ctx: StageContext, duration: float) -> list[float]:
    rows = ctx.db.query(
        "SELECT start, end FROM transcript_segments WHERE source_id=? ORDER BY start", (ctx.source_id,)
    )
    gaps: list[float] = []
    cursor = 0.0
    for r in rows:
        s, e = float(r["start"]), float(r["end"])
        if s - cursor > 6.0:
            gaps.append(round(cursor + (s - cursor) * 0.5, 3))
        cursor = max(cursor, e)
    if duration - cursor > 6.0:
        gaps.append(round(cursor + (duration - cursor) * 0.5, 3))
    return [g for g in gaps if g > 0.5]


def _thin_into(candidates: list[tuple[float, str, str]], new: list[tuple[float, str, str]],
               min_gap: float) -> None:
    existing = [c[0] for c in candidates]
    for item in new:
        if all(abs(item[0] - e) >= min_gap for e in existing):
            candidates.append(item)
            existing.append(item[0])


def _apply_min_gap(
    candidates: list[tuple[float, str, str]], min_gap: float
) -> list[tuple[float, str, str]]:
    out: list[tuple[float, str, str]] = []
    for c in sorted(candidates, key=lambda c: c[0]):
        if not out or c[0] - out[-1][0] >= min_gap:
            out.append(c)
    return out


_KIND_WEIGHT = {"scene": 4, "screen": 3, "transcript": 3, "silent": 2, "periodic": 1}


def _prioritise(
    kept: list[tuple[float, str, str]], budget: int, duration: float
) -> list[tuple[float, str, str]]:
    """Drop only the least informative candidates, keeping timeline spread."""
    if len(kept) <= budget:
        return kept
    scored = sorted(
        kept,
        key=lambda c: (_KIND_WEIGHT.get(c[1], 1), -(c[0] if duration <= 0 else 1.0)),
        reverse=True,
    )
    protected = [c for c in scored if _KIND_WEIGHT.get(c[1], 1) >= 3]
    others = [c for c in scored if _KIND_WEIGHT.get(c[1], 1) < 3]
    take = max(0, budget - len(protected))
    final = protected + others[:take]
    final.sort(key=lambda c: c[0])
    return final


def _write_frames(
    ctx: StageContext,
    src: Path,
    frames: list[tuple[float, str, str]],
    sigs: list[FrameSignature],
) -> None:
    out_dir = ctx.paths.source_frames(ctx.source_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    quality = int(ctx.settings.tuning.frame_quality)
    rows = []
    from ...paths import content_hash

    ctx.db.execute("DELETE FROM frames WHERE source_id=?", (ctx.source_id,))
    for t, kind, reason in frames:
        digest = content_hash(f"{ctx.source_id}:{t:.3f}".encode(), prefix=12)
        frame_id = f"f-{digest}"
        dst = out_dir / f"{frame_id}.jpg"
        try:
            extract_frame(src, t, dst, quality=quality)
        except MediaError:
            continue
        idx = min(int(t), len(sigs) - 1) if sigs else None
        rows.append(
            (
                frame_id, ctx.source_id, float(t), str(dst), None, None, dst.stat().st_size,
                kind, reason, None, None, None, time.time(),
            )
        )
    with ctx.db.transaction() as conn:
        conn.executemany("INSERT OR REPLACE INTO frames VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    ctx.artifacts["frames_written"] = len(rows)


def _total_bytes(ctx: StageContext) -> int:
    row = ctx.db.query_one(
        "SELECT SUM(bytes) AS b FROM frames WHERE source_id=?", (ctx.source_id,)
    )
    return int(row["b"]) if row and row["b"] else 0
