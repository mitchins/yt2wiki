"""Stage 6 — evidence extraction.

Reads the transcript plus every frame interpretation and emits *observations*
only: no prose, no instructions invented.  Each record carries its timestamp
span and the frames that support it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from ...backends.loader import ModelManager
from ...db.schema import json_dumps, json_loads
from ...records import Evidence, chunk_transcript, new_evidence
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.evidence")

PROMPT_VERSION = "v2"
SYSTEM = (
    "You extract structured observations from tutorial video transcripts and "
    "frame descriptions. You never invent instructions, values or steps. "
    "If the source is unclear, emit an 'unresolved' observation instead of guessing."
)

CHUNK_PROMPT = """You are given a transcript window from a technical tutorial video, plus
descriptions of the video frames that fall inside that window.

Extract every observation that a later article would need. Use ONLY what the
source says or shows. Do not add outside knowledge. Do not merge unrelated things.

Return JSON: {{"observations": [ ... ]}} where each observation is:
{{
  "kind": "action|prerequisite|tool|material|config|command|code|measurement|identifier|warning|expected_result|error|correction|alternative|unresolved|observation",
  "text": "one precise sentence, preserving exact values, commands, paths and identifiers verbatim",
  "confidence": 0.0,
  "supported_by": ["frame:<id>" or "transcript"]
}}

Rules:
- "action": something the presenter does or instructs the viewer to do.
- "command"/"code": exact command lines or code; keep every character.
- "config"/"measurement"/"identifier": exact setting names, values and numbers.
- "warning": safety cautions, destructive operations, gotchas stated by the source.
- "error": something that failed. "correction": the source revising an earlier value or step.
- "alternative": a second valid way stated by the source.
- "unresolved": something the viewer cannot determine from the source.
- Deduplicate near-identical observations.
- Prefer fewer, higher-quality observations over noisy padding.

TRANSCRIPT WINDOW ({window_start:.1f}s - {window_end:.1f}s):
\"\"\"
{transcript}
\"\"\"

FRAMES INSIDE THE WINDOW:
{frames}

