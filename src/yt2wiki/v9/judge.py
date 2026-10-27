"""V9-Z automated visual editorial review: veto-and-revise loop.

Each pipeline pick is vetted by single-image VLM decorative classification
plus a deterministic OCR-density check. A flagged pick is replaced by the
first clean alternative in rank order (one expansion round follows if none
is clean, otherwise the section abstains); the final pick is re-verified.
Every step is recorded for provenance.

V9-Z1 (below) extends this into a rubric-based editorial judge: the current
pick and strongest alternatives are scored comparatively (relevance,
clarity, instructional value, visual payoff, each 0-2) against the section
paragraph, with KEEP / REPLACE / EXPAND / USE_PAIR / OMIT decisions and up
to two directed refinement rounds. The veto path above is retained as the
inexpensive pre-filter and as the fallback when scoring fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from typing import Any, Sequence

log = logging.getLogger("yt2wiki.v9.judge")

def expand_candidates(frames: Sequence[dict[str, Any]], t_start: float, t_end: float,
                      seen: set[str], hint: str, *,
                      pad: float = 45.0, limit: int = 8) -> list[dict[str, Any]]:
    """Targeted re-retrieval driven by the judge's concrete criticism."""
    hint_l = (hint or "").lower()
    if "earlier" in hint_l or "before" in hint_l:
        lo, hi = t_start - pad, t_start + (t_end - t_start) / 2
    elif "later" in hint_l or "after" in hint_l or "result" in hint_l:
        lo, hi = t_start + (t_end - t_start) / 2, t_end + pad
    else:
        lo, hi = t_start - pad, t_end + pad
    cands = [f for f in frames if lo <= float(f["t"]) <= hi and f["frame_id"] not in seen]
    cands.sort(key=lambda f: (
        0 if str(f.get("kind")) in ("scene", "keyframe") else 1, float(f["t"])))
    # spread across the window rather than clustering
    if len(cands) > limit:
        step = len(cands) / limit
        cands = [cands[int(i * step)] for i in range(limit)]
    return cands


TEXTY_HINTS = ("code", "command", "config", "file", "class", "terminal", "css",
               "error", "setting", "value", "directive", "function", "import", "package")


def _section_expects_text(section: dict[str, Any]) -> bool:
    blob = ((section.get("heading") or "") + " " + (section.get("body") or "")).lower()
    return any(k in blob for k in TEXTY_HINTS)


CLASSIFY_PROMPT = """Classify this single image with exactly one label. Reply with ONLY the label, nothing else.
Labels: instructional, meme_or_joke, title_card, logo_only, talking_head, transition,
blank_screen, player_chrome, unrelated_broll, promo_or_sales.
instructional = code, terminal with content, UI screen, hardware, document, diagram,
or visible work being done. talking_head = a person's face is the main subject
with no instructional content. promo_or_sales = sales page, discount banner,
promo code overlay, pricing pitch, or affiliate marketing. Be strict: when torn
between instructional and a decorative label, choose the decorative one."""

DECORATIVE_LABELS = frozenset({
    "meme_or_joke", "title_card", "logo_only", "talking_head", "transition",
    "blank_screen", "player_chrome", "unrelated_broll", "promo_or_sales",
})

# Closed vocabulary for classifier replies (after synonym normalisation).
INSTRUCTIONAL_LABEL = "instructional"
KNOWN_LABELS = frozenset(DECORATIVE_LABELS | {INSTRUCTIONAL_LABEL})

# Near-synonyms the classifier emits despite the closed vocabulary.
# "hardware" is named in the prompt's own definition of instructional, but
# Qwen3.5 emits the bare word for hardware photos (observed 3/3 on a Pi
# NAS close-up); without this mapping those frames veto as unreadable.
CLASSIFY_SYNONYMS = frozenset({
    "ui", "document", "diagram", "slide", "screenshot", "webpage",
    "app", "software", "screen", "photo", "hardware",
})


def _canonical_label(raw: str | None) -> str:
    """Map a raw classifier reply onto the closed label vocabulary.

    Both veto paths (`_vet_candidate` single-frame and `_veto_from_label`
    batched) use this, so an off-vocabulary reply is treated identically
    everywhere: anything not in-vocabulary — empty output, backend
    failure, or a non-contract answer — becomes "unreadable", which the
    veto treats as fail-closed (flagged). A frame we cannot classify is
    never installed sight-unseen.
    """
    toks = ((raw or "").strip().lower().split())
    lab = toks[0].strip(".,:;\"'") if toks else "unreadable"
    if lab in CLASSIFY_SYNONYMS:
        return INSTRUCTIONAL_LABEL
    if lab in KNOWN_LABELS:
        return lab
    return "unreadable"

# OCR text below this many characters behaves like a blank screen.
OCR_BLANK_CHARS = 12

# Sales-overlay copy is never instructional; veto frames whose visible text
# contains it even when the VLM label looks benign.
PROMO_TOKENS = ("promo", "sale", "discount", "coupon", "checkout", "% off")


