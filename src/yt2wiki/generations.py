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

Failure handling is deliberately bounded, not a general transaction
framework: publishers stage outputs aside, snapshot and commit, then promote
staged files over canonical paths with per-file backups. An injected failure
anywhere before completion restores the previous files and rows. Crash
consistency across the filesystem/SQLite domains is NOT claimed: an unclean
shutdown mid-promotion can leave staged/new files ahead of the committed
generation — re-running publication heals it by regenerating from source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("yt2wiki.generations")

# V9 recipe version. Bump when the writer prompt, the judge prompt or the
# selection policy changes in a way that alters published output.
RECIPE_VERSION = 2
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

ARTICLE_MD_NAME = "article.md"


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
    commit = _read_build_commit(
        [Path("/app/BUILD"), Path(__file__).resolve().parents[2] / "BUILD"])
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


def _read_build_commit(candidates: list[Path]) -> str:
    """First non-empty BUILD file wins; anything unreadable reads as unknown."""
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip().split()[0]
        except Exception:  # noqa: BLE001
            continue
    return UNKNOWN


def snapshot_generation_files(paths, article_id: str,
                              generation_id: str,
                              source_dir: Path | None = None) -> dict[str, str]:
    """Copy article outputs to the immutable generation directory.

    Reads from ``source_dir`` (a publisher staging directory) or, when
    omitted, from the canonical article directory. No database writes. On
    failure the partial directory is removed and the exception propagates —
    the publisher must treat this as a failed publication, not a published
    article without provenance.
    """
    adir = paths.article_dir(article_id)
    src_dir = source_dir or adir
    src_assets = src_dir / "assets"
    gdir = adir / "generations" / generation_id
    artifacts: dict[str, str] = {}
    try:
        gdir.mkdir(parents=True, exist_ok=False)
        for name in (ARTICLE_MD_NAME, "article.html", "article.standalone.html"):
            src = src_dir / name
            if src.is_file():
                shutil.copy2(src, gdir / name)
                artifacts[name] = _file_hash(src)
        if src_assets.is_dir():
            gassets = gdir / "assets"
            gassets.mkdir(exist_ok=True)
            for f in sorted(src_assets.glob("*")):
                if f.is_file():
                    shutil.copy2(f, gassets / f.name)
                    artifacts[f"assets/{f.name}"] = _file_hash(f)
    except Exception:
        log.exception("generation snapshot failed for %s", article_id)
        shutil.rmtree(gdir, ignore_errors=True)
        raise
    return artifacts


@contextmanager
def promoted_stage(article_dir: Path, stage_dir: Path, gid: str, *,
                   prune_assets: bool = False) -> Iterator[None]:
    """Publish staged outputs over canonical paths, with bounded rollback.

    Moves every staged file over its canonical counterpart (per-file backups
    kept aside); with ``prune_assets`` also removes canonical assets absent
    from the stage. Yields once promotion completes. If the caller's block
    then fails (e.g. the DB transaction aborts), canonical files are restored
    from backup and the promoted files removed, and the exception propagates.

    This covers the injected-failure domain only. It is not a general
    transaction framework and claims no crash atomicity: an unclean shutdown
    mid-promotion can leave new files ahead of the committed generation, in
    which case re-running publication heals it.
    """
    backup_dir = article_dir / f".backup-{gid}"
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True)
    staged = sorted(p for p in stage_dir.rglob("*") if p.is_file())
    staged_rels = {p.relative_to(stage_dir) for p in staged}
    completed: list[tuple[Path | None, Path, bool]] = []  # (backup, canonical, promoted?)
    try:
        _promote_staged(staged, stage_dir, article_dir, backup_dir, completed)
        if prune_assets:
            _prune_canonical_assets(article_dir, backup_dir, staged_rels, completed)
        yield
    except Exception:
        _rollback_promotion(completed, stage_dir, backup_dir)
        raise
    else:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)




def _promote_staged(staged: list[Path], stage_dir: Path, article_dir: Path,
                    backup_dir: Path,
                    completed: list[tuple[Path | None, Path, bool]]) -> None:
    """Move staged files over canonical paths, backing up what they replace."""
    for src in staged:
        rel = src.relative_to(stage_dir)
        dest = article_dir / rel
        backup: Path | None = None
        if dest.is_file() or dest.is_symlink() or dest.is_dir():
            backup = backup_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
        # Record intent before the move: if the move itself fails, the
        # rollback below still restores the moved-aside original.
        completed.append((backup, dest, True))
        shutil.move(str(src), str(dest))