Return only JSON."""

def run(ctx: StageContext) -> dict[str, Any]:
    started = time.time()
    t = ctx.settings.tuning
    seg_rows = ctx.db.query(
        "SELECT idx, start, end, text FROM transcript_segments WHERE source_id=? ORDER BY start",
        (ctx.source_id,),
    )
    if not seg_rows:
        raise RuntimeError("no transcript available; run the transcribe stage first")

    frame_rows = ctx.db.query(
        """SELECT f.frame_id, f.t, p.payload_json FROM frames f
           LEFT JOIN frame_interpretations p ON p.frame_id = f.frame_id
           WHERE f.source_id=? ORDER BY f.t""",
        (ctx.source_id,),
    )
    frame_index = _index_frames(frame_rows) if t.evidence_use_frames else []
    model_key = ctx.settings.model("text").key

    chunks = chunk_transcript(seg_rows, window_s=float(t.text_window_s), overlap_s=15.0)
    ctx.artifacts["evidence_chunks"] = len(chunks)

    if ctx.artifacts.get("_force_evidence"):
        ctx.db.execute("DELETE FROM evidence WHERE source_id=?", (ctx.source_id,))
        ctx.artifacts["_force_evidence"] = False

    manager = ModelManager(ctx.settings, recorder=_recorder(ctx))
    collected: list[Evidence] = []
    raw_dump = ctx.work_dir("evidence") / "raw_replies.txt"
    with manager.acquire("text") as backend:
        for i, chunk in enumerate(chunks):
            ctx.check_cancel()
            window_start = float(chunk[0]["start"])
            window_end = float(chunk[-1]["end"])
            transcript = "\n".join(
                f"[{float(r['start']):.1f}-{float(r['end']):.1f}] {r['text'].strip()}"
                for r in chunk
            )
            frames = _frames_for_window(frame_index, window_start, window_end)
            prompt = CHUNK_PROMPT.format(
                window_start=window_start,
                window_end=window_end,
                transcript=transcript[:12000],
                frames=frames[:9000],
            )
            try:
                payload = backend.chat_with_dump(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
                    dump_path=raw_dump,
                    max_new_tokens=int(t.text_max_tokens),
                    temperature=t.text_temperature,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("chunk %d extraction failed: %s", i, exc)
                dump = ctx.work_dir("evidence") / f"chunk_{i:03d}.error.txt"
                try:
                    dump.write_text("PROMPT TAIL:\n" + prompt[-1500:] +
                                    "\n\n===== ERROR =====\n" + str(exc), encoding="utf-8")
                except OSError:
                    pass
                ctx.db.log(
                    "warning",
                    f"evidence chunk {i} failed: {exc}; prompt+error saved to {dump}",
                    source_id=ctx.source_id,
                )
                continue
            for obs in payload.get("observations", []) or []:
                if not isinstance(obs, dict):
                    continue
                evidence = _to_evidence(
                    ctx, obs, window_start, window_end, frame_index, model_key
                )
                if evidence is not None:
                    collected.append(evidence)
            if len(chunks) > 4 and i % max(1, len(chunks) // 4) == 0:
                ctx.progress(f"extracted evidence from {i + 1}/{len(chunks)} windows")

        consolidated = _consolidate(ctx, collected)
        contradictions = (
            _adjudicate_contradictions(ctx, backend, consolidated)
            if t.enable_contradiction_pass else []
        )

    _persist(ctx, consolidated, contradictions)
    ctx.artifacts["evidence_count"] = len(consolidated)
    ctx.artifacts["evidence_raw_count"] = len(collected)
    _write_digest(ctx, consolidated)
    return {
        "chunks": len(chunks),
        "observations_raw": len(collected),
        "observations": len(consolidated),
        "filtered_identifiers": ctx.artifacts.get("evidence_filtered_identifiers", 0),
        "corrections": len(contradictions),
        "by_kind": _kind_counts(consolidated),
        "literal_values": sum(len(e.literals) for e in consolidated),
        "frames_used": len(frame_index),
        "elapsed_s": round(time.time() - started, 3),
    }


# ----------------------------------------------------------------------
def _recorder(ctx: StageContext):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", "evidence")
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


def _index_frames(rows: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = json_loads(r["payload_json"], {}) or {}
        out.append(
            {
                "frame_id": r["frame_id"],
                "t": float(r["t"]),
                "summary": payload.get("summary", ""),
                "visible_text": payload.get("visible_text", []),
                "settings": payload.get("settings", []),
                "actions": payload.get("actions_shown", []),
                "warnings": payload.get("warnings", []),
                "frame_type": payload.get("frame_type", ""),
                "legibility": payload.get("legibility", ""),
            }
        )
    return out


def _frames_for_window(frames: Sequence[dict[str, Any]], start: float, end: float) -> str:
    lines: list[str] = []
    for f in frames:
        if f["t"] + 0.5 < start or f["t"] - 0.5 > end:
            continue
        bits = [f"frame:{f['frame_id']} @ {f['t']:.1f}s [{f['frame_type'] or '?'}]"]
        if f["summary"]:
            bits.append(f"  summary: {f['summary']}")
        if f["visible_text"]:
            bits.append("  visible text: " + " | ".join(str(x) for x in f["visible_text"][:14]))
        if f["settings"]:
            bits.append(
                "  settings: "
                + " | ".join(f"{s.get('name')}={s.get('value')}" for s in f["settings"][:10])
            )
        if f["actions"]:
            bits.append("  actions shown: " + " | ".join(str(x) for x in f["actions"][:6]))
        if f["warnings"]:
            bits.append("  warnings: " + " | ".join(str(x) for x in f["warnings"][:6]))
        lines.append("\n".join(bits))
    return "\n\n".join(lines)


def _to_evidence(
    ctx: StageContext,
    obs: dict[str, Any],
    window_start: float,
    window_end: float,
    frame_index: Sequence[dict[str, Any]],
    model_key: str,
) -> Evidence | None:
    kind = str(obs.get("kind", "observation")).strip().lower()
    from ...records import EVIDENCE_KINDS

    if kind not in EVIDENCE_KINDS:
        kind = "observation"
    text = str(obs.get("text", "")).strip()
    if len(text) < 4:
        return None
    support = [str(s) for s in (obs.get("supported_by") or [])]
    frame_ids: list[str] = []
    for s in support:
        if s.startswith("frame:"):
            fid = s[len("frame:"):]
            if fid:
                frame_ids.append(fid)
    if frame_ids:
        times = [f["t"] for f in frame_index if f["frame_id"] in frame_ids]
    else:
        times = []
    if times:
        t_start, t_end = min(times), max(times)
    else:
        t_start, t_end = window_start, window_end
    try:
        conf = float(obs.get("confidence", 0.6))
    except (TypeError, ValueError):
        conf = 0.6
    evidence = new_evidence(
        ctx.source_id,
        kind,
        text,
        t_start=t_start,
        t_end=t_end,
        frame_ids=sorted(set(frame_ids)),
        confidence=max(0.0, min(1.0, conf)),
        model_key=model_key,
        prompt_version=PROMPT_VERSION,
    )
    return evidence


def _consolidate(ctx: StageContext, collected: Sequence[Evidence]) -> list[Evidence]:
    """Deterministic merge - no model round-trip, so results are reproducible.

    Exact duplicates collapse, near-duplicates collapse onto the record carrying
    the most exact values, and everything else is preserved in chronological
    order.  Pairs that merely *look* contradictory are kept and adjudicated
    separately rather than silently merged.
    """
    ordered = sorted(collected, key=lambda e: (e.t_start or 0.0, e.evidence_id))
    out: list[Evidence] = []
    filtered = 0
    for cand in ordered:
        if _incidental_identifier(cand):
            # An "identifier" that is one or two bare words with no exact value
            # is almost always incidental on-screen text (a directory listing,
            # a column header), not a value a reader must reproduce.  Dropping
            # it here keeps the omission report meaningful instead of drowning
            # it in noise; the observation is still in evidence.jsonl under
            # `observations_raw`.
            filtered += 1
            continue
        key = cand.support_key()
        merged = False
        for i, existing in enumerate(out):
            if existing.t_start is not None and cand.t_start is not None:
                if abs(existing.t_start - cand.t_start) > 45:
                    continue
            if key == existing.support_key() or _near_duplicate(existing, cand):
                if _richness(cand) > _richness(existing):
                    out[i] = cand
                merged = True
                break
        if not merged:
            out.append(cand)
    ctx.artifacts["evidence_filtered_identifiers"] = filtered
    return out


def _incidental_identifier(e: Evidence) -> bool:
    if e.kind != "identifier":
        return False
    if e.literals:
        return False
    words = [w for w in e.text.split() if w.isalnum()]
    return 0 < len(words) <= 2


def _richness(e: Evidence) -> tuple[int, float]:
    return (len(e.literals), e.confidence)


def _near_duplicate(a: Evidence, b: Evidence) -> bool:
    if a.kind != b.kind:
        return False
    ta, tb = _tokens(a.text), _tokens(b.text)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.82


_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is",
         "are", "be", "that", "this", "it", "as", "at", "by", "from", "you", "your"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_.:/@-]{3,}", text.lower()) if w not in _STOP}


CONTRADICTION_PROMPT = """The same tutorial produced these observations. Some disagree because
the creator changed a value or corrected an earlier step.