def _ocr_data(im, config: str = "") -> dict[str, Any] | None:
    """Single tesseract pass; None when tesseract itself fails."""
    import pytesseract
    try:
        return pytesseract.image_to_data(im, config=config,
                                         output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001
        return None


def _ocr_reading(d: dict[str, Any], W: int, H: int) -> dict[str, Any]:
    texts = [t for t in d.get("text", []) if str(t).strip()]
    chars = " ".join(texts)
    hs = [h for h, t in zip(d.get("height", []), d.get("text", [])) if str(t).strip()]
    ws = [w for w, t in zip(d.get("width", []), d.get("text", [])) if str(t).strip()]
    return {"chars": len(chars), "text": chars.lower(),
            "max_h": round((max(hs) / H), 3) if hs else 0.0,
            "area": round(sum(w * h for w, h in zip(ws, hs)) / (W * H), 3) if hs else 0.0,
            "error": ""}


def ocr_stats(path: str) -> dict[str, Any]:
    """Deterministic OCR density signals (CPU only).

    Two segmentation passes: the default (PSM 3, fully automatic layout)
    misses text on photographed screens with moiré (it finds no text
    blocks at all), while PSM 6 (uniform text block) reads them. The
    richer reading wins; all density signals come from that same pass.
    """
    from PIL import Image

    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return {"chars": 0, "max_h": 0.0, "area": 0.0, "error": "unreadable"}
    W, H = im.size
    d3 = _ocr_data(im)
    d6 = _ocr_data(im, config="--psm 6")
    if d3 is None and d6 is None:
        return {"chars": 0, "max_h": 0.0, "area": 0.0, "error": "ocr-failed"}
    readings = [_ocr_reading(d, W, H) for d in (d3, d6) if d is not None]
    return max(readings, key=lambda r: r["chars"])


def classify_frame(backend, path: str) -> str:
    """Single-image decorative classification. Returns a label string."""
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return "unreadable"
    # Reasoning models spend completion tokens thinking before answering
    # (Halogen uses ~26 reasoning tokens on this prompt); a 20-token budget
    # starves the answer to empty, which vetoes every frame as unreadable.
    try:
        raw = backend.rank_images([im], CLASSIFY_PROMPT, max_new_tokens=150,
                                  temperature=0.0,
                                  system="Reply with only the label.")
    except Exception as exc:  # noqa: BLE001
        log.warning("classify failed: %s", exc)
        return "unreadable"
    return _canonical_label(raw)


def _vet_candidate(backend, cand: dict[str, Any],
                   expects_text: bool = False) -> dict[str, Any]:
    """Combined veto: VLM decorative label, OCR-empty screen.

    - A decorative VLM label vetoes, except `talking_head`, which requires
      corroboration (under 100 OCR chars): presenter picture-in-picture
      bubbles over genuine UI must not veto an otherwise good frame.
    - A blank-screen label against 40+ OCR chars is a known classifier
      misfire (e.g. bright browser panes) and does not veto.
    - Fewer than OCR_BLANK_CHARS characters vetoes when the section is about
      textual content; otherwise a textless action shot may be legitimate.
    - An unclassifiable frame (empty reply, backend failure, or
      out-of-vocabulary label) is flagged: with no evidence about the
      image, installing it would publish an unverified illustration.

    The verdict itself is computed by `_veto_from_label`, shared with the
    batched prefilter, so both paths apply identical canonical validation.
    """
    label = classify_frame(backend, cand["path"])
    stats = ocr_stats(cand["path"])
    return _veto_from_label(label, stats, expects_text)


def subject_of(backend, path: str, *,
                 cache: dict[str, Any] | None = None) -> str:
    """Plain-language main-subject description (finalist wrong-context backstop)."""
    key = f"subject:{path}"
    if cache is not None and key in cache:
        return cache[key]
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return ""
    try:
        raw = backend.rank_images(
            [im], "Describe the main subject of this image in 8 words or fewer. "
            "Reply with only the description.",
            max_new_tokens=30, temperature=0.0,
            system="Reply with only the description.")
    except Exception as exc:  # noqa: BLE001
        log.warning("subject_of failed: %s", exc)
        return ""
    desc = (raw or "").strip().lower()
    if cache is not None:
        cache[key] = desc
    return desc


def _subject_supported(desc: str, section: dict[str, Any]) -> bool:
    """The described subject shares a content token with the section."""
    from yt2wiki.records import content_tokens
    if not desc:
        return True  # undescribed: do not punish
    vis = {t for t in content_tokens(desc) if len(t) >= 4}
    sec = {t for t in content_tokens(
        (section.get("heading") or "") + " " + (section.get("body") or "")) if len(t) >= 4}
    return bool(vis & sec)


def _lexical_support(cand: dict[str, Any], section: dict[str, Any]) -> bool:
    """Visible text in the frame shares a content token with the section."""
    from yt2wiki.records import content_tokens
    import pytesseract
    from PIL import Image

    try:
        text = pytesseract.image_to_string(Image.open(cand["path"]).convert("RGB"))
    except Exception:  # noqa: BLE001
        return False
    vis = {t for t in content_tokens(text) if len(t) >= 4}
    sec = {t for t in content_tokens(
        (section.get("heading") or "") + " " + (section.get("body") or "")) if len(t) >= 4}
    return bool(vis & sec)


def _accept_replacement(backend, cand: dict[str, Any], section: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """A replacement must pass the veto AND show lexical support for the section."""
    chk = _vet_candidate(backend, cand, _section_expects_text(section))
    if chk["flagged"]:
        return False, chk
    if not _lexical_support(cand, section):
        chk = dict(chk, flagged=True,
                   reason=chk["reason"] + " + no-lexical-support")
        return False, chk
    return True, chk



def _first_clean_replacement(backend, section, current, alternatives, seen,
                             frames, history, max_rounds):
    """Targeted revision: first clean, lexically supported alternative in rank
    order (max 3 checks), then one expansion round of denser neighbours."""
    replacement = None
    checked = 0
    for alt in alternatives:
        if current is not None and alt["frame_id"] == current["frame_id"]:
            continue
        ok, chk = _accept_replacement(backend, alt, section)
        history.append({"round": 1, "current": alt["frame_id"], "check": chk})
        checked += 1
        if ok:
            replacement = alt
            break
        if checked >= 3:
            break
    if replacement is None and max_rounds >= 1:
        extra = expand_candidates(frames, section["t_start"], section["t_end"], seen, "")
        for c in extra:
            seen.add(c["frame_id"])
        for alt in extra[:6]:
            ok, chk = _accept_replacement(backend, alt, section)
            history.append({"round": 2, "current": alt["frame_id"], "check": chk})
            if ok:
                replacement = alt
                break
    return replacement


def adjudicate_section(backend, section: dict[str, Any], initial: dict[str, Any] | None,
                       alternatives: Sequence[dict[str, Any]],
                       frames: Sequence[dict[str, Any]],
                       *, max_rounds: int = 2, seed: int = 0,
                       frame_budget: int = 24) -> dict[str, Any]:
    """Bounded veto loop for one section. Returns the provenance record.

    The pipeline's initial pick is vetted (VLM decorative classification +
    deterministic OCR-density check). A flagged pick is replaced by the first
    clean alternative in rank order; one expansion round follows if nothing
    is clean, otherwise the section abstains. The final pick is re-verified.
    """
    started = time.time()
    seen = {c["frame_id"] for c in alternatives}
    if initial is not None:
        seen.add(initial["frame_id"])
    history: list[dict[str, Any]] = []
    inspected = 0

    _expects = _section_expects_text(section)
    def vet(c):
        nonlocal inspected
        inspected += 1
        return _vet_candidate(backend, c, _expects)

    current = initial
    first = vet(current) if current is not None else {
        "flagged": True, "reason": "no-initial-pick", "label": "", "ocr": {}}
    history.append({"round": 0, "current": current["frame_id"] if current else None,
                    "check": first})
    if not first["flagged"]:
        final, outcome = current, "kept-clean"
    else:
        replacement = _first_clean_replacement(backend, section, current,
                                               alternatives, seen, frames,
                                               history, max_rounds)
        if replacement is not None:
            final, outcome = replacement, "replaced"
        else:
            final, outcome = None, "abstained"
    # re-verify the final pick (must be clean, else abstain)
    if final is not None:
        chk = vet(final)
        history.append({"round": 99, "current": final["frame_id"], "check": chk,
                        "note": "re-verification"})
        if chk["flagged"]:
            final, outcome = None, "abstained"
    return {"section_id": section["section_id"], "initial": initial["frame_id"] if initial else None,
            "final": (final["frame_id"] if final else None),
            "outcome": outcome, "history": history,
            "frames_inspected": inspected,
            "elapsed_s": round(time.time() - started, 3)}


# ===========================================================================
# V9-Z1: rubric-based editorial judge with directed refinement.
# ===========================================================================

RUBRIC_CRITERIA = ("relevance", "clarity", "instructional", "payoff")

RUBRIC_PROMPT = """You are the editorial image judge for a technical how-to article. Score ONLY what is visible \
in the pixels. Text density is NOT required: a physical demonstration with no text can score highly. \
Never invent content. A reader understands an excellent frame directly from seeing it; prose is not needed.

Section heading: {heading}
Section paragraph:
\"\"\"
{body}
\"\"\"

The images are numbered 1..{n} in the order shown. Image {current_pos} is the current selection; the rest are \
alternatives. Score EACH image 0, 1 or 2 on:
- relevance: 0 wrong subject, action or context; 1 generally relevant; 2 the specific subject, action, \
configuration or result described above.
- clarity: 0 unclear, obscured or unreadable; 1 understandable; 2 immediately clear, well-framed and legible.
- instructional: 0 decorative or unhelpful; 1 supports the explanation; 2 clearly demonstrates what, where, \
how, or the expected outcome.
- payoff: 0 adds little to understanding; 1 useful additional visual detail; 2 reveals important implicit \
information or an exceptionally clear visual example.
Flag veto=true with a specific veto_reason for wrong-context, promotional/sales, or misleading material: \
a high total must never rescue these. Set context_ok=true only for a diagram or context image that is \
valuable despite relevance below 2.
Grade strictly: most candidates land at 3-6 total; award 2 only when fully earned. relevance 2 requires \
the specific subject, action, configuration or result NAMED in the paragraph to be visible (a related \
screen or title card is 1 at most). instructional 2 requires the image alone to show a reader what, where, \
how, or the outcome (a generic view of the right app is 1). clarity 2 requires the key content to be \
legible at a glance (small, blurry or cropped key content is 1). Title cards, logos, presenter shots and \
generic overviews score instructional 0. If the core action is missing from view, say so in weakness and \
keep instructional at 0 or 1.
Recommend one decision: KEEP (image {current_pos} is good enough, nothing materially better), REPLACE \
(another candidate is demonstrably better), EXPAND (candidates inadequate; more material needed), USE_PAIR \
(a before/after pair explains the operation better than one still; give pair), OMIT (no useful image).
Weaknesses and the explanation must cite visible specifics (e.g. "highlighted BIOS option is obscured", \
"candidate 3 exposes the connector orientation"). Never write generics like "better quality".
Refinement direction when EXPAND: earlier, later, denser (nearby frames at higher temporal resolution), \
clearer (unobstructed/closer/more legible), result (completed action or resulting state), pair \
(before/after around the action), or none.

Reply COMPACT JSON only: no markdown fences, no prose outside the JSON, one
line per candidate, weakness <= 12 words, explanation <= 30 words. Score ALL
{n} images, then stop immediately after the final closing brace.
First report images_received as the number of images numbered 1..{n} above.
If you received no images at all, reply {{"scores": [], "decision": "NO_IMAGE", "winner": 0,
"pair": [], "explanation": "NO_IMAGE", "refinement": {{"direction": "none", "detail": ""}},
"images_received": 0}}:
{{"scores": [{{"idx": 1, "relevance": 2, "clarity": 2, "instructional": 2, "payoff": 1, "veto": false, \
"veto_reason": "", "context_ok": false, "weakness": "..."}}],
"decision": "KEEP", "winner": 1, "pair": [], "explanation": "...",
"refinement": {{"direction": "none", "detail": "..."}}, "images_received": {n}}}"""

# Server-enforced shape for the rubric reply (json_schema/strict). The client
# still verifies images_received against the images actually shown and treats
# NO_IMAGE as unusable: a schema alone cannot stop a made-up verdict on a
# malformed (e.g. imageless) prompt.
RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "idx": {"type": "integer"},
                "relevance": {"type": "integer"},
                "clarity": {"type": "integer"},
                "instructional": {"type": "integer"},
                "payoff": {"type": "integer"},
                "veto": {"type": "boolean"},
                "veto_reason": {"type": "string"},
                "context_ok": {"type": "boolean"},
                "weakness": {"type": "string"},
            },
            "required": ["idx", "relevance", "clarity", "instructional",
                         "payoff", "veto", "veto_reason", "context_ok", "weakness"],
            "additionalProperties": False,
        }},
        "decision": {"type": "string",
                     "enum": ["KEEP", "REPLACE", "EXPAND", "USE_PAIR",
                              "OMIT", "NO_IMAGE"]},
        "winner": {"type": "integer"},
        "pair": {"type": "array", "items": {"type": "integer"}},
        "explanation": {"type": "string"},
        "refinement": {
            "type": "object",
            "properties": {
                "direction": {"type": "string"},
                "detail": {"type": "string"},
            },
            "required": ["direction", "detail"],
            "additionalProperties": False,
        },
        "images_received": {"type": "integer"},
    },
    "required": ["scores", "decision", "winner", "pair", "explanation",
                 "refinement", "images_received"],
    "additionalProperties": False,
}

