"""V9 publish: assemble the illustrated article, persist it, export assets."""

from __future__ import annotations

import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger("yt2wiki.v9.publish")


def _target_seconds(target: str) -> float | None:
    """Seconds encoded in an explicit `t=` link target, if any."""
    import re as _re
    m = _re.search(r"[?&]t=(\d+(?:\.\d+)?)", target or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def drop_far_links(body: str, url: str | None, t_start: float, t_end: float,
                   *, tol: float = 60.0) -> str:
    """De-link citations whose explicit target lies far outside the section.

    The writer sometimes cites spans it was never shown (e.g. a verdict
    section pointing at install-troubleshooting timestamps). A false link
    is worse than plain text, so targets more than `tol` seconds outside
    [t_start, t_end] lose their href. In-window links, asset links and
    targets without an explicit `t=` are untouched.
    """
    import re as _re

    def fix(m: _re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        if url and "youtu" in url and "youtu" in target:
            secs = _target_seconds(target)
            if secs is not None and (secs < t_start - tol or secs > t_end + tol):
                return ""  # drop false provenance entirely (no plain label)
        return m.group(0)

    return _re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", fix, body)


def _strip_bare_float_ranges(text: str, t_start: float | None = None,
                             t_end: float | None = None, *, tol: float = 60.0) -> str:
    """Preserve bare floating-point ranges verbatim.

    Earlier heuristics removed ranges like '340.0-349.4' that leaked from the
    writer, but the pattern also matches exact measurements (3.3-5.0 V,
    0.5–1.5 mm, 1.2-1.4 volts) and versions. The safe default is to preserve
    all such ranges; transcript-second noise is handled by the writer's
    timestamp normalisation instead.
    """
    # No stripping — preserve measurements, versions and quantities verbatim.
    # Signature retained for backwards compatibility with callers that pass
    # t_start/t_end.
    _ = (t_start, t_end, tol)
    return text


def _section_source_ref(t_start: float, t_end: float, url: str | None,
                        duration: float | None) -> tuple[str, dict[str, Any]]:
    """One subdued source reference per section, with structured provenance.

    Returns (markdown_line, provenance_dict). The line is a single
    reference covering the section's time window, formatted as m:ss.
    For YouTube it deep-links; for local uploads it is plain metadata.
    """
    from ..article import format_ts, timestamp_url
    start = format_ts(t_start)
    end = format_ts(t_end)
    prov = {"t_start": t_start, "t_end": t_end, "duration": duration,
            "url": url, "valid": True}
    if url and "youtu" in url:
        link = timestamp_url(url, t_start)
        if link:
            return f"*Source {start}–{end} — [{start}]({link})*", prov
    # Local upload or no link: subdued non-clickable metadata
    where = "local video" if not url or "youtu" not in url else "source"
    return f"*Source {start}–{end} · {where}*", prov


def _strip_inline_timestamp_links(body: str) -> str:
    """Remove inline [m:ss](url) citations from body.

    The writer's per-sentence citations are consolidated into the single
    section reference emitted by _section_source_ref, so leaving them inline
    would duplicate the provenance and clutter prose. Non-timestamp links
    (e.g. documentation URLs) are preserved. Code blocks are left untouched.
    """
    import re as _re

    def _collapse_preserve_indent(s: str) -> str:
        lines = s.split("\n")
        out = []
        for line in lines:
            m = _re.match(r"^(\s*)", line)
            leading = m.group(1) if m else ""
            rest = line[len(leading):]
            rest = _re.sub(r" {2,}", " ", rest)
            rest = _re.sub(r" ([.,;:!?])", r"\1", rest)
            out.append(leading + rest)
        return "\n".join(out)

    # Preserve fenced and inline code
    parts = _re.split(r"(```.*?```|`[^`]*`)", body, flags=_re.DOTALL)
    out_parts: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out_parts.append(part)
            continue

        def drop(m: _re.Match[str]) -> str:
            label, target = m.group(1), m.group(2)
            if target.startswith("assets/"):
                return m.group(0)
            if _re.match(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–]\s*\d{1,2}:\d{2}(?::\d{2})?)?$", label.strip()):
                return ""  # consolidated into the section reference
            return m.group(0)

        part = _re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", drop, part)
        # Do NOT strip plain m:ss — that would delete clock times (10:30) and
        # commands (ffmpeg -ss 00:01:30). Far citations are already de-linked to ""
        # by drop_far_links or removed above.
        part = _collapse_preserve_indent(part)
        out_parts.append(part)
    body = "".join(out_parts)
    body = _re.sub(r"\n{2,}", "\n\n", body)
    body = _re.sub(r" +\n", "\n", body)
    return body.strip()


def assemble_markdown(article_title: str, summary: str, sections: list[dict[str, Any]],
                      picks: dict[str, list[dict[str, Any]]],
                      provenance: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Build markdown with section images placed after their paragraph blocks.

    Returns (markdown, asset_rows). Timestamp handling is consolidated:
    inline per-sentence citations are normalized, stripped of bare floats,
    de-duplicated to one subdued source reference per section (with an
    optional expandable detailed view in the reader), and only YouTube
    references are clickable. Local uploads get non-clickable metadata.
    """
    from ..article import format_ts, timestamp_url

    lines = [f"# {article_title}", ""]
    quote = []
    if provenance.get("attribution_line"):
        quote.append(provenance["attribution_line"])
    if provenance.get("url"):
        quote.append(f"Source video: {provenance['url']}")
    # The "link" claim holds only when citations actually link: the writer
    # emits [m:ss](url&t=..) for YouTube sources, plain [m:ss] otherwise
    # (uploads, non-YouTube URLs) — normalize_timestamps only links youtu targets.
    if provenance.get("url") and "youtu" in provenance["url"]:
        quote.append("This illustrated guide was compiled locally from the video's narration "
                     "and original frames. Timestamps link each section to its source span.")
    else:
        quote.append("This illustrated guide was compiled locally from the video's narration "
                     "and original frames. Timestamps mark each claim's source span "
                     "(no source URL to link).")
    lines.append("> " + "\n> ".join(quote))
    lines.append("")
    if summary:
        lines.append(summary.strip())
        lines.append("")
    asset_rows: list[dict[str, Any]] = []
    duration = max([float(s.get("t_end", 0) or 0) for s in sections] or [0]) or None
    for sec in sections:
        lines.append(f"## {sec['heading']}")
        lines.append("")
        body = (sec.get("body") or "").strip()
        if body:
            hint = (float(sec.get("t_start", 0) or 0) + float(sec.get("t_end", 0) or 0)) / 2
            t0 = float(sec.get("t_start", 0) or 0)
            t1 = float(sec.get("t_end", 0) or 0)
            body = normalize_timestamps(body, provenance.get("url"),
                                        duration_s=duration, hint_s=hint)
            body = drop_far_links(body, provenance.get("url"), t0, t1)
            body = _strip_bare_float_ranges(body, t0, t1)
            # Separate data from prose: pull per-sentence citations into one section ref
            body = _strip_inline_timestamp_links(body)
            # Collapse any blank lines that stripping left behind
            import re as _re
            body = _re.sub(r"\n{3,}", "\n\n", body).strip()
            if body:
                lines.append(body)
                lines.append("")
            # One subdued source reference per section, after the prose
            ref_line, _ = _section_source_ref(
                float(sec.get("t_start", 0) or 0),
                float(sec.get("t_end", 0) or 0),
                provenance.get("url"), duration)
            lines.append(ref_line)
            lines.append("")
        for pick in picks.get(sec["section_id"], [])[:2]:
            fid = pick["frame_id"]
            fname = f"{fid}.jpg"
            cap = (pick.get("caption") or "").strip() or "Source frame"
            # Alt is visible description, not a duplicate of the caption line
            alt_raw = (pick.get("alt_text") or cap.split("—")[0] or cap).strip()
            # No timestamps in alt unless time itself is visually relevant
            import re as _re
            alt_raw = _re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–]\s*\d{1,2}:\d{2}(?::\d{2})?)?\]", "", alt_raw)
            alt_raw = alt_raw.strip(" –-")
            alt = alt_raw.replace("]", ")").replace("[", "(")[:180] or cap.replace("]", ")").replace("[", "(")
            if not alt.strip() or alt.lower() == "source frame":
                alt = f"Illustration for {sec['heading']}"[:120]
            lines.append(f"![{alt}](assets/{fname})")
            lines.append("")
            if link := timestamp_url(provenance.get("url"), pick.get("t")):
                lines.append(f"*{cap} — source at [{format_ts(pick.get('t'))}]({link})*")
            else:
                lines.append(f"*{cap}*")
            lines.append("")
            asset_rows.append({"asset_id": f"asset-{fid}", "frame_id": fid,
                               "filename": fname, "caption": cap, "alt_text": alt,
                               "t": pick.get("t"), "source_path": pick.get("path", "")})
    md = normalize_timestamps(
        "\n".join(lines).rstrip() + "\n", provenance.get("url"), duration_s=duration)
    return md, asset_rows


def _citation_candidates(label: str, *, loose_seconds: bool = False) -> list[float]:
    """All plausible second-values for a model-written timestamp label."""
    import re

    out: list[float] = []
    if re.fullmatch(r"\d+(?:\.\d+)?", label):
        return [float(label)]
    mm = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", label)
    if mm:
        a, b, c = int(mm.group(1)), int(mm.group(2)), mm.group(3)
        if c is not None:
            return [a * 3600 + b * 60 + int(c)]
        if b >= 60:
            # Not a valid clock reading: "7:83" means 783s, not 7m83s.
            out.append(a * 100 + b)
            return out
        out.append(a * 60 + b)  # plain mm:ss, preferred
        # A single trailing digit reads as a decimal point ("159:0" = 159.0s).
        # Two-digit seconds additionally allow the transcript-seconds reading
        # inside ranges ("13:00-20:00" in a 524s video = 13-20s).
        if b <= 9 and (len(mm.group(2)) == 1 or loose_seconds):
            out.append(a + b / 10.0)  # "159:0" for 159.0s
    rm = re.fullmatch(r"(\d+):(\d{1,2})-(\d+):(\d{1,2})", label)
    if rm:
        a, b, c, d = (int(rm.group(1)), int(rm.group(2)), int(rm.group(3)), int(rm.group(4)))
        out.append(a * 60 + b)
        out.append(c * 60 + d)
        if b >= 60:
            out.append(a * 100 + b)
        if d >= 60:
            out.append(c * 100 + d)
        if b <= 9:
            out.append(a + b / 10.0)
        if d <= 9:
            out.append(c + d / 10.0)
    return out


def _choose(cands: list[float], hint: float | None, duration: float | None) -> float | None:
    if not cands:
        return None
    pool = [c for c in cands if duration is None or c <= duration + 1] or cands
    if hint is None:
        pick = pool[0]
        return pick if duration is None or pick <= duration + 1 else None
    best = min(pool, key=lambda c: abs(c - hint))
    if duration is not None and best > duration + 1:
        near = [c for c in pool if c <= duration + 1]
        return min(near, key=lambda c: abs(c - hint)) if near else None
    return best


def normalize_timestamps(md: str, url: str | None, duration_s: float | None = None,
                         hint_s: float | None = None) -> str:
    """Fix LLM timestamp citations: [m:ss] form, working links, no bare files.

    Handles linked `[label](target)` and bare `[label]` citations. Ambiguous
    labels (e.g. "7:83", "159:0") are resolved against the section midpoint
    hint; unresolvable or past-EOF citations become plain text, never links.
    """
    import re

    def fmt(total: float) -> str:
        s = max(0, int(round(total)))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    def emit(secs: float, target: str | None) -> str:
        if target is not None and (target.startswith("assets/")
                                   or target.strip().lower() in ("none", "null", "")):
            target = None
        if url and ("youtu" in url):
            sep = "&" if "?" in url else "?"
            return f"[{fmt(secs)}]({url}{sep}t={int(secs)})"
        if target and ("youtu" in target):
            sep = "&" if "?" in target else "?"
            return f"[{fmt(secs)}]({target}{sep}t={int(secs)})"
        return fmt(secs)

    def fix_label(label: str, target: str | None) -> str:
        import re as _re

        rm = _re.fullmatch(r"(.+?)-(.+)", label)
        if rm and ":" in label:
            lo = _choose(_citation_candidates(rm.group(1), loose_seconds=True), hint_s, duration_s)
            hi = _choose(_citation_candidates(rm.group(2), loose_seconds=True), hint_s, duration_s)
            if lo is not None and hi is not None:
                if hi < lo:
                    lo, hi = hi, lo  # descending ranges are authoring slips, not time travel
                if target is not None and url and ("youtu" in url):
                    sep = "&" if "?" in url else "?"
                    return f"[{fmt(lo)}-{fmt(hi)}]({url}{sep}t={int(lo)})"
                return f"[{fmt(lo)}-{fmt(hi)}]"
            return label
        secs = _choose(_citation_candidates(label), hint_s, duration_s)
        if secs is None:
            # Unresolvable: keep the label text, drop any href.
            return label
        return emit(secs, target)

    def fix(m: "re.Match[str]") -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith("assets/"):
            return m.group(0)
        return fix_label(label, target)

    def fix_bare(m: "re.Match[str]") -> str:
        label = m.group(1)
        if ":" not in label and "-" not in label and "." not in label:
            return m.group(0)
        return fix_label(label, None)

    # linked citations first, then bare [..] that are not images/links
    out = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", fix, md)
    out = re.sub(r"(?<!!)(?<![\w\]])\[(\d[\d:.\-]*)\](?!\()", fix_bare, out)
    return out


def persist(settings, db, *, article_id: str, source_id: str, job_id: str,
            title: str, markdown: str, sections: list[dict[str, Any]],
            picks: dict[str, list[dict[str, Any]]],
            provenance: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    from ..article import markdown_to_html, render_html
    from ..db.schema import json_dumps
    from ..article import Article, FrameRef, Section

    secs = []
    for sec in sections:
        frames = []
        for pick in picks.get(sec["section_id"], [])[:2]:
            frames.append(FrameRef(frame_id=pick["frame_id"], path=Path(pick.get("path", "")),
                                   caption=pick.get("caption", ""), t=pick.get("t")))
        secs.append(Section(heading=sec["heading"], level=2,
                           body=[sec.get("body", "")], frames=frames))
    article = Article(source_id=source_id, title=title, summary="",
                      sections=secs, provenance=provenance, content_type="v9_illustrated")

    adir = settings.paths.article_dir(article_id)
    adir.mkdir(parents=True, exist_ok=True)
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    # Validate and install selected assets before committing article outputs;
    # a missing or failed copy must fail visibly, not publish a broken article.
    asset_rows = _asset_rows(settings, assets, picks, article_id)
    # Drop files from previous publishes: the zip below is built from this
    # directory, and anything not in the current picks would otherwise ship
    # as an unreferenced stale asset.
    keep = {f"{p['frame_id']}.jpg" for plist in picks.values() for p in plist[:2]}
    for f in assets.glob("*"):
        if f.is_file() and f.name not in keep:
            try:
                f.unlink()
            except OSError:
                continue
    md_path = settings.paths.article_markdown(article_id)
    md_path.write_text(markdown, encoding="utf-8")
    body_html = markdown_to_html(markdown)
    html = render_html(article, body_html, embed_images=False, asset_dir=assets)
    settings.paths.article_html(article_id).write_text(html, encoding="utf-8")
    standalone = render_html(article, body_html, embed_images=True, asset_dir=assets)
    (settings.paths.article_dir(article_id) / "article.standalone.html").write_text(
        standalone, encoding="utf-8")

    outline = [{"section_id": s["section_id"], "heading": s["heading"],
                "t_start": s["t_start"], "t_end": s["t_end"],
                "frames": [p["frame_id"] for p in picks.get(s["section_id"], [])]}
               for s in sections]
    with db.transaction() as conn:
        prev = conn.execute(
            "SELECT version, markdown, html FROM articles WHERE article_id=?",
            (article_id,)).fetchone()
        if prev is not None:
            # Republishing must never discard what was there (including user
            # edits): keep it as a restorable revision.
            import uuid
            conn.execute(
                "INSERT INTO article_revisions(revision_id, article_id, version, markdown, html, "
                "edit_note, origin, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"rev-{uuid.uuid4().hex[:12]}", article_id, int(prev["version"]),
                 prev["markdown"], prev["html"], "kept before republishing", "generated",
                 time.time()))
        conn.execute(
            """INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version,
                                     status, fingerprint, outline_json, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?, 'published',?,?,?,?)
               ON CONFLICT(article_id) DO UPDATE SET
                 job_id=excluded.job_id, title=excluded.title, markdown=excluded.markdown,
                 html=excluded.html, version=articles.version+1, fingerprint=excluded.fingerprint,
                 outline_json=excluded.outline_json, updated_at=excluded.updated_at""",
            (article_id, source_id, job_id, title, markdown, html, 1,
             settings.fingerprint(), json_dumps(outline), time.time(), time.time()))
        conn.execute("DELETE FROM articles_fts WHERE article_id=?", (article_id,))
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
                     (article_id, title, markdown))
        conn.execute("DELETE FROM article_assets WHERE article_id=?", (article_id,))
        for a in asset_rows:
            conn.execute(
                """INSERT OR REPLACE INTO article_assets(asset_id, article_id, frame_id, path,
                                                          caption, alt_text, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (a["asset_id"], article_id, a["frame_id"], a["path"], a["caption"],
                 a.get("alt_text", ""), time.time()))
    (adir / "v9_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (adir / "sections.json").write_text(json.dumps(sections, indent=1), encoding="utf-8")
    zp = settings.paths.article_dir(article_id) / "article.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(md_path, "article.md")
        for f in sorted(assets.glob("*")):
            if f.is_file():
                zf.write(f, f"assets/{f.name}")
    return {"article_id": article_id, "markdown_chars": len(markdown),
            "assets": len(list(assets.glob('*'))), "zip_bytes": zp.stat().st_size}


def _asset_rows(settings, assets: Path, picks: dict[str, list[dict[str, Any]]],
                article_id: str):
    rows = []
    for plist in picks.values():
        for pick in plist[:2]:
            fid = pick["frame_id"]
            src = Path(pick.get("path", ""))
            fname = f"{fid}.jpg"
            dest = assets / fname
            if not src.is_file():
                raise FileNotFoundError(f"selected frame {fid} missing at {src}")
            try:
                shutil.copy2(src, dest)
            except OSError as exc:
                raise RuntimeError(f"failed to install asset {fname}: {exc}") from exc
            if not dest.is_file():
                raise RuntimeError(f"asset {fname} not installed at {dest}")
            rows.append({"asset_id": f"{article_id}-asset-{fid}",
                         "frame_id": fid, "path": str(dest),
                         "caption": pick.get("caption", ""), "alt_text": pick.get("alt_text", "")})
    return rows