For each pair decide whether the LATER observation supersedes the earlier one and, if so,
write one "correction" observation naming both versions.

Return JSON: {{"corrections": [{{"kind": "correction", "text": "...", "supersedes": "ev-...", "t_start": 0.0, "t_end": 0.0, "confidence": 0.9}}], "notes": "..."}}

PAIRS:
{pairs}

Return only JSON."""


def _adjudicate_contradictions(
    ctx: StageContext, backend, items: Sequence[Evidence]
) -> list[dict[str, Any]]:
    """Ask the model only about specific, rule-detected value disagreements."""
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if b.kind not in ("command", "config", "measurement", "identifier"):
                continue
            if _near_duplicate(a, b) and _values_differ(a, b):
                pairs.append({
                    "earlier": {"evidence_id": a.evidence_id, "text": a.text,
                                "t": round(a.t_start or 0, 1)},
                    "later": {"evidence_id": b.evidence_id, "text": b.text,
                              "t": round(b.t_start or 0, 1)},
                })
    if not pairs:
        return []
    pairs = pairs[:12]
    try:
        payload = backend.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": CONTRADICTION_PROMPT.format(
                 pairs=json.dumps(pairs, ensure_ascii=False)[:9000])}],
            max_new_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("contradiction adjudication failed: %s", exc)
        return []
    return [p for p in (payload.get("corrections") or [])
            if isinstance(p, dict) and p.get("text")]


def _values_differ(a: Evidence, b: Evidence) -> bool:
    av = {lit.normalized for lit in a.literals}
    bv = {lit.normalized for lit in b.literals}
    return bool(av) and bool(bv) and not (av & bv)


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _kind_counts(items: Sequence[Evidence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in items:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _persist(
    ctx: StageContext,
    items: Sequence[Evidence],
    contradictions: Sequence[dict[str, Any]] = (),
) -> None:
    ctx.db.execute("DELETE FROM evidence_fts")
    superseded = {
        str(p.get("supersedes")): str(p.get("text") or "")[:200]
        for p in contradictions
        if p.get("supersedes")
    }
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM evidence WHERE source_id=?", (ctx.source_id,))
        for e in items:
            conn.execute(
                "INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                e.to_row(ctx.source_id),
            )
        rows = conn.execute(
            "SELECT rowid, evidence_id, text, normalized, kind FROM evidence WHERE source_id=?",
            (ctx.source_id,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO evidence_fts(rowid, evidence_id, text, normalized, kind) VALUES(?,?,?,?,?)",
            [(r["rowid"], r["evidence_id"], r["text"], r["normalized"] or "", r["kind"]) for r in rows],
        )


def _write_digest(ctx: StageContext, items: Sequence[Evidence]) -> None:
    out = ctx.work_dir("evidence") / "evidence.json"
    payload = []
    for e in items:
        d = e.to_dict()
        payload.append(d)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.record_artifact_hash("evidence", str(out))
    ctx.artifacts["evidence_json"] = str(out)