# A single strong frame must normally show exactly what the section describes.
# Diagrams / context images may pass with relevance 1 via context_ok.
ACCEPT_MIN_TOTAL = 5
STRONG_MIN_TOTAL = 7
# Replacing the incumbent requires a material (not marginal) improvement.
REPLACE_MARGIN = 2



def _cached_labels(paths: Sequence[str], cache: dict[str, Any] | None) -> list[str]:
    """Cached pre-filter labels; the uncached positions start empty."""
    labels: list[str] = ["" for _ in paths]
    for i, p in enumerate(paths):
        key = f"veto-label:{p}"
        if cache is not None and key in cache:
            labels[i] = cache[key]
    return labels


def _classify_uncached(backend, paths: Sequence[str], labels: list[str],
                       cache: dict[str, Any] | None) -> int:
    """Label the not-yet-cached paths; returns 1 when a batched call was made."""
    todo_idx, todo_paths = [], []
    for i, p in enumerate(paths):
        if cache is None or f"veto-label:{p}" not in cache:
            if not labels[i]:
                todo_idx.append(i)
                todo_paths.append(p)
    if not todo_paths:
        return 0
    from PIL import Image
    images = []
    for p in todo_paths:
        try:
            images.append(Image.open(p).convert("RGB"))
        except Exception:  # noqa: BLE001
            images.append(None)
    raw = ["unreadable"] * len(todo_paths)
    good = [(k, im) for k, im in enumerate(images) if im is not None]
    if good:
        try:
            # See classify_frame: reasoning models need headroom
            # beyond the label itself or the answer comes back empty.
            out = backend.describe_batch(
                [im for _, im in good], CLASSIFY_PROMPT, max_new_tokens=150,
                temperature=0.0, system="Reply with only the label.",
                batch_size=4)
            for (k, _), r in zip(good, out):
                raw[k] = r
        except Exception as exc:  # noqa: BLE001
            log.warning("batched classify failed: %s", exc)
    for i, p, r in zip(todo_idx, todo_paths, raw):
        lab = _canonical_label(r)
        labels[i] = lab
        if cache is not None:
            cache[f"veto-label:{p}"] = lab
    return 1


