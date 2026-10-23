"""Immutable per-publication generation records.

Every successful publication (V9 or classic) records one generation: an
immutable identity plus a manifest that pins the exact application build,
the recipe version and prompt hashes, the job-captured provider identities,
the selected frames and the hashes of the published artifacts.

A new generation never erases its parent: the Markdown, HTML and assets of
each generation are snapshotted under ``articles/<id>/generations/<gid>/``
while the existing current-article paths keep serving the reader unchanged.
Human edits stay versioned as article revisions (origin ``user``) and are
never misrepresented as model generations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("yt2wiki.generations")

# V9 recipe version. Bump when the writer prompt, the judge prompt or the
# selection policy changes in a way that alters published output.
RECIPE_VERSION = 1
CLASSIC_RECIPE_VERSION = 1

# Frozen selection policy descriptor for the V9 recipe. Part of the manifest
# so a policy change is detectable even before RECIPE_VERSION is bumped.
SELECTION_POLICY = (
    "v9-z1: batched decorative veto, comparative 0-2x4 rubric scoring, "
    "KEEP/REPLACE/EXPAND/USE_PAIR/OMIT adjudication, directed refinement, "
    "re-verification; judge finals install, rank order never publishes"
)

# Snapshot keys that are safe to persist. Anything else — notably API keys,
# bearer tokens and credential values — is dropped, never stored.
_SAFE_ROLE_KEYS = (
    "provider", "url", "model", "revision", "mode",
    "reachable", "qualified_for_writer", "qualified_for_vision", "qualified_for_asr",
)

UNKNOWN = "unknown"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hashes() -> dict[str, str]:
    """Hashes of the exact V9 prompt templates and selection policy."""
    from .v9 import judge as _judge
    from .v9 import writer as _writer

    return {
        "recipe_version": str(RECIPE_VERSION),
        "writer_title": _sha256_text(_writer.TITLE_PROMPT),
        "writer_section": _sha256_text(_writer.SECTION_PROMPT),
        "judge_rubric": _sha256_text(_judge.RUBRIC_PROMPT),
        "selection_policy": _sha256_text(SELECTION_POLICY),
    }


def _repo_commit(root: Path) -> str:
    """Best-effort git commit for a source checkout (never leaves the repo).

    Returns ``unknown`` outside a checkout (installed package, image without
    source) or when git is unavailable/slow — never a foreign repo's HEAD.
    """
    try:
        if not (root / ".git").is_dir():
            return UNKNOWN
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=10).strip()
        return out or UNKNOWN
    except Exception:  # noqa: BLE001
        return UNKNOWN


def build_info() -> dict[str, str]:
    """Identify the running application build.

    Version comes from the installed package metadata; the commit is
    baked into the image at build time (``/app/BUILD``) with a guarded
    local ``git rev-parse`` fallback; the OCI image digest is only known when
    the runtime environment declares it (``YT2WIKI_IMAGE_DIGEST``).
    Anything unavailable reads ``unknown`` — never fabricated.
    """
    version = UNKNOWN
    try:
        from importlib.metadata import version as _pkg_version
        version = _pkg_version("yt2wiki")
    except Exception:  # noqa: BLE001
        try:
            from . import __version__ as _v
            version = _v
        except Exception:  # noqa: BLE001
            pass
    commit = UNKNOWN
    for candidate in (Path("/app/BUILD"), Path(__file__).resolve().parents[2] / "BUILD"):
        try:
            if candidate.is_file():
                commit = candidate.read_text(encoding="utf-8").strip().split()[0]
                break
        except Exception:  # noqa: BLE001
            continue
    if commit == UNKNOWN:
        commit = _repo_commit(Path(__file__).resolve().parents[2])
    digest = os.environ.get("YT2WIKI_IMAGE_DIGEST", "").strip() or UNKNOWN
    return {"app_version": version, "git_commit": commit, "image_digest": digest}


def sanitize_snapshot(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Keep only non-secret provider identity fields per role.

    Drops ``api_key_env`` values, tokens and any unexpected keys: the
    manifest must identify *which* provider served a role, never carry
    credentials or bearer tokens.
    """
    clean: dict[str, dict[str, Any]] = {}
    for role in ("asr", "writer", "vision"):
        entry = (snapshot or {}).get(role)
        if not isinstance(entry, dict):
            continue
        kept = {k: entry[k] for k in _SAFE_ROLE_KEYS if k in entry}
        if kept.get("provider") or kept.get("url") or kept.get("model"):
            kept.setdefault("revision", UNKNOWN)
            clean[role] = kept
    return clean


