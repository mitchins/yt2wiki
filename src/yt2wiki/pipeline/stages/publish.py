"""Stage 10 — publication.

Writes the Markdown article, a fully self-contained HTML file (images embedded,
zero network requests), and a portable export bundle.  Registers everything for
search and provenance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from ...article import render_html
from .. import StageContext

log = logging.getLogger("yt2wiki.stage.publish")

_STANDALONE_HTML_NAME = "article.standalone.html"


def run(ctx: StageContext) -> dict[str, Any]:
    article_id = ctx.artifacts.get("article_id")
    if not article_id:
        raise RuntimeError("no article id from compose stage")
    row = ctx.db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
    if row is None:
        raise RuntimeError(f"article {article_id} missing")

    md_path = ctx.paths.article_markdown(article_id)
    assets = ctx.paths.article_assets(article_id)
    out_dir = ctx.paths.article_dir(article_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = md_path.read_text(encoding="utf-8")

    from ...article import Article, markdown_to_html

    article = Article(
        source_id=ctx.source_id,
        title=row["title"],
        summary="",
        provenance=ctx.meta().get("provenance") or {},
    )
    article.provenance.setdefault("profile", ctx.settings.profile)
    article.provenance.setdefault("fingerprint", ctx.settings.fingerprint())
    article.provenance.setdefault("url", row_source_url(ctx))
    article.provenance.setdefault("title", row["title"])

    body_html = markdown_to_html(md)
    standalone = render_html(article, body_html, embed_images=True, asset_dir=assets)
    (out_dir / _STANDALONE_HTML_NAME).write_text(standalone, encoding="utf-8")

    bundle = ctx.paths.export_bundle(article_id)
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "article.md").write_text(md, encoding="utf-8")
    (bundle / "article.html").write_text(standalone, encoding="utf-8")
    if assets.is_dir():
        shutil.copytree(assets, bundle / "assets", dirs_exist_ok=True)

    verification = ctx.artifacts.get("verification", {})
    (bundle / "verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (bundle / "provenance.json").write_text(
        json.dumps(_provenance_payload(ctx), ensure_ascii=False, indent=1), encoding="utf-8"
    )

    zip_path = out_dir / "article.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(bundle)))

    with ctx.db.transaction() as conn:
        # Keep the lightweight HTML (relative asset paths) in the database and
        # the fully self-contained version on disk: a library of hundreds of
        # articles should not carry megabytes of base64 inside SQLite.
        conn.execute(
            "UPDATE articles SET status='published', updated_at=? WHERE article_id=?",
            (time.time(), article_id),
        )
        conn.execute(
            "DELETE FROM articles_fts WHERE article_id=?", (article_id,)
        )
        conn.execute(
            "INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
            (article_id, row["title"], md),
        )
        conn.execute(
            "UPDATE jobs SET article_id=?, updated_at=? WHERE job_id=?",
            (article_id, time.time(), ctx.job_id),
        )
    ctx.artifacts["published"] = True
    ctx.artifacts["standalone_html"] = str(out_dir / _STANDALONE_HTML_NAME)
    ctx.artifacts["export_bundle"] = str(bundle)
    ctx.artifacts["export_zip"] = str(zip_path)
    return {
        "article_id": article_id,
        "markdown_bytes": len(md.encode()),
        "standalone_html_bytes": (out_dir / _STANDALONE_HTML_NAME).stat().st_size,
        "db_html_bytes": len(row["html"] or ""),
        "zip_bytes": zip_path.stat().st_size,
        "assets": len(list(assets.glob('*.jpg'))) if assets.is_dir() else 0,
        "verification": verification.get("verdict", "unknown"),
        "selfcontained": standalone.count("data:image/jpeg;base64,") > 0 or "images" not in md,
    }


def row_source_url(ctx: StageContext) -> str:
    row = ctx.db.query_one("SELECT origin_url FROM sources WHERE source_id=?", (ctx.source_id,))
    return (row["origin_url"] if row else None) or "local file"


def _provenance_payload(ctx: StageContext) -> dict[str, Any]:
    meta = ctx.meta()
    loads = ctx.db.query(
        "SELECT * FROM model_loads WHERE job_id=? ORDER BY load_id", (ctx.job_id,)
    )
    return {
        "source_id": ctx.source_id,
        "job_id": ctx.job_id,
        "profile": ctx.settings.profile,
        "fingerprint": ctx.settings.fingerprint(),
        "models": {role: spec.repo + "@" + spec.revision
                   for role, spec in ((r, ctx.settings.model(r))
                                      for r in ("asr", "vision", "text", "verify"))},
        "model_loads": [dict(r) for r in loads],
        "provenance": meta.get("provenance", {}),
        "transcript_mode": ctx.artifacts.get("transcript_source", {}),
        "verification": ctx.artifacts.get("verification", {}),
        "generated_at": time.time(),
    }
