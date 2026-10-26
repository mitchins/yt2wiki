"""V9 image selection: per-section candidates, comparative VLM ranking, captions.

Selection operates on stable section IDs. Ranking compares shortlisted
frames in one VLM pass; single-frame scoring is the fallback. The same
frame is never reused for two sections unless no alternative exists.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("yt2wiki.v9.select")

RANK_PROMPT = """You are choosing illustration(s) for one section of a technical article.
Section heading: {heading}
Section text:
\"\"\"
{body}
\"\"\"

The images are numbered 1..{n} in order. Compare them and reply JSON only:
{{"best": [<1-based indices, best first>], "reason": "one sentence per chosen image, what the reader learns from it",
 "second": <1-based index or 0 if no second image is worthwhile>,
 "second_reason": "...",
  "images_received": <number of images numbered 1..{n} above>}}
Rules: pick at most 2; second only for a genuinely useful before/after pair or a distinct result state;
reply {{"best": [], ...}} when none of the images materially helps this section. Never invent content.
Never select a title card, logo-only graphic, presenter talking-head shot, transition graphic, or mostly
empty screen when any instructional frame is available. Prefer the frame showing the actual subject, code,
screen, hardware, or action being described."""

# Server-enforced shape for the rank reply (json_schema/strict). The client
# still verifies images_received against the images actually sent: a schema
# alone cannot stop a made-up verdict on a malformed (e.g. imageless) prompt.
RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "best": {"type": "array", "items": {"type": "integer"}},
        "second": {"type": "integer"},
        "reason": {"type": "string"},
        "second_reason": {"type": "string"},
        "images_received": {"type": "integer"},
    },
    "required": ["best", "second", "reason", "second_reason", "images_received"],
    "additionalProperties": False,
}

CAPTION_PROMPT = """Describe this source frame in one sentence for a technical manual reader.
State only what is visibly present. No invented actions, warnings or values.
If nothing instructional is visible, reply exactly: NO_USEFUL_CONTENT.
Reply with the sentence only."""


def candidates_for_section(frames: Sequence[dict[str, Any]], t_start: float, t_end: float,
                           *, pad: float = 20.0, limit: int = 10) -> list[dict[str, Any]]:
    """Frames near the section window, spread across it, plus edge neighbours."""
    lo, hi = t_start - pad, t_end + pad
    inside = [f for f in frames if lo <= float(f["t"]) <= hi]
    inside.sort(key=lambda f: float(f["t"]))
    if len(inside) <= limit:
        return inside
    # Spread: prefer scene/keyframe kinds, then even stride.
    scored = sorted(inside, key=lambda f: (0 if str(f.get("kind")) in ("scene", "keyframe") else 1,
                                           float(f["t"])))
    picked = scored[: max(2, limit // 2)]
    rest = [f for f in inside if f not in picked]
    stride = max(1, len(rest) // max(1, (limit - len(picked))))
    picked += rest[::stride][: limit - len(picked)]
    return sorted(picked, key=lambda f: float(f["t"]))


def rank_section(backend, section: dict[str, Any],
                 candidates: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Comparative rank; falls back to independent relevance scoring."""
    from PIL import Image
    images, valid = [], []
    for c in candidates[:8]:
        try:
            images.append(Image.open(c["path"]).convert("RGB"))
            valid.append(c)
        except Exception:  # noqa: BLE001
            continue
    if not valid:
        return {"best": [], "reason": "no readable candidates"}
    raw = ""
    schema_kw: dict[str, Any] = {}
    if getattr(backend, "supports_structured_schema", False):
        schema_kw = {"response_schema": RANK_SCHEMA, "schema_name": "rank"}
    try:
        raw = backend.rank_images(
            images,
            RANK_PROMPT.format(heading=section["heading"][:120],
                               body=section["body"][:2500], n=len(images)),
            max_new_tokens=400, temperature=0.0,
            system="You rank images for technical documentation. Reply JSON only.",
            **schema_kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("comparative rank failed: %s", exc)
    best: list[int] = []
    reason = ""
    if raw:
        try:
            from ..backends.llm import extract_json
            payload = extract_json(raw)
            # Grounding check: the verdict must account for exactly the
            # images sent. A schema cannot stop a made-up verdict on a
            # malformed prompt, so a mismatch fails the rank outright and
            # the heuristic fallback below takes over. The count must be a
            # real JSON integer: bool/float/str approximations (True, 2.9,
            # "2") fail even when they would coerce to the right number.
            received = payload.get("images_received")
            if (received is None or isinstance(received, bool)
                    or not isinstance(received, int)
                    or received != len(valid)):
                raise ValueError(
                    f"images_received={received!r} for {len(valid)} sent")
            if str(payload.get("reason") or "") == "NO_IMAGE":
                raise ValueError("model reports no images received")
            best = [int(i) - 1 for i in (payload.get("best") or []) if 0 < int(i) <= len(valid)]
            second = int(payload.get("second") or 0)
            if 0 < second <= len(valid) and (second - 1) not in best:
                best.append(second - 1)
            reason = str(payload.get("reason") or payload.get("second_reason") or "")
        except Exception:  # noqa: BLE001
            log.warning("rank parse failed: %r", raw[:200])
    if not best:
        # Independent fallback: reuse rank_images per image is wasteful; use a
        # cheap relevance heuristic (scene/keyframe kinds + window centrality).
        mid = (section["t_start"] + section["t_end"]) / 2
        order = sorted(range(len(valid)),
                       key=lambda i: (0 if str(valid[i].get("kind")) in ("scene", "keyframe") else 1,
                                      abs(float(valid[i]["t"]) - mid)))
        best = order[:1]
        reason = "fallback: central keyframe (comparative rank unavailable)"
    return {"best": best, "reason": reason,
            "candidates": [{"frame_id": c["frame_id"], "t": c["t"]} for c in valid]}


def caption_frame(backend, path: str) -> str:
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return ""
    try:
        out = backend.describe_batch(
            [im], CAPTION_PROMPT, max_new_tokens=120, temperature=0.0,
            system="Describe only what is visible. One sentence.", batch_size=1)
        text = (out[0] if out else "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("caption failed: %s", exc)
        return ""
    if not text or text.strip().upper() == "NO_USEFUL_CONTENT":
        return ""
    return text[:300]


ENRICH_PROMPT = """Improve ONE paragraph using the image description. Original paragraph:
\"\"\"
{paragraph}
\"\"\"
Related transcript excerpt:
\"\"\"
{transcript}
\"\"\"
Image ({t:.1f}s): {caption}
Uncertainty: {uncertainty}

Return JSON only: {{"paragraph": "improved paragraph or the ORIGINAL unchanged",
 "changed": true/false, "why": "..."}}
Rules: only clarify what the paragraph or image directly establishes; never add
commands, settings, warnings or requirements; keep timestamps; leave unrelated text alone."""


def enrich_paragraph(backend, paragraph: str, transcript: str, caption: str,
                     t: float, uncertainty: str) -> dict[str, Any]:
    try:
        raw = backend.chat_json(
            [{"role": "system", "content": "Return JSON only."},
             {"role": "user", "content": ENRICH_PROMPT.format(
                 paragraph=paragraph[:1200], transcript=transcript[:1500],
                 caption=caption[:400], t=t, uncertainty=uncertainty)}],
            max_new_tokens=600, temperature=0.1)
        if isinstance(raw, dict) and isinstance(raw.get("paragraph"), str):
            return {"paragraph": raw["paragraph"], "changed": bool(raw.get("changed")),
                    "why": str(raw.get("why", ""))}
    except Exception as exc:  # noqa: BLE001
        log.warning("enrichment failed: %s", exc)
    return {"paragraph": paragraph, "changed": False, "why": "enrichment unavailable"}
