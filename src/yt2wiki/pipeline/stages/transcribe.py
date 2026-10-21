"""Stage 3 — transcription.

Chooses between supplied captions and local ASR, records which was used and
why, and always keeps word-level timestamps when ASR runs.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ...backends.asr import Transcript
from ...backends.loader import ModelManager
from ...db.schema import json_dumps, json_loads
from ...media import MediaError
from .. import StageContext

log = logging.getLogger("yt2wiki.stage.transcribe")


def run(ctx: StageContext) -> dict[str, Any]:
    src = Path(ctx.artifacts.get("video_path") or ctx.paths.source_video(ctx.source_id))
    audio = Path(ctx.artifacts.get("audio_path") or ctx.paths.source_audio(ctx.source_id))
    if not audio.is_file():
        raise MediaError(f"normalised audio missing: {audio}")

    row = ctx.db.query_one("SELECT * FROM sources WHERE source_id=?", (ctx.source_id,))
    if row is None:
        raise KeyError("source row missing")
    duration = float(ctx.artifacts.get("source_duration_s") or row["duration_s"] or 0.0)

    captions = _load_captions(ctx)
    decision = _decide(captions, duration)
    ctx.artifacts["transcript_source"] = decision

    if decision["mode"] == "captions":
        transcript = _captions_to_transcript(captions["captions"], duration)
        metrics = {"mode": "captions", "reason": decision["reason"]}
    else:
        transcript = _run_asr(ctx, audio, duration, decision, row)
        metrics = {"mode": "asr", "reason": decision["reason"]}

    _persist(ctx, transcript)
    ctx.record_input_hash("transcript", audio)
    ctx.artifacts["transcript_segments"] = len(transcript.segments)
    ctx.artifacts["transcript_words"] = sum(len(s.words) for s in transcript.segments)
    ctx.artifacts["transcript_seconds"] = round(
        sum(s.end - s.start for s in transcript.segments), 1
    )
    metrics.update(ctx.artifacts)
    return metrics


# ----------------------------------------------------------------------
def _load_captions(ctx: StageContext) -> dict[str, Any]:
    meta = ctx.meta()
    if "subtitle" not in meta:
        return {"mode": "none"}
    p = ctx.source_dir / "captions.srt.json"
    if not p.is_file():
        return {"mode": "none"}
    try:
        caps = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "none"}
    if not caps:
        return {"mode": "none"}
    return {
        "mode": "manual" if not meta["subtitle"].get("auto") else "auto",
        "captions": caps,
        "lang": meta["subtitle"].get("lang"),
    }


def _decide(captions: dict[str, Any], duration: float) -> dict[str, Any]:
    if captions.get("mode") == "none":
        return {"mode": "asr", "reason": "no usable supplied captions"}
    covered = sum(c["end"] - c["start"] for c in captions["captions"])
    coverage = (covered / duration) if duration > 0 else 0.0
    if captions["mode"] == "manual":
        return {
            "mode": "captions",
            "reason": f"manual captions by the uploader (coverage {coverage:.0%})",
            "coverage": round(coverage, 3),
        }
    return {
        "mode": "asr",
        "reason": (
            f"only automatic captions (coverage {coverage:.0%}); local ASR gives better "
            "handling of numbers, identifiers and command syntax"
        ),
        "coverage": round(coverage, 3),
    }


def _captions_to_transcript(captions: list[dict[str, Any]], duration: float) -> Transcript:
    from ...backends.asr import Segment

    segs = [
        Segment(start=float(c["start"]), end=float(c["end"]), text=c["text"].strip())
        for c in captions
        if c.get("text", "").strip()
    ]
    segs.sort(key=lambda s: s.start)
    return Transcript(language="en", language_probability=1.0, duration_s=duration, segments=segs)


def _run_asr(
    ctx: StageContext,
    audio: Path,
    duration: float,
    decision: dict[str, Any],
    row: Any,
) -> Transcript:
    t = ctx.settings.tuning
    manager = ModelManager(ctx.settings, recorder=_recorder(ctx, "transcribe"))
    hint = _vocabulary_hint(ctx, row)

    def on_progress(done: int, end_s: float, total: float) -> None:
        if total > 0:
            ctx.progress(f"transcribing: {end_s:.0f}s / {total:.0f}s")

    with manager.acquire("asr") as backend:
        transcript = backend.transcribe(
            audio,
            language=t.asr_language,
            vad_filter=t.asr_vad_filter,
            condition_on_previous_text=t.asr_condition_on_previous_text,
            initial_prompt=hint,
            on_progress=on_progress,
        )
    return transcript


def _vocabulary_hint(ctx: StageContext, row: Any) -> str:
    """Seed the decoder with domain vocabulary drawn from the source's own text.

    This is deliberately built from *evidence about the video* (title, channel,
    description, chapter titles) and never from outside knowledge.
    """
    bits: list[str] = []
    title = row["title"] if row["title"] else ""
    uploader = row["uploader"] if row["uploader"] else ""
    if title:
        bits.append(str(title))
    if uploader:
        bits.append(f"by {uploader}")
    for ch in json_loads(row["chapters_json"] if "chapters_json" in row.keys() else None, []) or []:
        if ch.get("title"):
            bits.append(str(ch["title"]))
    desc = (row["description"] or "") if "description" in row.keys() else ""
    for line in desc.splitlines():
        line = line.strip()
        if 12 < len(line) < 90 and any(c.isdigit() for c in line):
            bits.append(line)
        if len(bits) > 14:
            break
    return ". ".join(bits)[:700]


def _recorder(ctx: StageContext, stage: str):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", stage)
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


# ----------------------------------------------------------------------
def _persist(ctx: StageContext, transcript: Transcript) -> None:
    ctx.db.execute("DELETE FROM transcript_segments WHERE source_id=?", (ctx.source_id,))
    ctx.db.execute("DELETE FROM transcript_words WHERE source_id=?", (ctx.source_id,))
    model_key = ctx.settings.model("asr").key if ctx.artifacts.get("transcript_source", {}).get("mode") == "asr" else "captions"
    rows = []
    word_rows = []
    for idx, seg in enumerate(transcript.segments):
        rows.append(
            (
                ctx.source_id, idx, seg.start, seg.end, seg.text.strip(),
                seg.avg_logprob, seg.no_speech_prob, seg.compression_ratio, model_key,
            )
        )
        for w_i, w in enumerate(seg.words):
            word_rows.append((ctx.source_id, idx, w_i, w.start, w.end, w.word, w.probability))
    with ctx.db.transaction() as conn:
        conn.executemany(
            "INSERT INTO transcript_segments VALUES(?,?,?,?,?,?,?,?,?)", rows
        )
        conn.executemany("INSERT INTO transcript_words VALUES(?,?,?,?,?,?,?)", word_rows)
        conn.executemany(
            "INSERT INTO transcript_fts(source_id, idx, text) VALUES(?,?,?)",
            [(ctx.source_id, i, s.text) for i, s in enumerate(transcript.segments)],
        )
    out = ctx.work_dir("transcribe") / "transcript.jsonl"
    transcript.save_jsonl(out)
    ctx.artifacts["transcript_jsonl"] = str(out)
    ctx.db.log("info", f"persisted {len(rows)} transcript segments", source_id=ctx.source_id)
