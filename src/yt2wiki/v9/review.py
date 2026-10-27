"""V9 review page: static acceptance-review HTML for generated articles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any



def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return {} if default is None else default
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {} if default is None else default


def _article_review_html(r, rep: dict, secs: list) -> str:
    parts = [f"<h2>{r['title']} <small>({r['article_id']})</small></h2>",
             f"<p>source: {r['origin_url']} · {r['duration_s']}s · "
             f"<a href='/articles/{r['article_id']}'>reader view</a></p>",
             f"<pre>{json.dumps({k: rep.get(k) for k in ('profile', 'timings', 'vram', 'coverage', 'frame_coverage')}, indent=1, default=str)}</pre>"]
    decisions = {d["section_id"]: d for d in (rep.get("selection") or [])}
    for s in secs:
        d = decisions.get(s["section_id"], {})
        parts.append(f"<div class='sec'><h3>{s['heading']} "
                     f"<small>[{s['t_start']}–{s['t_end']}s]</small></h3>")
        parts.append(f"<p>{(s.get('body') or '')[:600]}</p>")
        parts.append("<table><tr><th>role</th><th>frame</th><th>score/link</th></tr>")
        for c in (d.get("candidates") or [])[:4]:
            mark = "CHOSEN" if c["frame_id"] in (d.get("chosen") or []) else "alt"
            parts.append(f"<tr><td>{mark}</td><td>{c['frame_id']} @ {c['t']}s</td>"
                         f"<td><a href='/frames/{r['source_id']}/{c['frame_id']}.jpg'>view</a></td></tr>")
        parts.append("</table>")
        parts.append(f"<p><i>reason: {(d.get('reason') or '')[:300]}</i></p></div>")
    return "\n".join(parts)


def build_review_page(settings, db, out: Path | None = None) -> str:
    rows = db.query(
        """SELECT a.article_id, a.title, a.source_id, a.markdown, a.updated_at,
                  s.uploader, s.origin_url, s.duration_s
           FROM articles a JOIN sources s ON s.source_id=a.source_id
           WHERE a.article_id LIKE 'art9-%' ORDER BY a.updated_at DESC""")
    parts = ["<html><head><meta charset='utf-8'><title>V9 acceptance review</title>",
             "<style>img{max-width:520px}div.sec{border:1px solid #999;margin:12px;padding:10px}"
             "table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 8px}</style>",
             "</head><body><h1>V9 acceptance review</h1>"]
    for r in rows:
        adir = settings.paths.article_dir(r["article_id"])
        rep = _read_json(adir / "v9_report.json")
        secs = _read_json(adir / "sections.json", default=[])
        parts.append(_article_review_html(r, rep, secs))
    parts.append("</body></html>")
    out = out or Path("v9_review.html")
    # False positive (pythonsecurity:S2083): `out` is an operator-supplied
    # path for a static report dump, never request-derived; default is
    # v9_review.html next to CWD.
    out.write_text("\n".join(parts), encoding="utf-8")  # NOSONAR
    return str(out)
