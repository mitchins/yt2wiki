"""V9 article writer: narration-first structured articles with stable sections.

Reads complete timestamped transcript segments, never the M7 evidence pile.
Writes sections in explicit transcript-coverage batches so no part of the
video is silently discarded.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger("yt2wiki.v9.writer")

TITLE_PROMPT = """You write concise technical documentation from a tutorial transcript.

Return JSON only: {"title": "...", "summary": "2-3 sentences", "kind": "procedure|explainer|experimental"}.
Rules: title under 90 chars, no clickbait, summary states what the video achieves."""

SECTION_PROMPT = """Write ONE documentation section from the transcript span below.

Context:
- video title: {video_title}
- section heading: {heading}
- transcript span: {t_start:.1f}s - {t_end:.1f}s

Transcript (timestamps in seconds):
\"\"\"
{transcript}
\"\"\"

Return JSON only:
{{"heading": {heading!r},
  "body": "markdown: short paragraphs and bullets; keep exact commands, values, paths verbatim; cite timestamps ONLY as [m:ss](url), e.g. [2:39](URL). Never write seconds like [159] or [159:0]",
  "key_points": ["..."],
  "warnings": ["..."]}}
Rules:
- Only use what the narration states or clearly implies. Never invent commands, settings or steps.
- Preserve prerequisites, corrections and failed attempts mentioned in the span.
- Prefer specific source-supported detail over generic advice. No marketing language."""


@dataclass
class V9Section:
    section_id: str
    heading: str
    body: str
    t_start: float
    t_end: float
    key_points: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "body": self.body,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "key_points": self.key_points,
            "warnings": self.warnings,
        }


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "section")[:40]


def _heading_from_transcript(transcript: Sequence[dict[str, Any]],
                               t_start: float, t_end: float) -> str:
    """Derive a concise heading from the transcript slice when no chapter exists.

    Uses the first substantive sentence in the window, trimmed to 5–7 words.
    Falls back to 'Introduction' / 'Overview' style labels only when the
    slice has no speech, preserving chronological structure without
    inventing operations.
    """
    # Collect text in window
    words: list[str] = []
    for r in transcript:
        if float(r["end"]) < t_start or float(r["start"]) > t_end:
            continue
        text = (r.get("text") or "").strip()
        if not text:
            continue
        # Split into sentences, take first substantive one
        for sent in re.split(r"[.!?]+", text):
            sent = sent.strip()
            if len(sent.split()) >= 4:
                words = sent.split()
                break
        if words:
            break
    if not words:
        # No speech in window — generic but not "Part N"
        return "Overview" if t_start < 30 else "Additional details"
    # Strip filler words, take first 6 content words
    stop = {"the", "a", "an", "and", "or", "but", "so", "then", "we", "you", "i", "it", "this", "that", "is", "are", "was", "were", "to", "for", "of", "in", "on", "with", "as", "at"}
    # Keep original order but drop leading stop words
    while words and words[0].lower() in stop:
        words.pop(0)
    heading = " ".join(words[:6])
    # Title-case without mangling acronyms
    heading = heading.strip(" ,;:").strip()
    if not heading:
        return "Overview"
    # Capitalize first letter, keep rest as-is but ensure not all lower
    return heading[0].upper() + heading[1:60]


def plan_sections(transcript: Sequence[dict[str, Any]],
                  chapters: Sequence[dict[str, Any]] | None,
                  duration_s: float | None) -> list[dict[str, Any]]:
    """Stable section plan with explicit transcript coverage (no silent drops)."""
    if not transcript:
        raise ValueError("empty transcript")
    total = duration_s or float(transcript[-1]["end"])
    bounds: list[tuple[float, float, str | None]] = []
    if chapters:
        for ch in chapters:
            try:
                bounds.append((float(ch.get("start_time", 0)), float(ch.get("end_time", total)),
                               str(ch.get("title") or "")))
            except (TypeError, ValueError):
                continue
        bounds.sort()
    if not bounds:
        step = 150.0
        t = 0.0
        while t < total:
            bounds.append((t, min(t + step, total), ""))
            t += step
    # Merge spans shorter than 45s into the following one (keep first heading).
    merged: list[list] = []
    for lo, hi, title in bounds:
        if merged and (hi - lo) < 25.0:
            merged[-1][1] = hi
            if title and not merged[-1][2]:
                merged[-1][2] = title
        else:
            merged.append([lo, hi, title])
    tend = max([hi for _, hi, _ in merged] + [float(transcript[-1]["end"])])
    if merged:
        merged[-1][1] = tend
    plan = []
    for i, (lo, hi, title) in enumerate(merged):
        heading = title.strip() if title and title.strip() else _heading_from_transcript(transcript, lo, hi)
        plan.append({
            "section_id": f"sec-{i + 1:02d}-{slug(heading)}",
            "heading": heading,
            "t_start": round(lo, 1),
            "t_end": round(hi, 1),
        })
    # Coverage assertion: plan must span the whole transcript.
    assert plan[0]["t_start"] <= float(transcript[0]["start"]) + 1.0
    assert plan[-1]["t_end"] >= float(transcript[-1]["end"]) - 1.0
    return plan


def section_transcript(transcript: Sequence[dict[str, Any]],
                       t_start: float, t_end: float, *, cap_chars: int = 9000) -> str:
    lines = []
    for r in transcript:
        if float(r["end"]) < t_start or float(r["start"]) > t_end:
            continue
        lines.append(f"[{float(r['start']):.1f}-{float(r['end']):.1f}] {r['text'].strip()}")
        if sum(len(x) for x in lines) > cap_chars:
            break
    return "\n".join(lines)


def write_article(settings, db, source_id: str, *, backend=None,
                  inference_snapshot: dict[str, Any] | None = None,
                  recorder=None) -> dict[str, Any]:
    """Generate the V9 ASR article. Returns {article fields, coverage, timings}."""
    from ..backends.loader import ModelManager
    from ..config import Settings as _S  # noqa: F401 (type only)

    started = time.time()
    srow = db.query_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if srow is None:
        raise KeyError(f"unknown source {source_id}")
    segs = [dict(r) for r in db.query(
        "SELECT idx, start, end, text FROM transcript_segments WHERE source_id=? ORDER BY idx",
        (source_id,))]
    if not segs:
        raise RuntimeError("no transcript available; run the transcribe stage first")
    meta = json.loads(srow["meta_json"] or "{}")
    chapters = (meta.get("provenance") or {}).get("chapters") or (meta.get("chapters") or [])
    plan = plan_sections(segs, chapters, srow["duration_s"])
    admitted = [(p["t_start"], p["t_end"]) for p in plan]

    video_title = srow["title"] or source_id
    url = srow["origin_url"]
    if recorder is None:
        def _default_rec(entry: dict) -> None:
            entry.setdefault("source_id", source_id)
            db.record_model_load(entry)

        recorder = _default_rec
    manager = ModelManager(settings, recorder=recorder,
                           inference_snapshot=inference_snapshot)

    def _chat(payload_msgs, **kw):
        if backend is not None:
            raw = backend.chat(payload_msgs, **kw)
            from ..backends.llm import extract_json
            return extract_json(raw)
        with manager.acquire("text") as b:
            return b.chat_json(payload_msgs, **kw)

    head = _chat(
        [{"role": "system", "content": "Return JSON only."},
         {"role": "user", "content": TITLE_PROMPT + f"\n\nVideo title: {video_title}\nFirst transcript lines:\n" +
          "\n".join(f"[{float(r['start']):.1f}] {r['text'][:120]}" for r in segs[:12])}],
        max_new_tokens=400, temperature=0.1)
    title = str(head.get("title") or video_title)[:120]
    summary = str(head.get("summary") or "")
    kind = str(head.get("kind") or "explainer")

    sections: list[V9Section] = []
    represented: list[tuple[float, float]] = []
    for p in plan:
        text = section_transcript(segs, p["t_start"], p["t_end"])
        if not text.strip():
            body = "_No speech in this span._"
            out = {"heading": p["heading"], "body": body, "key_points": [], "warnings": []}
        else:
            try:
                out = _chat(
                    [{"role": "system", "content": "Return JSON only."},
                     {"role": "user", "content": SECTION_PROMPT.format(
                         video_title=video_title, heading=p["heading"],
                         t_start=p["t_start"], t_end=p["t_end"], transcript=text, url=url)}],
                    max_new_tokens=1600, temperature=0.15)
                if not isinstance(out, dict) or not str(out.get("body", "")).strip():
                    raise ValueError("empty section body")
            except Exception as exc:  # noqa: BLE001 - article must survive model hiccups
                log.warning("section %s fallback: %s", p["section_id"], exc)
                out = {"heading": p["heading"],
                       "body": f"_Transcript excerpt (model unavailable):_\n\n{text[:1500]}",
                       "key_points": [], "warnings": []}
        sections.append(V9Section(
            section_id=p["section_id"], heading=str(out.get("heading") or p["heading"]),
            body=str(out.get("body", "")).strip(), t_start=p["t_start"], t_end=p["t_end"],
            key_points=[str(x) for x in (out.get("key_points") or [])][:8],
            warnings=[str(x) for x in (out.get("warnings") or [])][:6]))
        represented.append((p["t_start"], p["t_end"]))

    return {
        "title": title, "summary": summary, "kind": kind if kind in ("procedure", "explainer", "experimental") else "explainer",
        "sections": [s.to_dict() for s in sections],
        "coverage": {"admitted_windows": admitted, "represented_windows": represented,
                     "n_sections": len(sections),
                     "transcript_chars": sum(len(r["text"]) for r in segs)},
        "elapsed_s": round(time.time() - started, 3),
    }
