"""Stage 5 — visual interpretation.

Runs the VLM over every candidate frame in bounded batches.  Frames that look
text-dense get a second, higher-resolution tiled read so that small technical
text (version numbers, paths, register names, resistor colour codes) is not
lost.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from ...backends.loader import ModelManager
from ...db.schema import json_dumps, json_loads
from ...paths import content_hash
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.vision")

PROMPT_VERSION = "v3"
SYSTEM = (
    "You are a precise visual analyst for technical tutorial videos. "
    "You report only what is actually visible in the frame. "
    "You never infer, never guess and never add outside knowledge."
)

FRAME_PROMPT = """Analyse this single video frame for an instructional article.

Report ONLY what is visible. If something is unreadable, say so.

Return a JSON object:
{
  "frame_type": "terminal|code_editor|ui_settings|browser|hardware|diagram|slide|talking_head|other",
  "summary": "one short sentence describing the frame",
  "visible_text": ["verbatim important lines: commands, paths, filenames, version numbers, values, error text"],
  "ui_elements": ["labelled buttons, menus, fields or panels visible"],
  "settings": [{"name": "setting label", "value": "exact value shown"}],
  "actions_shown": ["physical or on-screen actions demonstrated in this frame, without narration"],
  "warnings": ["warnings, errors, caution text visible on screen"],
  "is_instructional": true,
  "legibility": "high|medium|low",
  "confidence": 0.0
}
Rules: preserve case, digits, punctuation and symbols exactly in visible_text and settings."""

CROP_PROMPT = """Read every piece of text visible in this cropped, enlarged region.
Preserve exact case, digits, punctuation, symbols and spacing.