def classify_many(backend, paths: Sequence[str], *,
                  cache: dict[str, Any] | None = None,
                  expects_text: bool = False) -> list[dict[str, Any]]:
    """Batched decorative veto pre-filter: one prompt per image, packed forward passes."""
    labels = _cached_labels(paths, cache)
    _classify_uncached(backend, paths, labels, cache)
    vetoed = []
    for p, lab in zip(paths, labels):
        stats = ocr_stats(p)
        vetoed.append(_veto_from_label(lab, stats, expects_text))
    return [{"path": p, "label": lab, "veto": v["flagged"], "reason": v["reason"], "ocr": v["ocr"]}
            for p, lab, v in zip(paths, labels, vetoed)]


def _veto_from_label(label: str, stats: dict[str, Any],
                   expects_text: bool = False) -> dict[str, Any]:
    """Apply the V9-Z veto rules to an already-classified label (no VLM call).

    An unclassifiable frame (backend failure, empty output) is flagged:
    with no evidence about the image, installing it would publish an
    unverified illustration.
    """
    if not label or label == "unreadable":
        return {"flagged": True, "reason": "unclassifiable-label",
                "label": label, "ocr": stats}
    if label in DECORATIVE_LABELS:
        if label == "blank_screen" and stats.get("chars", 0) >= 40:
            return {"flagged": False,
                    "reason": f"contested label={label} chars={stats['chars']}",
                    "label": label, "ocr": stats}
        if label == "talking_head" and stats.get("chars", 0) >= 100:
            return {"flagged": False,
                    "reason": f"contested label={label} chars={stats['chars']}",
                    "label": label, "ocr": stats}
        return {"flagged": True, "reason": f"vlm-label={label}", "label": label,
                "ocr": stats}
    if stats.get("error") in ("unreadable", "ocr-failed"):
        return {"flagged": True, "reason": f"ocr-{stats['error']}", "label": label,
                "ocr": stats}
    if stats.get("chars", 0) < OCR_BLANK_CHARS and expects_text:
        return {"flagged": True, "reason": f"blank-screen chars={stats.get('chars', 0)}",
                "label": label, "ocr": stats}
    if any(p in stats.get("text", "") for p in PROMO_TOKENS):
        return {"flagged": True, "reason": "promo-overlay", "label": label,
                "ocr": stats}
    return {"flagged": False, "reason": f"clean label={label} chars={stats.get('chars', 0)}",
            "label": label, "ocr": stats}


def _parse_rubric(raw: str, n: int) -> dict[str, Any] | None:
    """Parse and validate the judge's JSON. Returns None when unusable.

    Uses tolerant first-object decoding (leading fences / trailing prose
    are ignored) but strict top-level schema validation: scores, decision,
    winner and explanation must live at the top level. A response that
    nests the verdict inside the scores array is rejected rather than
    silently defaulted.
    """
    try:
        start = raw.index("{")
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    scores = data.get("scores")
    if not isinstance(scores, list) or not scores:
        return None
    if not isinstance(data.get("decision"), str):
        return None
    if not isinstance(data.get("winner"), int) or isinstance(data.get("winner"), bool):
        return None
    if not isinstance(data.get("explanation"), str):
        return None
    clean = _parse_score_entries(scores, n)
    if not _rubric_grounded(clean, n, data):
        return None
    verdict = _parse_verdict(data)
    if verdict is None:
        return None
    return {"scores": clean, **verdict,
            "explanation": str(data.get("explanation") or "")[:600]}