def _job_snapshot(db, job_id: str | None) -> dict[str, Any] | None:
    """The inference snapshot captured on the job row at submission time."""
    if not job_id:
        return None
    try:
        row = db.query_one("SELECT config_json FROM jobs WHERE job_id=?", (job_id,))
        cfg = json.loads(row["config_json"] or "{}") if row else {}
        snap = cfg.get("inference")
        return snap if isinstance(snap, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _recorded_asr_identity(db, source_id: str) -> dict[str, Any] | None:
    """The ASR server identity behind the stored transcript, if recorded."""
    try:
        rows = db.query(
            """SELECT s.metrics_json FROM stages s JOIN jobs j ON j.job_id = s.job_id
               WHERE j.source_id = ? AND s.stage = 'transcribe' AND s.state = 'done'
               ORDER BY s.finished_at DESC LIMIT 5""",
            (source_id,))
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except ValueError:
            continue
        if metrics.get("mode") == "asr" and isinstance(metrics.get("asr_identity"), dict):
            ident = dict(metrics["asr_identity"])
            ident.setdefault("revision", UNKNOWN)
            return ident
        if metrics.get("mode") == "captions":
            return {"mode": "captions"}
    return None


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _resolve_providers(db, source_id: str, job_id: str | None,
                       snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Job-captured provider identities, allowlisted for storage."""
    providers = sanitize_snapshot(snapshot if snapshot is not None
                                  else _job_snapshot(db, job_id))
    asr_recorded = _recorded_asr_identity(db, source_id)
    if asr_recorded and providers.get("asr", {}).get("url") in (
            None, "", asr_recorded.get("url")):
        # The stage-metrics identity takes the same allowlist as the snapshot:
        # a future stage must never smuggle credential-like keys into storage.
        providers["asr"] = {k: v for k, v in asr_recorded.items()
                            if k in _SAFE_ROLE_KEYS}
        providers["asr"].setdefault("revision", UNKNOWN)
    return providers


def _resolve_source_hash(db, source_id: str,
                         source_content_hash: str | None) -> str:
    if source_content_hash is not None:
        return source_content_hash
    try:
        srow = db.query_one(
            "SELECT content_hash FROM sources WHERE source_id=?", (source_id,))
        return (srow["content_hash"] if srow else None) or UNKNOWN
    except Exception:  # noqa: BLE001
        return UNKNOWN


def _plan_generation(db, *, article_id: str) -> tuple[str | None, str]:
    """Parent generation and operation for the next publication."""
    prev = db.query_one(
        "SELECT generation_id FROM generations WHERE article_id=? "
        "ORDER BY created_at DESC LIMIT 1", (article_id,))
    parent = prev["generation_id"] if prev else None
    return parent, ("initial" if parent is None else "regenerate")


def _build_manifest(*, generation_id: str, article_id: str, source_id: str,
                    job_id: str | None, parent: str | None, operation: str,
                    recipe: str, recipe_version: int,
                    settings_fingerprint: str | None, profile: str | None,
                    providers: dict[str, dict[str, Any]],
                    source_content_hash: str, stored_frames: list[str],
                    artifacts: dict[str, str], selection: dict[str, Any] | None,
                    created_at: float) -> dict[str, Any]:
    build = build_info()
    prompts: dict[str, Any] = {"recipe_version": str(recipe_version)}
    if recipe == "v9":
        try:
            prompts.update(prompt_hashes())
        except Exception:  # noqa: BLE001
            prompts["error"] = "prompt hash unavailable"
    return {
        "generation_id": generation_id,
        "article_id": article_id,
        "source_id": source_id,
        "job_id": job_id,
        "parent_generation_id": parent,
        "operation": operation,
        "recipe": recipe,
        "recipe_version": recipe_version,
        "app": build,
        "settings_fingerprint": settings_fingerprint,
        "profile": profile,
        "providers": providers,
        "prompts": prompts,
        "selection": selection or ({"policy": SELECTION_POLICY} if recipe == "v9" else {}),
        "source_content_hash": source_content_hash,
        "frame_ids": stored_frames,
        "artifacts": artifacts,
        "created_at": created_at,
    }


def snapshot_generation_files(paths, article_id: str,
                              generation_id: str) -> dict[str, str]:
    """Copy the current article outputs to the immutable generation directory.

    No database writes. On failure the partial directory is removed and the
    exception propagates — the publisher must treat this as a failed
    publication, not a published article without provenance.
    """
    adir = paths.article_dir(article_id)
    gdir = adir / "generations" / generation_id
    assets_src = paths.article_assets(article_id)
    artifacts: dict[str, str] = {}
    try:
        gdir.mkdir(parents=True, exist_ok=False)
        for name in ("article.md", "article.html", "article.standalone.html"):
            src = adir / name
            if src.is_file():
                shutil.copy2(src, gdir / name)
                artifacts[name] = _file_hash(src)
        if assets_src.is_dir():
            gassets = gdir / "assets"
            gassets.mkdir(exist_ok=True)
            for f in sorted(assets_src.glob("*")):
                if f.is_file():
                    shutil.copy2(f, gassets / f.name)
                    artifacts[f"assets/{f.name}"] = _file_hash(f)
    except Exception:
        log.exception("generation snapshot failed for %s", article_id)
        shutil.rmtree(gdir, ignore_errors=True)
        raise
    return artifacts


def write_manifest(paths, article_id: str, manifest: dict[str, Any]) -> str:
    """Persist the manifest JSON beside the snapshotted files."""
    manifest_rel = f"generations/{manifest['generation_id']}/manifest.json"
    (paths.article_dir(article_id) / manifest_rel).write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest_rel


def insert_generation_row(conn, manifest: dict[str, Any],
                          settings_fingerprint: str | None,
                          profile: str | None) -> None:
    """Insert the generation row on the caller's connection/transaction.

    Lets publishers commit the article and its generation atomically: either
    both land or neither does. Never commits by itself.
    """
    from .db.schema import json_dumps

    providers = manifest["providers"]
    prompts = manifest["prompts"]
    conn.execute(
        """INSERT INTO generations(generation_id, article_id, source_id, job_id,
                                   parent_generation_id, operation, recipe, recipe_version,
                                   app_version, git_commit, image_digest,
                                   settings_fingerprint, profile,
                                   asr_json, writer_json, vision_json,
                                   prompts_json, selection_json,
                                   source_content_hash, frame_ids_json, artifacts_json,
                                   manifest_path, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (manifest["generation_id"], manifest["article_id"], manifest["source_id"],
         manifest["job_id"], manifest["parent_generation_id"], manifest["operation"],
         manifest["recipe"], manifest["recipe_version"],
         manifest["app"]["app_version"], manifest["app"]["git_commit"],
         manifest["app"]["image_digest"], settings_fingerprint, profile,
         json_dumps(providers.get("asr", {})),
         json_dumps(providers.get("writer", {})),
         json_dumps(providers.get("vision", {})),
         json_dumps(prompts),
         json_dumps(manifest["selection"]),
         manifest["source_content_hash"], json_dumps(manifest["frame_ids"]),
         json_dumps(manifest["artifacts"]),
         f"generations/{manifest['generation_id']}/manifest.json",
         manifest["created_at"]),
    )
    conn.execute("UPDATE articles SET current_generation_id=? WHERE article_id=?",
                 (manifest["generation_id"], manifest["article_id"]))


def prepare_generation(db, paths, *, article_id: str, source_id: str,
                       job_id: str | None, recipe: str, recipe_version: int,
                       settings_fingerprint: str | None = None,
                       profile: str | None = None,
                       snapshot: dict[str, Any] | None = None,
                       source_content_hash: str | None = None,
                       frame_ids: list[str] | None = None,
                       selection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan + snapshot a generation without touching the database.

    Returns the manifest dict and its directory. The caller inserts the row
    with :func:`insert_generation_row` inside its own article transaction and
    then calls :func:`write_manifest`. Any failure before the transaction
    commits leaves no visible trace (the snapshot directory is removed).
    """
    providers = _resolve_providers(db, source_id, job_id, snapshot)
    parent, operation = _plan_generation(db, article_id=article_id)
    gid = f"gen-{uuid.uuid4().hex[:12]}"
    now = time.time()
    artifacts = snapshot_generation_files(paths, article_id, gid)
    manifest = _build_manifest(
        generation_id=gid, article_id=article_id, source_id=source_id,
        job_id=job_id, parent=parent, operation=operation, recipe=recipe,
        recipe_version=recipe_version, settings_fingerprint=settings_fingerprint,
        profile=profile, providers=providers,
        source_content_hash=_resolve_source_hash(db, source_id, source_content_hash),
        stored_frames=sorted(frame_ids or []), artifacts=artifacts,
        selection=selection, created_at=now)
    return {"manifest": manifest,
            "directory": str(paths.article_dir(article_id) / "generations" / gid)}


def record_generation(db, paths, *, article_id: str, source_id: str,
                      job_id: str | None, recipe: str,
                      recipe_version: int,
                      settings_fingerprint: str | None = None,
                      profile: str | None = None,
                      snapshot: dict[str, Any] | None = None,
                      source_content_hash: str | None = None,
                      frame_ids: list[str] | None = None,
                      selection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Snapshot the current article outputs as one immutable generation.

    Composed from :func:`prepare_generation` (files) plus a dedicated
    transaction for the row: either both the files and the row land or the
    snapshot directory is removed and nothing is recorded. Publishers that
    need article + generation atomicity use ``prepare_generation`` /
    ``insert_generation_row`` / ``write_manifest`` directly inside their own
    article transaction.
    """
    prep = prepare_generation(
        db, paths, article_id=article_id, source_id=source_id, job_id=job_id,
        recipe=recipe, recipe_version=recipe_version,
        settings_fingerprint=settings_fingerprint, profile=profile,
        snapshot=snapshot, source_content_hash=source_content_hash,
        frame_ids=frame_ids, selection=selection)
    manifest = prep["manifest"]
    try:
        with db.transaction() as conn:
            insert_generation_row(conn, manifest, settings_fingerprint, profile)
    except Exception:
        shutil.rmtree(prep["directory"], ignore_errors=True)
        raise
    manifest_rel = write_manifest(paths, article_id, manifest)
    return {"generation_id": manifest["generation_id"],
            "parent_generation_id": manifest["parent_generation_id"],
            "operation": manifest["operation"], "manifest": manifest_rel,
            "artifacts": len(manifest["artifacts"])}


def restore_generation(db, paths, generation_id: str) -> dict[str, Any]:
    """Copy a generation snapshot back over the current article outputs.

    Returns the restored content for the caller to record as a user
    revision (restoring is an edit, not a new model generation). Raises
    KeyError for an unknown generation id and FileNotFoundError when the
    snapshot files are missing.
    """
    row = db.query_one("SELECT * FROM generations WHERE generation_id=?",
                       (generation_id,))
    if row is None:
        raise KeyError(f"unknown generation: {generation_id}")
    article_id = row["article_id"]
    adir = paths.article_dir(article_id)
    gdir = adir / "generations" / generation_id
    manifest_file = gdir / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError(f"generation snapshot missing: {manifest_file}")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"generation manifest unreadable: {exc}") from exc
    try:
        gen_artifacts = manifest.get("artifacts") or {}
        for rel, digest in gen_artifacts.items():
            src = gdir / rel
            if not src.is_file():
                raise FileNotFoundError(f"generation file missing: {rel}")
            if _file_hash(src) != digest:
                raise ValueError(f"generation file corrupted: {rel}")
    except (ValueError, FileNotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"generation manifest unreadable: {exc}") from exc
    for rel in gen_artifacts:
        dest = adir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gdir / rel, dest)
    md = (adir / "article.md").read_text(encoding="utf-8") \
        if (adir / "article.md").is_file() else ""
    return {"article_id": article_id, "generation_id": generation_id,
            "markdown": md, "artifacts": sorted(gen_artifacts)}