Return JSON: {"text_lines": ["...", "..."]}"""

TILE_GRID = (2, 2)
TILE_OVERLAP = 0.12


def run(ctx: StageContext) -> dict[str, Any]:
    t = ctx.settings.tuning
    frames = ctx.db.query(
        "SELECT * FROM frames WHERE source_id=? ORDER BY t", (ctx.source_id,)
    )
    if not frames:
        raise RuntimeError("no candidate frames were stored; run frame_candidates first")

    frames = [dict(f) for f in frames]
    already = _interpreted(ctx, frames)
    todo = frames if ctx.artifacts.get("_force_vision") else [
        f for f in frames if f["frame_id"] not in already
    ]

    started = time.time()
    model_key = ctx.settings.model("vision").key
    metrics: dict[str, Any] = {
        "frames_total": len(frames),
        "frames_cached": len(frames) - len(todo),
        "model": model_key,
        "prompt_version": PROMPT_VERSION,
        "vision_batch": int(t.vision_batch),
    }
    if not todo:
        metrics["elapsed_s"] = 0.0
        return metrics

    batch = max(1, int(t.vision_batch))
    manager = ModelManager(ctx.settings, recorder=_recorder(ctx))
    processed = 0
    crops_used = 0
    with manager.acquire("vision") as backend:
        for start in range(0, len(todo), batch):
            chunk = todo[start:start + batch]
            ctx.check_cancel()
            images: list[Any] = []
            payloads: list[dict[str, Any]] = []
            for frame in chunk:
                path = Path(frame["path"])
                if not path.is_file():
                    continue
                try:
                    from PIL import Image

                    images.append(Image.open(path).convert("RGB"))
                    payloads.append(frame)
                except Exception as exc:  # noqa: BLE001
                    log.warning("unreadable frame %s: %s", frame["frame_id"], exc)
            if not images:
                continue
            results = backend.describe_batch(
                images,
                FRAME_PROMPT,
                max_new_tokens=int(t.vision_max_new_tokens),
                temperature=t.vision_temperature,
                system=SYSTEM,
                batch_size=batch,
            )
            dense_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for frame, raw, image in zip(payloads, results, images):
                payload = _safe_json(raw)
                if payload.get("parse_error"):
                    # The model hit the token budget mid-JSON.  Retry that frame
                    # alone with double the room before giving up on it.
                    retry = backend.describe_batch(
                        [image], FRAME_PROMPT,
                        max_new_tokens=int(t.vision_max_new_tokens) * 2,
                        temperature=0.0, system=SYSTEM, batch_size=1,
                    )
                    if retry:
                        payload = _safe_json(retry[0])
                text_lines = list(payload.get("visible_text") or [])
                if _looks_text_dense(payload, text_lines):
                    dense_candidates.append((frame, payload))
                else:
                    _store(ctx, frame, payload)
                    processed += 1
            if dense_candidates and t.vision_dense_text_pass:
                tiles: list[Any] = []
                owners: list[dict[str, Any]] = []
                meta: list[dict[str, Any]] = []
                for frame, payload in dense_candidates:
                    t_tiles, t_meta = _dense_tiles(frame)
                    tiles.extend(t_tiles)
                    meta.extend(t_meta)
                    owners.extend([frame] * len(t_tiles))
                tile_text = backend.describe_batch(
                    tiles, CROP_PROMPT, max_new_tokens=320, temperature=0.0,
                    system=SYSTEM, batch_size=batch,
                )
                grouped: dict[str, list[str]] = {}
                used: dict[str, list[dict[str, Any]]] = {}
                for frame, m, raw in zip(owners, meta, tile_text):
                    try:
                        tile_payload = _safe_json(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    lines = [str(x) for x in (tile_payload.get("text_lines") or [])]
                    grouped.setdefault(frame["frame_id"], []).extend(lines)
                    used.setdefault(frame["frame_id"], []).append(m)
                for frame, payload in dense_candidates:
                    extra = grouped.get(frame["frame_id"]) or []
                    if extra:
                        payload["visible_text"] = _merge_text_lines(
                            payload.get("visible_text") or [], extra
                        )
                        payload["crop_tiles"] = used.get(frame["frame_id"]) or []
                        crops_used += len(payload["crop_tiles"])
                    _store(ctx, frame, payload)
                    processed += 1
            ctx.progress(f"interpreted {processed + len(frames) - len(todo)}/{len(frames)} frames")
            ctx.artifacts["_force_vision"] = False

    metrics.update(
        {
            "frames_interpreted": processed,
            "crop_tiles": crops_used,
            "batch_degraded": int(getattr(backend, "batch_degradations", 0)),
            "elapsed_s": round(time.time() - started, 3),
            "peak_vram_gb": _peak(ctx),
        }
    )
    ctx.artifacts["frames_interpreted"] = len(frames)
    return metrics


# ----------------------------------------------------------------------
def _recorder(ctx: StageContext):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", "visual_interpret")
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


def _interpreted(ctx: StageContext, frames: Sequence[dict[str, Any]]) -> set[str]:
    rows = ctx.db.query(
        "SELECT frame_id FROM frame_interpretations WHERE model_key=? AND prompt_version=? "
        "AND profile=?",
        (ctx.settings.model("vision").key, PROMPT_VERSION, ctx.settings.profile),
    )
    return {r["frame_id"] for r in rows}


def _looks_text_dense(payload: dict[str, Any], text_lines: Sequence[str]) -> bool:
    if payload.get("frame_type") in ("terminal", "code_editor", "ui_settings", "browser"):
        return True
    joined = " ".join(text_lines)
    if len(joined) > 90:
        return True
    return payload.get("legibility") == "low"


def _dense_tiles(frame: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Split a dense frame into enlarged overlapping tiles for a second read."""
    from PIL import Image

    path = Path(frame["path"])
    try:
        img = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return [], []
    w, h = img.size
    cols, rows_n = TILE_GRID
    tw, th = w / cols, h / rows_n
    ox, oy = tw * TILE_OVERLAP, th * TILE_OVERLAP
    tiles: list[Any] = []
    meta: list[dict[str, Any]] = []
    for r in range(rows_n):
        for c in range(cols):
            x0 = max(0, int(c * tw - ox))
            y0 = max(0, int(r * th - oy))
            x1 = min(w, int((c + 1) * tw + ox))
            y1 = min(h, int((r + 1) * th + oy))
            crop = img.crop((x0, y0, x1, y1))
            if crop.width * crop.height < 20000:
                continue
            scale = max(1.0, 900.0 / max(1, crop.width))
            if scale > 1.0:
                crop = crop.resize(
                    (int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS
                )
            tiles.append(crop)
            meta.append(
                {
                    "box": [int(x0), int(y0), int(x1), int(y1)],
                    "source": frame["frame_id"],
                    "scale": round(scale, 3),
                }
            )
    return tiles, meta


def _merge_text_lines(base: Sequence[str], extra: Sequence[str]) -> list[str]:
    out = [str(b).strip() for b in base if str(b).strip()]
    seen = {_norm(b) for b in out}
    for line in extra:
        s = str(line).strip()
        if not s:
            continue
        n = _norm(s)
        if n in seen:
            continue
        if len(s) > 200:
            continue
        out.append(s)
        seen.add(n)
    return out


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


# ----------------------------------------------------------------------
def _safe_json(raw: str) -> dict[str, Any]:
    """Parse the model reply, repairing truncation instead of discarding it."""
    from ...backends.llm import extract_json

    text = (raw or "").strip()
    empty = {"summary": "", "visible_text": [], "legibility": "unknown",
             "confidence": 0.0, "parse_error": True}
    if not text:
        return dict(empty)
    try:
        value = extract_json(text)
    except ValueError:
        return {
            "summary": text[:400],
            "raw_reply": text[:2500],
            **{k: v for k, v in empty.items() if k != "summary"},
        }
    if not isinstance(value, dict):
        return {"summary": str(value)[:400], **{k: v for k, v in empty.items() if k != "summary"}}
    value.setdefault("summary", "")
    value.setdefault("visible_text", [])
    value.setdefault("legibility", "unknown")
    if not isinstance(value.get("visible_text"), list):
        value["visible_text"] = []
    if not isinstance(value.get("summary"), str):
        value["summary"] = json.dumps(value["summary"], ensure_ascii=False)[:400]
    return value


def _store(ctx: StageContext, frame: dict[str, Any], payload: dict[str, Any]) -> None:
    ctx.db.execute(
        """INSERT OR REPLACE INTO frame_interpretations
             (frame_id, model_key, prompt_version, profile, payload_json, created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            frame["frame_id"], ctx.settings.model("vision").key, PROMPT_VERSION,
            ctx.settings.profile, json_dumps(payload), time.time(),
        ),
    )


def _peak(ctx: StageContext) -> float:
    from ...gpu import peak_vram_gb

    return round(peak_vram_gb(), 3)
