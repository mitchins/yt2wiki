"""Overlay persistence: additive v5 tables, history never overwritten.

Follows the existing migration scheme (additive deltas, idempotent,
newer-than-supported refusal stays in `schema.py`). A recheck inserts a
new overlay row; `current_overlay` presents one only when its bound
revision still matches the canonical article markdown.
"""

from __future__ import annotations

import json
import time

from .contracts import ClaimRecord, EvidenceRecord, OverlayRecord, revision_sha256

MIGRATION_V5 = """
CREATE TABLE IF NOT EXISTS factcheck_overlays (
    overlay_id               TEXT PRIMARY KEY,
    article_id               TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    article_revision_sha256  TEXT NOT NULL,
    article_type             TEXT,
    schema_version           INTEGER NOT NULL DEFAULT 1,
    checked_at               TEXT NOT NULL,
    verifier_json            TEXT NOT NULL DEFAULT '{}',
    run_status               TEXT NOT NULL DEFAULT 'failed',
    coverage_json            TEXT NOT NULL DEFAULT '{}',
    claims_json              TEXT NOT NULL DEFAULT '[]',
    created_at               REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS factcheck_overlays_article
    ON factcheck_overlays(article_id, created_at);
"""


def _claim_to_dict(claim: ClaimRecord) -> dict:
    return {
        "claim_id": claim.claim_id,
        "article_anchor": {"section_id": claim.section_id,
                           "quote": claim.quote, "offset": claim.offset},
        "video_provenance": claim.video_provenance,
        "attribution": claim.attribution,
        "claim_kind": claim.claim_kind,
        "assessment": claim.assessment,
        "reason": claim.reason,
        "evidence": [{
            "url": e.url, "title": e.title, "publisher": e.publisher,
            "published_at": e.published_at, "retrieved_at": e.retrieved_at,
            "locator": e.locator, "excerpt": e.excerpt,
            "relationship": e.relationship,
            "independence_group": e.independence_group,
            "content_hash": e.content_hash,
        } for e in claim.evidence],
    }


def _claim_from_dict(d: dict) -> ClaimRecord:
    return ClaimRecord(
        claim_id=d["claim_id"],
        section_id=d["article_anchor"]["section_id"],
        quote=d["article_anchor"]["quote"],
        offset=d["article_anchor"].get("offset", 0),
        video_provenance=d.get("video_provenance", []),
        attribution=d.get("attribution", "article_narration"),
        claim_kind=d.get("claim_kind", "empirical"),
        assessment=d.get("assessment", "not_checked"),
        reason=d.get("reason", ""),
        evidence=[EvidenceRecord(
            url=e.get("url", ""), title=e.get("title", ""),
            publisher=e.get("publisher", ""),
            published_at=e.get("published_at"),
            retrieved_at=e.get("retrieved_at"),
            locator=e.get("locator", ""), excerpt=e.get("excerpt", ""),
            relationship=e.get("relationship", "context_only"),
            independence_group=e.get("independence_group", ""),
            content_hash=e.get("content_hash", "")) for e in d.get("evidence", [])],
    )