def _prune_canonical_assets(article_dir: Path, backup_dir: Path,
                            staged_rels: set[Path],
                            completed: list[tuple[Path | None, Path, bool]]) -> None:
    """Move canonical assets absent from the stage into the backup dir."""
    canonical_assets = article_dir / "assets"
    staged_assets = {r for r in staged_rels if r.parts[:1] == ("assets",)}
    if not canonical_assets.is_dir():
        return
    for existing in sorted(canonical_assets.glob("*")):
        rel = existing.relative_to(article_dir)
        if rel not in staged_assets and existing.is_file():
            backup = backup_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(existing), str(backup))
            # Prune moves are single-op: only record success, so a
            # failed prune never discards an untouched original.
            completed.append((backup, existing, False))


def _rollback_promotion(completed: list[tuple[Path | None, Path, bool]],
                        stage_dir: Path, backup_dir: Path) -> None:
    """Undo promotion in reverse order; log loudly if restoration is partial."""
    restore_failed = False
    for backup, dest, promoted in reversed(completed):
        try:
            # Remove only what promotion put there; never touch a file
            # promotion did not create.
            if promoted and (dest.is_file() or dest.is_symlink()):
                dest.unlink()
            if backup is not None and backup.exists() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(dest))
        except Exception:  # noqa: BLE001
            restore_failed = True
            log.exception("promotion rollback failed for %s", dest)
    shutil.rmtree(stage_dir, ignore_errors=True)
    if restore_failed:
        # Keep the leftovers for manual inspection and say so loudly;
        # re-running publication heals the canonical state.
        log.error("promotion rollback incomplete; leftovers kept under %s",
                  backup_dir)
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)

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
                       selection: dict[str, Any] | None = None,
                       source_dir: Path | None = None) -> dict[str, Any]:
    """Plan + snapshot a generation without touching the database.

    Snapshots from ``source_dir`` (a publisher staging directory) or the
    canonical article directory, writes the manifest beside the snapshot,
    and returns the manifest dict plus paths. The caller inserts the row
    with :func:`insert_generation_row` inside its own article transaction.
    Any failure here leaves no visible trace (the snapshot directory,
    manifest included, is removed).
    """
    providers = _resolve_providers(db, source_id, job_id, snapshot)
    parent, operation = _plan_generation(db, article_id=article_id)
    gid = f"gen-{uuid.uuid4().hex[:12]}"
    now = time.time()
    gdir = paths.article_dir(article_id) / "generations" / gid
    artifacts = snapshot_generation_files(paths, article_id, gid,
                                          source_dir=source_dir)
    manifest = _build_manifest(
        generation_id=gid, article_id=article_id, source_id=source_id,
        job_id=job_id, parent=parent, operation=operation, recipe=recipe,
        recipe_version=recipe_version, settings_fingerprint=settings_fingerprint,
        profile=profile, providers=providers,
        source_content_hash=_resolve_source_hash(db, source_id, source_content_hash),
        stored_frames=sorted(frame_ids or []), artifacts=artifacts,
        selection=selection, created_at=now)
    try:
        manifest_rel = write_manifest(paths, article_id, manifest)
    except Exception:
        shutil.rmtree(gdir, ignore_errors=True)
        raise
    return {"manifest": manifest, "manifest_rel": manifest_rel,
            "directory": str(gdir)}


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

    Composed from :func:`prepare_generation` (snapshot + manifest files)
    plus a dedicated transaction for the row: either the files, manifest and
    row all land or the snapshot directory is removed and nothing is
    recorded. Publishers that need article + generation consistency use
    ``prepare_generation`` / ``insert_generation_row`` inside their own
    article transaction together with :func:`promoted_stage`.
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
    return {"generation_id": manifest["generation_id"],
            "parent_generation_id": manifest["parent_generation_id"],
            "operation": manifest["operation"], "manifest": prep["manifest_rel"],
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
    md = (adir / ARTICLE_MD_NAME).read_text(encoding="utf-8") \
        if (adir / ARTICLE_MD_NAME).is_file() else ""
    return {"article_id": article_id, "generation_id": generation_id,
            "markdown": md, "artifacts": sorted(gen_artifacts)}