def _parse_score_entries(scores: list, n: int) -> list[dict[str, Any]]:
    """Per-candidate rubric entries; unusable entries are skipped."""
    clean = []
    for s in scores:
        if not isinstance(s, dict):
            continue
        idx = s.get("idx")
        # Scores must be real JSON integers: bool/float/str approximations
        # (true, 1.9, "1") are skipped, never coerced into an index.
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if not 1 <= idx <= n:
            continue
        entry = {"idx": idx, "veto": bool(s.get("veto", False)),
                 "veto_reason": str(s.get("veto_reason") or "")[:200],
                 "context_ok": bool(s.get("context_ok", False)),
                 "weakness": str(s.get("weakness") or "")[:300]}
        total = 0
        for crit in RUBRIC_CRITERIA:
            try:
                v = int(s.get(crit, 0))
            except (TypeError, ValueError):
                v = 0
            v = max(0, min(2, v))
            entry[crit] = v
            total += v
        entry["total"] = total
        clean.append(entry)
    return clean


def _rubric_grounded(clean: list[dict[str, Any]], n: int, data: dict[str, Any]) -> bool:
    """Grounding check: every shown image scored exactly once, count honest.

    A schema cannot stop a made-up verdict on a malformed prompt, so a
    partial or padded score set fails the round outright and the caller
    falls back to the veto loop.
    """
    if sorted(e["idx"] for e in clean) != list(range(1, n + 1)):
        return False
    # The count must be a real JSON integer: bool/float/str approximations
    # (true, 2.9, "2") fail even when they would coerce to the right number.
    received = data.get("images_received")
    return (isinstance(received, int) and not isinstance(received, bool)
            and received == n)


def _parse_verdict(data: dict[str, Any]) -> dict[str, Any] | None:
    """Decision/winner/pair/refinement; None when the model saw no images."""
    decision = str(data.get("decision") or "").upper()
    if decision == "NO_IMAGE":
        # The model reports it saw no images: never install on this verdict.
        return None
    if decision not in ("KEEP", "REPLACE", "EXPAND", "USE_PAIR", "OMIT"):
        decision = "KEEP"
    try:
        winner = int(data.get("winner") or 0)
    except (TypeError, ValueError):
        winner = 0
    pair = []
    for p in (data.get("pair") or [])[:2]:
        try:
            pair.append(int(p))
        except (TypeError, ValueError):
            continue
    ref = data.get("refinement") or {}
    direction = str(ref.get("direction") or "none").lower()
    if direction not in ("none", "earlier", "later", "denser", "clearer", "result", "pair"):
        direction = "none"
    return {"decision": decision, "winner": winner, "pair": pair,
            "refinement": {"direction": direction,
                           "detail": str(ref.get("detail") or "")[:300]}}


def score_candidates(backend, section: dict[str, Any],
                     ordered: Sequence[dict[str, Any]],
                     current_pos: int, *,
                     cache: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, int]:
    """One comparative forward pass: rubric scores for the shown candidates.

    Returns (parsed judgement, vlm_calls). Positions map back to frame_ids by
    the caller; frame_ids are never shown to the judge.
    """
    images = _load_slot_images(ordered)
    if images is None:
        return None, 0
    key = _rubric_cache_key(section, ordered)
    if cache is not None and key in cache:
        return cache[key], 0
    prompt = RUBRIC_PROMPT.format(
        heading=(section.get("heading") or "")[:150],
        body=(section.get("body") or "")[:1200],
        n=len(ordered), current_pos=current_pos)
    parsed, calls = _rubric_pass(backend, images, prompt, ordered)
    if cache is not None and parsed is not None:
        cache[key] = parsed
    return parsed, calls


def _load_slot_images(ordered: Sequence[dict[str, Any]]) -> list | None:
    """Slot-per-candidate images; None when every file is unreadable."""
    from PIL import Image

    images = []
    for c in ordered:
        try:
            images.append(Image.open(c["path"]).convert("RGB"))
        except Exception:  # noqa: BLE001
            images.append(None)
    if not any(im is not None for im in images):
        return None
    # Unreadable files still occupy their numbered slot (blank) so positions hold.
    blank = Image.new("RGB", (64, 64), (0, 0, 0))
    return [im if im is not None else blank for im in images]


def _rubric_cache_key(section: dict[str, Any],
                      ordered: Sequence[dict[str, Any]]) -> str:
    return "rubric:" + hashlib.sha1(
        ("|".join(c["frame_id"] for c in ordered) + "|" +
         (section.get("heading") or "")[:80]).encode()).hexdigest()[:16]