def persist_overlay(db, overlay: OverlayRecord) -> None:
    """Insert a new overlay revision (never update an existing row)."""
    import json as _json
    db.execute(
        """INSERT INTO factcheck_overlays(overlay_id, article_id,
               article_revision_sha256, article_type, schema_version,
               checked_at, verifier_json, run_status, coverage_json,
               claims_json, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (overlay.overlay_id, overlay.article_id,
         overlay.article_revision_sha256, overlay.article_type,
         overlay.schema_version, overlay.checked_at,
         _json.dumps(overlay.verifier, sort_keys=True), overlay.run_status,
         _json.dumps(overlay.coverage, sort_keys=True),
         _json.dumps([_claim_to_dict(c) for c in overlay.claims], sort_keys=True),
         time.time()),
    )


def _row_to_overlay(row) -> OverlayRecord:
    return OverlayRecord(
        schema_version=row["schema_version"],
        overlay_id=row["overlay_id"],
        article_id=row["article_id"],
        article_revision_sha256=row["article_revision_sha256"],
        article_type=row["article_type"],
        checked_at=row["checked_at"],
        verifier=json.loads(row["verifier_json"] or "{}"),
        run_status=row["run_status"],
        coverage={
            "eligible_claims": 0, "checked_claims": 0,
            "skipped_claims": [], "by_assessment": {},
            **json.loads(row["coverage_json"] or "{}"),
        },
        claims=[_claim_from_dict(c)
                for c in json.loads(row["claims_json"] or "[]")],
    )


def _canonical_revision(db, article_id: str) -> str | None:
    row = db.query_one("SELECT markdown FROM articles WHERE article_id=?",
                       (article_id,))
    if row is None:
        return None
    return revision_sha256(row["markdown"])


def ship_sidecar_in_zip(paths, article_id: str,
                        overlay: OverlayRecord | None) -> None:
    """Add or remove the `factcheck.md` zip entry, atomically.

    With a current overlay: regenerate the sidecar from that overlay and
    ship it. Without one: strip any stale `factcheck.md` (file and entry)
    so a stale sidecar can never ship. Other members are copied
    byte-identically; the canonical `article.md` is never modified. The
    archive is rebuilt to a temp file and atomically replaced.
    """
    import os as _os
    import uuid as _uuid
    import zipfile as _zipfile

    from .panel import render_factcheck_md
    adir = paths.article_dir(article_id)
    sidecar = adir / "factcheck.md"
    zp = adir / "article.zip"
    if overlay is not None:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(render_factcheck_md(overlay), encoding="utf-8")
    else:
        try:
            sidecar.unlink()
        except OSError:
            pass
    if not zp.is_file():
        return
    with _zipfile.ZipFile(zp, "r") as zin:
        members = [(info, zin.read(info.filename))
                   for info in zin.infolist()
                   if info.filename != "factcheck.md"]
    tmp = adir / f"article.zip.tmp.{_uuid.uuid4().hex[:8]}"
    with _zipfile.ZipFile(tmp, "w", _zipfile.ZIP_DEFLATED) as zout:
        for info, data in members:
            zout.writestr(info, data)
        if overlay is not None:
            zout.write(sidecar, "factcheck.md")
    _os.replace(tmp, zp)


def refresh_export_sidecar(db, paths, article_id: str,
                           overlay: OverlayRecord):
    """Ship the sidecar after an explicit run — only for the live revision.

    Re-reads the canonical article and refuses when it changed during the
    run: the overlay is then historical (retained, never presented), and
    no sidecar ships for a revision it does not describe. Returns the
    sidecar path, or None on refusal.
    """
    live = _canonical_revision(db, article_id)
    if live is None:
        raise KeyError(f"no published article {article_id}")
    if live != overlay.article_revision_sha256:
        return None
    ship_sidecar_in_zip(paths, article_id, overlay)
    return paths.article_dir(article_id) / "factcheck.md"


def overlay_history(db, article_id: str) -> list[OverlayRecord]:
    rows = db.query(
        "SELECT * FROM factcheck_overlays WHERE article_id=? "
        "ORDER BY created_at ASC", (article_id,))
    return [_row_to_overlay(r) for r in rows]


def current_overlay(db, article_id: str) -> OverlayRecord | None:
    """The overlay for the live revision, or None when stale/absent.

    A stored overlay whose bound revision no longer matches the canonical
    article markdown is historical only — never presented as current.
    """
    article = db.query_one(
        "SELECT markdown FROM articles WHERE article_id=?", (article_id,))
    if article is None:
        return None
    live = revision_sha256(article["markdown"])
    rows = db.query(
        "SELECT * FROM factcheck_overlays WHERE article_id=? "
        "ORDER BY created_at DESC", (article_id,))
    for row in rows:
        overlay = _row_to_overlay(row)
        if overlay.article_revision_sha256 == live:
            return overlay
    return None
