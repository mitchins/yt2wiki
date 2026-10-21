"""Read models for the web pages: library cards, reader context, job views.

Everything here is derived from real database rows and files on disk; a
missing thumbnail or report simply leaves that part of the page out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import reader

# Writer-assigned article kinds (v9_write metrics) -> reader-facing label.
KIND_LABELS = {
    "procedure": "How-to",
    "explainer": "Explainer",
    "experimental": "Experiment",
    "experiment": "Experiment",
    "comparison": "Comparison",
    "teardown": "Teardown",
    "review": "Review",
}

STATUS_LABELS = {
    "published": ("Published", "ok"),
    "review": ("Needs review", "warn"),
    "draft": ("Draft", "muted"),
    "archived": ("Archived", "muted"),
}

# V9 job stages in run order, with reader-facing labels.  The base stages
# are the shared acquisition/ASR steps that V9 reuses when already done.
JOB_STAGES = (
    ("acquisition", "Fetch the video"),
    ("inspect", "Inspect media"),
    ("transcribe", "Transcribe the narration"),
    ("frame_candidates", "Collect source frames"),
    ("v9_write", "Write the guide"),
    ("v9_discover", "Gather frames per section"),
    ("v9_select", "Choose the images"),
    ("v9_caption", "Captions"),
    ("v9_publish", "Publish"),
)
_STAGE_LABEL = dict(JOB_STAGES)

JOB_STATE_LABELS = {
    "queued": ("Queued", "muted"),
    "running": ("Processing", "run"),
    "done": ("Done", "ok"),
    "failed": ("Failed", "bad"),
    "cancelled": ("Cancelled", "muted"),
    "interrupted": ("Interrupted", "warn"),
    "stale": ("Not scheduled", "muted"),
}


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def kind_label(kind: str | None) -> str:
    return KIND_LABELS.get((kind or "").lower(), "")


def status_view(status: str | None) -> dict[str, str]:
    label, tone = STATUS_LABELS.get(status or "", ((status or "").title(), "muted"))
    return {"label": label, "tone": tone, "value": status or ""}


def source_label(src: dict[str, Any] | None) -> dict[str, str]:
    """Attribution bits for a source; uploads have no uploader or URL."""
    if not src:
        return {"title": "", "by": "", "where": "", "url": ""}
    url = src.get("origin_url") or ""
    if src.get("kind") == "youtube" or "youtu" in url:
        return {"title": src.get("title") or "", "by": src.get("uploader") or "",
                "where": "YouTube", "url": url}
    return {"title": src.get("title") or "", "by": src.get("uploader") or "",
            "where": "Local video", "url": ""}


def _article_kinds(db, job_ids: list[str]) -> dict[str, str]:
    ids = [j for j in job_ids if j]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.query(
        f"SELECT job_id, metrics_json FROM stages WHERE stage='v9_write' AND job_id IN ({marks})",
        ids)
    return {r["job_id"]: (_loads(r["metrics_json"], {}) or {}).get("kind", "") for r in rows}


def thumbnail_for(paths, article_id: str, outline_json: str | None, markdown: str) -> str | None:
    """Best representative thumbnail: prefer a middle-section installed frame.

    The first installed frame is often an intro/title card; the most
    instructional image is usually in the middle third of the article.
    We score outline order by distance from the middle, falling back to
    markdown order and finally to any asset on disk. Graceful text-only
    when nothing suitable exists.
    """
    assets = paths.article_assets(article_id)
    # Outline order is already instructionally useful (vetoed decorative
    # images never reach outline). Prefer the middle section's frame.
    outline = _loads(outline_json, []) or []
    names: list[str] = []
    for sec in outline:
        names.extend(f"{fid}.jpg" for fid in (sec.get("frames") or []))
    if names:
        # Reorder by proximity to the centre section
        m = len(outline) // 2
        ordered_secs = sorted(outline, key=lambda s: abs(outline.index(s) - m))
        ordered_names: list[str] = []
        for sec in ordered_secs:
            ordered_names.extend(f"{fid}.jpg" for fid in (sec.get("frames") or []))
        for name in ordered_names:
            if (assets / name).is_file():
                return f"/assets/{article_id}/{name}"
        # Fallback to original order
        for name in names:
            if (assets / name).is_file():
                return f"/assets/{article_id}/{name}"
    if not names:
        import re
        names = re.findall(r"!\[[^\]]*\]\(assets/([^)\s/]+)\)", markdown or "")
        for name in names:
            if (assets / name).is_file():
                return f"/assets/{article_id}/{name}"
    # Final fallback: any image asset (should not happen for published)
    try:
        for p in sorted(assets.iterdir()):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return f"/assets/{article_id}/{p.name}"
    except OSError:
        pass
    return None


def _is_v9(article_id: str) -> bool:
    return article_id.startswith("art9-")


def library_cards(db, paths, *, q: str = "", fts_query: str = "") -> list[dict[str, Any]]:
    """One card per source: the V9 article when present, else the newest other.

    With a search query, cards are FTS5 matches in rank order and carry a
    highlighted snippet; superseded legacy articles are still excluded.
    """
    base = """SELECT a.article_id, a.source_id, a.job_id, a.title, a.markdown, a.status,
                     a.version, a.outline_json, a.created_at, a.updated_at,
                     s.kind AS source_kind, s.title AS source_title, s.uploader,
                     s.origin_url, s.duration_s"""
    if q:
        rows = db.query(
            base + """, snippet(articles_fts, 2, '[[', ']]', '…', 18) AS snip
               FROM articles_fts f JOIN articles a ON a.article_id=f.article_id
               JOIN sources s ON s.source_id=a.source_id
               WHERE articles_fts MATCH ? ORDER BY rank LIMIT 60""",
            (fts_query,))
    else:
        rows = db.query(
            base + """, '' AS snip FROM articles a JOIN sources s ON s.source_id=a.source_id
               ORDER BY a.created_at DESC""")
    v9_sources = {r["source_id"] for r in db.query(
        "SELECT source_id FROM articles WHERE article_id LIKE 'art9-%'")}
    seen: set[str] = set()
    picked = []
    for r in rows:
        if r["source_id"] in seen:
            continue
        if not _is_v9(r["article_id"]) and r["source_id"] in v9_sources:
            continue  # superseded by the V9 article; still reachable from it
        seen.add(r["source_id"])
        picked.append(dict(r))
    kinds = _article_kinds(db, [r["job_id"] for r in picked])
    cards = []
    for r in picked:
        cards.append({
            "article_id": r["article_id"],
            "title": r["title"],
            "summary": reader.lead_summary(r["markdown"] or "", 200),
            "snippet": _snippet_html(r.get("snip") or ""),
            "thumb": thumbnail_for(paths, r["article_id"], r["outline_json"], r["markdown"]),
            "kind": kind_label(kinds.get(r["job_id"] or "")),
            "status": status_view(r["status"]),
            "source": source_label({"kind": r["source_kind"], "title": r["source_title"],
                                    "uploader": r["uploader"], "origin_url": r["origin_url"]}),
            "created_at": r["created_at"],
            "edited": int(r["version"] or 1) > 1,
            "legacy": not _is_v9(r["article_id"]),
        })
    return cards


def _snippet_html(snip: str) -> str:
    """Escape an FTS snippet, then turn its [[ ]] markers into <mark>."""
    import html
    import re

    if not snip:
        return ""
    text = html.escape(snip)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\(https?://[^)\s]*\)", "", text)
    text = re.sub(r"[#*`>]+", "", text)
    text = text.replace("[[", "<mark>").replace("]]", "</mark>")
    return " ".join(text.split())


# ----------------------------------------------------------------------
# reader
def _asset_resolver(paths, article_id: str):
    assets = paths.article_assets(article_id)
    sizes: dict[str, tuple[int, int] | None] = {}

    def url(name: str) -> str | None:
        if "/" in name or ".." in name:
            return None
        return f"/assets/{article_id}/{name}" if (assets / name).is_file() else None

    def size(name: str) -> tuple[int, int] | None:
        if name not in sizes:
            try:
                from PIL import Image
                with Image.open(assets / name) as im:
                    sizes[name] = im.size
            except Exception:  # noqa: BLE001
                sizes[name] = None
        return sizes[name]

    return url, size


def render_reader(paths, article_id: str, markdown: str, *, numbered: bool = False,
                  zoom_links: bool = True) -> dict[str, Any]:
    from ..article import markdown_to_html

    parts = reader.split_markdown(markdown or "")
    url, size = _asset_resolver(paths, article_id)
    lead_html, _ = reader.enhance(markdown_to_html(parts.lead_md), asset_url=url,
                                  asset_size=size, zoom_links=zoom_links) if parts.lead_md else ("", [])
    body_html, toc = reader.enhance(markdown_to_html(parts.body_md), asset_url=url,
                                    asset_size=size, zoom_links=zoom_links,
                                    numbered_sections=numbered)
    return {"title": parts.title, "lead_html": lead_html, "body_html": body_html,
            "toc": toc, "attribution": parts.attribution,
            "n_images": body_html.count('<figure class="frame"')}


def article_context(db, paths, article_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
    if row is None:
        return None
    article = dict(row)
    src_row = db.query_one("SELECT * FROM sources WHERE source_id=?", (article["source_id"],))
    source = dict(src_row) if src_row else {}
    kind = _article_kinds(db, [article.get("job_id") or ""]).get(article.get("job_id") or "", "")
    view = render_reader(paths, article_id, article["markdown"], numbered=(kind == "procedure"))
    report = _loads(_read(paths.article_dir(article_id) / "v9_report.json"), {}) or {}
    others = [dict(r) for r in db.query(
        "SELECT article_id, title, created_at, updated_at FROM articles "
        "WHERE source_id=? AND article_id<>? ORDER BY created_at DESC",
        (article["source_id"], article_id))]
    revisions = db.query_one(
        "SELECT COUNT(*) c FROM article_revisions WHERE article_id=?", (article_id,))["c"]
    video = None
    if source:
        vp = paths.source_video(article["source_id"])
        if vp.is_file():
            video = f"/media/{article['source_id']}/{vp.name}"
    return {
        "article": article,
        "view": view,
        "kind": kind_label(kind),
        "kind_raw": kind,
        "status": status_view(article["status"]),
        "source": source,
        "source_label": source_label(source),
        "video_url": video,
        "report": report,
        "decisions": _decisions(report, article["source_id"], _loads(article.get("outline_json"), []),
                                frame_ok=lambda fid: _frame_exists(db, paths, article["source_id"], fid)),
        "others": others,
        "revision_count": revisions,
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _frame_exists(db, paths, source_id: str, frame_id: str) -> bool:
    if (paths.source_frames(source_id) / f"{frame_id}.jpg").is_file():
        return True
    row = db.query_one("SELECT path FROM frames WHERE source_id=? AND frame_id=?",
                       (source_id, frame_id))
    return bool(row and row["path"] and Path(row["path"]).is_file())


def _decisions(report: dict[str, Any], source_id: str,
               outline: list[dict[str, Any]] | None = None,
               frame_ok=lambda fid: True) -> list[dict[str, Any]]:
    """Per-section judge outcome with the frames it weighed."""
    headings = {s.get("section_id"): s.get("heading") for s in outline or []}
    out = []
    for rec in report.get("selection") or []:
        chosen = set(rec.get("chosen") or [])
        cands = []
        for c in (rec.get("candidates") or [])[:6]:
            fid = c.get("frame_id")
            if fid and frame_ok(fid):
                cands.append({"frame_id": fid, "t": c.get("t"), "chosen": fid in chosen,
                              "src": f"/frames/{source_id}/{fid}.jpg"})
        out.append({
            "section_id": rec.get("section_id", ""),
            "heading": headings.get(rec.get("section_id")) or rec.get("section_id", ""),
            "t": rec.get("t") or [None, None],
            "decision": rec.get("judge_decision") or "",
            "reason": rec.get("judge_reason") or rec.get("rank_reason") or "",
            "chosen": list(chosen),
            "candidates": cands,
            "model": rec.get("vision_model") or "",
        })
    return out


# ----------------------------------------------------------------------
# jobs
def job_view(job: dict[str, Any], *, live: set[str]) -> dict[str, Any]:
    """A job row plus ordered, labelled stage progress.

    `live` is the set of job ids the in-process worker holds (running or
    queued); a queued job outside it was never scheduled by this service.
    """
    cfg = _loads(job.get("config_json"), {}) or {}
    state = job.get("state") or "queued"
    if state == "queued" and job["job_id"] not in live:
        state = "stale"
    stages = {s["stage"]: s for s in job.get("stages") or []}
    pipeline = cfg.get("pipeline") or ("v9" if any(k.startswith("v9_") for k in stages) else
                                       ("legacy" if stages else "v9"))
    steps = []
    if pipeline == "v9":
        for key, label in JOB_STAGES:
            st = stages.get(key)
            if key in ("inspect", "v9_caption") and (st is None or st["state"] != "failed"):
                continue  # internal sub-steps: shown only when they fail
            if st is None and state not in ("queued", "running", "stale"):
                continue  # finished before this stage was recorded: nothing to show
            steps.append(_step(key, label, st, job))
    else:
        for key, st in stages.items():
            steps.append(_step(key, key.replace("_", " ").capitalize(), st, job))
    label, tone = JOB_STATE_LABELS.get(state, (state.title(), "muted"))
    now = time.time()
    started = job.get("started_at")
    ended = job.get("finished_at") or (now if state == "running" else None)
    done = sum(1 for s in steps if s["state"] in ("done", "skipped"))
    return {
        **job,
        "state": state,
        "state_label": label,
        "tone": tone,
        "pipeline": pipeline,
        "steps": steps,
        "progress": round(100 * done / len(steps)) if steps else 0,
        "current_label": _STAGE_LABEL.get(job.get("stage") or "", ""),
        "elapsed_s": (ended - started) if (started and ended) else None,
        "can_retry": state in ("failed", "interrupted", "stale", "cancelled"),
        "can_cancel": state == "queued",
        "origin": cfg.get("origin", ""),
    }


def _step(key: str, label: str, st: dict[str, Any] | None, job: dict[str, Any]) -> dict[str, Any]:
    state = (st or {}).get("state") or "pending"
    metrics = _loads((st or {}).get("metrics_json"), {}) or {}
    return {"key": key, "label": label, "state": state,
            "duration_s": (st or {}).get("duration_s") if state == "done" else None,
            "detail": _stage_detail(key, state, metrics),
            "error": (st or {}).get("error") or ""}


def _stage_detail(key: str, state: str, m: dict[str, Any]) -> str:
    if state == "skipped" and m.get("reused"):
        return "Reused from an earlier run"
    if state != "done":
        return ""
    if key == "v9_write" and m.get("sections"):
        kind = kind_label(m.get("kind"))
        return f"{m['sections']} sections" + (f" · {kind}" if kind else "")
    if key == "v9_discover" and m.get("n_candidates"):
        return f"{m['n_candidates']} candidate frames"
    if key == "v9_select" and "images" in m:
        return (f"{m['images']} images across {m.get('sections_with_image', 0)} sections")
    if key == "v9_publish" and m.get("assets") is not None:
        return f"{m['assets']} images installed"
    return ""


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"