def _rubric_pass(backend, images: list, prompt: str,
                 ordered: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    """Two attempts (retry compact at temperature 0.3); (verdict, calls)."""
    calls = 0
    schema_kw: dict[str, Any] = {}
    if getattr(backend, "supports_structured_schema", False):
        schema_kw = {"response_schema": RUBRIC_SCHEMA, "schema_name": "rubric"}
    for attempt in range(2):
        try:
            raw = backend.rank_images(
                images, prompt,
                max_new_tokens=1100,
                temperature=0.0 if attempt == 0 else 0.3,
                system="You judge images for technical documentation. Reply JSON only.",
                **schema_kw)
            calls += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("rubric scoring failed: %s", exc)
            raw = ""
        parsed = _parse_rubric(raw or "", len(ordered))
        if parsed is not None:
            return parsed, calls
        prompt = ("COMPACT JSON only, one line per candidate, no fences, no extra "
                  "prose. " + prompt)
    return None, calls


def _decide(scored: list[dict[str, Any]], current_pos: int, *,
            rounds_left: int) -> dict[str, Any]:
    """Pure decision policy over validated rubric scores (no I/O; unit-tested).

    Each entry: idx, total, relevance, veto, context_ok. Returns
    {action, winner_idx|None, pair|[], reason}.
    """
    eligible = [s for s in scored
                if not s["veto"] and (s["relevance"] == 2 or s["context_ok"])]
    if not eligible:
        if rounds_left > 0:
            return {"action": "EXPAND", "winner_idx": None, "pair": [],
                    "reason": "no eligible candidate; refinement needed"}
        return {"action": "OMIT", "winner_idx": None, "pair": [],
                "reason": "no eligible candidate after refinement"}
    # Highest total wins; ties prefer the incumbent, then the earliest slot.
    best = sorted(eligible, key=lambda s: (
        -s["total"], 0 if s["idx"] == current_pos else 1, s["idx"]))[0]
    cur = next((s for s in eligible if s["idx"] == current_pos), None)
    if best["total"] < ACCEPT_MIN_TOTAL:
        if rounds_left > 0:
            return {"action": "EXPAND", "winner_idx": best["idx"], "pair": [],
                    "reason": f"best total {best['total']} below accept threshold"}
        return {"action": "OMIT", "winner_idx": None, "pair": [],
                "reason": f"best total {best['total']} below accept threshold"}
    if cur is not None and (best["idx"] == current_pos
                            or best["total"] - cur["total"] < REPLACE_MARGIN):
        return {"action": "KEEP", "winner_idx": current_pos, "pair": [],
                "reason": ("incumbent best or within margin "
                            f"({cur['total']} vs best {best['total']})")}
    return {"action": "REPLACE", "winner_idx": best["idx"], "pair": [],
            "reason": (f"candidate {best['idx']} total {best['total']} beats "
                       f"incumbent {cur['total'] if cur else 'none'} by material margin")}


def refine_candidates(frames: Sequence[dict[str, Any]], section: dict[str, Any],
                      seen: set[str], direction: str, detail: str,
                      anchor_t: float | None = None, *,
                      pad: float = 45.0, limit: int = 8) -> list[dict[str, Any]]:
    """Directed retrieval honouring the judge's refinement request."""
    t0, t1 = float(section["t_start"]), float(section["t_end"])
    mid = (t0 + t1) / 2
    if direction == "earlier":
        lo, hi = t0 - pad, mid
    elif direction in ("later", "result"):
        lo, hi = mid, t1 + pad
    elif direction == "denser":
        c = anchor_t if anchor_t is not None else mid
        lo, hi, limit = c - 15.0, c + 15.0, 10
    else:  # clearer, pair, or unspecified: fresh faces across a wide window
        lo, hi = t0 - pad, t1 + pad
    cands = [f for f in frames if lo <= float(f["t"]) <= hi and f["frame_id"] not in seen]
    cands.sort(key=lambda f: (
        0 if str(f.get("kind")) in ("scene", "keyframe") else 1, float(f["t"])))
    if direction == "pair" and len(cands) >= 2:
        # Widest temporal span: likely before/after states of the operation.
        return [cands[0], cands[-1]]
    if len(cands) > limit:
        step = len(cands) / limit
        cands = [cands[int(i * step)] for i in range(limit)]
    return cands


def adjudicate_section_z1(backend, section: dict[str, Any], initial: dict[str, Any] | None,
                          alternatives: Sequence[dict[str, Any]],
                          frames: Sequence[dict[str, Any]], *,
                          max_rounds: int = 2, seed: int = 0,
                          frame_budget: int = 24, alt_limit: int = 3,
                          cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rubric-based editorial judgement for one section. Returns the evidence record.

    Flow: inexpensive batched veto pre-filter -> comparative rubric scoring of
    the incumbent plus strongest alternatives against the section paragraph ->
    threshold policy (KEEP / REPLACE / USE_PAIR / OMIT) -> up to `max_rounds`
    directed refinement rounds (EXPAND) -> re-verified final. Falls back to
    the V9-Z veto loop when comparative scoring fails.
    """
    started = time.time()
    if cache is None:
        cache = {}
    cache_before = len(cache)
    counters = {"vlm": 0, "inspected": 0}
    rounds: list[dict[str, Any]] = []
    _expects = _section_expects_text(section)

    pool, seen, verdict = _z1_pool(backend, section, initial, alternatives,
                                   alt_limit, frame_budget, cache, _expects,
                                   counters)

    def shown_set(extra=()):
        return _z1_shown_set(initial, alternatives, extra, alt_limit)

    # --- round 0: comparative scoring of incumbent + strongest alternatives ---
    shown = shown_set()
    if not shown:
        return _z1_record(section, initial, [], rounds, None, "OMIT",
                          "no candidates available", started,
                          counters["inspected"], counters["vlm"],
                          cache_before, cache)
    # Honour the frame budget across refinement rounds.
    shown = shown[:alt_limit + 1]

    def score_round(shown_cands, rnd, rng_seed):
        parsed, ordered, cur_pos, calls = _z1_score_round(
            backend, section, shown_cands, initial, rng_seed, cache, verdict)
        counters["vlm"] += calls
        return parsed, ordered, cur_pos

    parsed, ordered, cur_pos = score_round(shown, 0, seed)
    if parsed is None:
        # Comparative scoring failed: fall back to the V9-Z veto loop.
        fb = adjudicate_section(backend, section, initial, alternatives, frames,
                                max_rounds=max_rounds, seed=seed,
                                frame_budget=frame_budget)
        fb["z1_fallback"] = "v9z-veto"
        fb["z1_rounds"] = []
        return fb
    rounds.append({"round": 0, "shown": [c["frame_id"] for c in ordered],
                     "judgement": parsed})

    state = _Z1State(parsed=parsed, ordered=ordered, cur_pos=cur_pos, seed=seed)

    def refine(shown2):
        state.refinement_rounds += 1
        parsed2, ordered2, cur_pos2 = score_round(shown2, state.refinement_rounds,
                                                  seed + state.refinement_rounds)
        return parsed2, ordered2, cur_pos2

    decision, reason, pair_ids = _z1_policy_loop(
        backend, section, frames, seen, verdict, cache, _expects,
        max_rounds, state, rounds, score_round, refine)

    final_ids = _z1_final_ids(decision, pair_ids, state, initial)
    # re-verify finals through the single-frame veto (never publish knowingly poor)
    if final_ids:
        final_ids, reason = _z1_verify_finals(
            backend, section, state, pool, frames, _expects, cache, counters,
            final_ids, reason)
        if not final_ids:
            decision = "OMIT"
            reason = reason + " (all finals failed re-verification)"
    return _z1_record(section, initial,
                      [c["frame_id"] for c in shown], rounds, final_ids,
                      decision or "OMIT", reason, started,
                      counters["inspected"], counters["vlm"],
                      cache_before, cache)



class _Z1State:
    """Mutable rubric-round state (rescored in place during refinement)."""

    def __init__(self, *, parsed: dict[str, Any], ordered: list,
                 cur_pos: int, seed: int) -> None:
        self.parsed = parsed
        self.ordered = ordered
        self.cur_pos = cur_pos
        self.seed = seed
        self.refinement_rounds = 0


def _z1_pool(backend, section, initial, alternatives, alt_limit, frame_budget,
             cache, _expects, counters):
    """Budgeted candidate pool plus its batched veto pre-filter."""
    pool = ([initial] if initial is not None else []) + list(alternatives[:alt_limit + 2])
    # de-duplicate while preserving rank order
    uniq, seen_ids = [], set()
    for c in pool:
        if c["frame_id"] not in seen_ids:
            uniq.append(c)
            seen_ids.add(c["frame_id"])
    pool = uniq[:frame_budget]
    seen = {c["frame_id"] for c in pool}
    pre = classify_many(backend, [c["path"] for c in pool],
                        cache=cache, expects_text=_expects)
    counters["vlm"] += 1  # one batched classify_many invocation
    counters["inspected"] += len(pool)
    verdict = {c["frame_id"]: v for c, v in zip(pool, pre)}
    return pool, seen, verdict


def _z1_shown_set(initial, alternatives, extra, alt_limit) -> list:
    """Incumbent plus up to alt_limit alternatives (rank order, deduped)."""
    shown = []
    if initial is not None:
        shown.append(initial)
    for c in list(alternatives) + list(extra):
        if len(shown) >= alt_limit + 1:
            break
        if c["frame_id"] not in {s["frame_id"] for s in shown}:
            shown.append(c)
    return shown


def _z1_score_round(backend, section, shown_cands, initial, rng_seed, cache,
                    verdict):
    """One comparative scoring pass; fuses the pre-filter veto into scores."""
    order = list(range(len(shown_cands)))
    random.Random(rng_seed).shuffle(order)  # S2245: non-cryptographic deterministic shuffle for presentation order
    ordered = [shown_cands[i] for i in order]
    if initial is not None and any(s["frame_id"] == initial["frame_id"]
                                   for s in shown_cands):
        cur_pos = next(i for i, s in enumerate(ordered)
                       if s["frame_id"] == initial["frame_id"])
    else:
        cur_pos = -1
    parsed, calls = score_candidates(backend, section, ordered, cur_pos + 1,
                                     cache=cache)
    if parsed is None:
        return None, ordered, cur_pos, calls
    # fuse rubric veto with pre-filter veto (pre-filter keyed by frame_id)
    for s in parsed["scores"]:
        f = ordered[s["idx"] - 1]
        s["frame_id"] = f["frame_id"]
        pv = verdict.get(f["frame_id"])
        if pv is not None and pv["veto"]:
            s["veto"] = True
            prev = s.get("veto_reason") or ""
            join = " + " if prev else ""
            s["veto_reason"] = (f"prefilter: {pv['reason']}" + join + prev)[:200]
    return parsed, ordered, cur_pos, calls


def _z1_use_pair(parsed: dict[str, Any], by_idx: dict[int, dict[str, Any]],
                 ordered: list) -> tuple[str, str, list[str]] | None:
    """USE_PAIR when the judge nominates two eligible, non-dominant candidates."""
    scored = parsed["scores"]
    if (parsed["decision"] != "USE_PAIR" or len(parsed["pair"]) != 2
            or not all(p in by_idx and not by_idx[p]["veto"]
                       and (by_idx[p]["relevance"] == 2 or by_idx[p]["context_ok"])
                       for p in parsed["pair"])):
        return None
    best_single = max((s["total"] for s in scored if not s["veto"]), default=0)
    if best_single >= STRONG_MIN_TOTAL:
        return None
    return ("USE_PAIR",
            (parsed["explanation"] or "judge-nominated before/after pair")[:300],
            [ordered[p - 1]["frame_id"] for p in parsed["pair"][:2]])


def _z1_eligible_strong(s: dict[str, Any] | None) -> bool:
    return (s is not None and not s["veto"]
            and (s["relevance"] == 2 or s["context_ok"]))


def _z1_policy_loop(backend, section, frames, seen, verdict, cache, _expects,
                    max_rounds, state: _Z1State, rounds, score_round,
                    refine):
    """Threshold policy (KEEP/REPLACE/USE_PAIR/OMIT) with EXPAND refinement.

    Returns (decision, reason, pair_ids). The loop only iterates through
    refinement rescoring, exactly as the original implementation did.
    """
    decision: str | None = None
    reason = ""
    pair_ids: list[str] = []
    while True:
        parsed = state.parsed
        scored = parsed["scores"]
        policy = _decide(scored, state.cur_pos + 1,
                         rounds_left=max_rounds - state.refinement_rounds)
        jdec = parsed["decision"]
        by_idx = {s["idx"]: s for s in scored}
        pair = _z1_use_pair(parsed, by_idx, state.ordered)
        if pair is not None:
            decision, reason, pair_ids = pair
            break
            # USE_PAIR check matched; nothing else applies this round.
        if policy["action"] == "EXPAND" and state.refinement_rounds < max_rounds:
            outcome = _z1_expand_round(
                backend, section, frames, seen, verdict, cache, _expects,
                state, rounds, by_idx, policy, refine)
            if outcome is None:
                continue  # rescored: policy re-runs on the new state
            kind, payload = outcome
            if kind == "overruled":
                # Strong incumbent despite EXPAND advice: keep, record dissent.
                decision = "KEEP (judge EXPAND overruled: strong incumbent)"
                reason = payload
                break
            decision, reason = "OMIT", payload
            break
        elif policy["action"] == "EXPAND":
            decision, reason = "OMIT", policy["reason"]
            break
        decision, reason = policy["action"], policy["reason"]
        if jdec != decision and jdec in ("KEEP", "REPLACE", "OMIT"):
            reason += f" (judge advised {jdec})"
        break
    return decision, reason, pair_ids


def _z1_expand_round(backend, section, frames, seen, verdict, cache, _expects,
                     state: _Z1State, rounds, by_idx, policy, refine
                     ) -> tuple[str, str] | None:
    """Handle an EXPAND policy: overrule, omit, or refine and rescore.

    Returns None when the loop should re-run policy on the rescored state,
    or ("omit", reason) / ("overruled", reason) when refinement ended the
    section.
    """
    parsed = state.parsed
    scored = parsed["scores"]
    direction = parsed["refinement"]["direction"]
    incumb = by_idx.get(state.cur_pos + 1) if state.cur_pos >= 0 else None
    if _z1_eligible_strong(incumb) and incumb["total"] >= STRONG_MIN_TOTAL:
        return ("overruled", policy["reason"])
    if direction == "none" and not any(not s["veto"] for s in scored):
        return ("omit", "no eligible candidate and no directed refinement request")
    anchor = None
    w = policy.get("winner_idx")
    if w and w in by_idx:
        anchor = float(state.ordered[w - 1]["t"])
    new = refine_candidates(frames, section, seen, direction,
                            parsed["refinement"]["detail"], anchor_t=anchor)
    for c in new:
        seen.add(c["frame_id"])
    new_pre = classify_many(backend, [c["path"] for c in new],
                            cache=cache, expects_text=_expects)
    for c, v in zip(new, new_pre):
        verdict[c["frame_id"]] = v
    clean_new = [c for c, v in zip(new, new_pre) if not v["veto"]][:2]
    rounds.append({"round": state.refinement_rounds + 1,
                   "refinement": parsed["refinement"],
                   "retrieved": [c["frame_id"] for c in new],
                   "judgement": None})
    if not clean_new:
        return ("omit", f"refinement '{direction}' retrieved nothing clean")
    # Rescore: incumbent winner + fresh material.
    keep = policy.get("winner_idx")
    base = []
    if keep and keep in by_idx:
        base = [state.ordered[keep - 1]]
    shown2 = base + [c for c in clean_new
                     if c["frame_id"] not in {b["frame_id"] for b in base}]
    parsed2, ordered2, cur_pos2 = refine(shown2)
    if parsed2 is None:
        return ("omit", "rescoring failed after refinement")
    state.parsed, state.ordered, state.cur_pos = parsed2, ordered2, cur_pos2
    rounds[-1]["judgement"] = parsed2
    rounds[-1]["shown"] = [c["frame_id"] for c in ordered2]
    return None


def _z1_final_ids(decision: str | None, pair_ids: list[str],
                  state: _Z1State, initial) -> list[str] | None:
    """Winner ids for the decided action (deterministic recompute, never EXPAND)."""
    if decision == "USE_PAIR":
        return pair_ids
    if decision in ("KEEP", "REPLACE") or (decision or "").startswith("KEEP"):
        w = _decide(state.parsed["scores"], state.cur_pos + 1, rounds_left=0)
        final_ids = ([state.ordered[w["winner_idx"] - 1]["frame_id"]]
                     if w["winner_idx"] else None)
        if decision.startswith("KEEP") and initial is not None:
            final_ids = [initial["frame_id"]]
        return final_ids
    return None


def _z1_verify_finals(backend, section, state: _Z1State, pool, frames,
                      _expects, cache, counters, final_ids: list[str],
                      reason: str):
    """Re-verify every finalist; drop vetoed ones (never publish knowingly poor)."""
    for fid in list(final_ids):
        cand = next((c for c in pool if c["frame_id"] == fid),
                    next((c for c in frames if c["frame_id"] == fid), None))
        if cand is None:
            final_ids.remove(fid)
            continue
        chk = _vet_candidate(backend, cand, _expects)
        counters["inspected"] += 1
        counters["vlm"] += 1
        if chk["flagged"]:
            final_ids.remove(fid)
            reason += f" (re-verification vetoed {fid}: {chk['reason']})"
            continue
        weak = next((str(s.get("weakness") or "") for s in state.parsed["scores"]
                     if s.get("frame_id") == fid), "")
        if len(weak.split()) < 6:
            desc = subject_of(backend, cand["path"], cache=cache)
            counters["vlm"] += 1
            if not _subject_supported(desc, section):
                expl = str(state.parsed.get("explanation") or "")
                if expl.strip() and _subject_supported(expl, section):
                    reason += (f" (subject probe kept {fid} on explanation "
                               f"overlap despite: {desc!r})")
                else:
                    final_ids.remove(fid)
                    reason += (f" (subject backstop dropped {fid}: "
                               f"described as {desc!r}, no section overlap)")
                    continue
        else:
            reason += f" (subject probe skipped for {fid}: specific weakness)"
    return final_ids, reason


def _z1_record(section, initial, shown_ids, rounds, final_ids, decision, reason,
               started, inspected, vlm_calls, cache_before, cache) -> dict[str, Any]:
    return {"section_id": section["section_id"],
            "initial": initial["frame_id"] if initial else None,
            "shown": shown_ids,
            "final": final_ids,
            "decision": decision,
            "reason": reason,
            "rounds": rounds,
            "frames_inspected": inspected,
            "vlm_calls": vlm_calls,
            "cache_entries": len(cache) - cache_before,
            "elapsed_s": round(time.time() - started, 3)}
