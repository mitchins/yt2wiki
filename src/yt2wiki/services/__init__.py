"""Service layer: creating sources, jobs, and running the pipeline."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ..acquisition import AcquisitionError, probe_url
from ..config import Settings
from ..db.schema import Database, json_dumps, json_loads
from ..paths import file_hash, source_id_for_file, source_id_for_youtube

log = logging.getLogger("yt2wiki.service")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.paths = settings.paths

    # ------------------------------------------------------------------
    # sources
    def create_youtube_source(self, url: str) -> str:
        sid = source_id_for_youtube(url)
        try:  # noqa: SIM105
            self.db.execute(
                """INSERT INTO sources(source_id, kind, origin_url, created_at, title, meta_json)
                   VALUES(?,?,?,?,?,?)""",
                (sid, "youtube", url, time.time(), "pending acquisition", json_dumps({})),
            )
        except sqlite3.IntegrityError:
            pass
        return sid

    def _read_upload_row(self, sid: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT source_id, upload_path, title FROM sources WHERE source_id=?", (sid,)
        )

    def _is_valid_upload(self, path: Path, expected_hash: str) -> bool:
        """A recorded path is authoritative only if the file exists and matches the hash."""
        try:
            candidate = Path(path)
            return candidate.is_file() and file_hash(candidate) == expected_hash
        except OSError:
            return False

    def _canonical_for(self, sid: str, expected_hash: str) -> Path | None:
        """Validated canonical path from the winning row, or None when repair is needed."""
        row = self._read_upload_row(sid)
        if row is None or not row["upload_path"]:
            return None
        candidate = Path(row["upload_path"])
        if self._is_valid_upload(candidate, expected_hash):
            return candidate
        return None

    def _retire_loser(self, path: Path, canonical: Path | None) -> None:
        """Remove a staged file that is not the canonical media.

        Only removes files no registered source references. Never touches the
        canonical file or media owned by another registered source. A canonical
        of None means registration failed: drop the staged file when it is
        unreferenced so failures leave no orphans.
        """
        candidate = Path(path)
        if canonical is not None and candidate == canonical:
            return
        if not candidate.exists():
            return
        ref = self.db.query_one(
            "SELECT source_id FROM sources WHERE upload_path=?", (str(candidate),)
        )
        if ref is not None:
            return
        candidate.unlink(missing_ok=True)

    def _exclusive_copy(self, tmp: Path, stored: Path) -> None:
        """Cross-filesystem publish fallback that never replaces an existing file."""
        try:
            fd = os.open(stored, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            return  # concurrent publication won; the caller validates the winner
        try:
            with os.fdopen(fd, "wb") as out, open(tmp, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
        except BaseException:
            stored.unlink(missing_ok=True)
            raise

    def _clear_blocking_file(self, stored: Path, sid: str) -> None:
        """Remove the file blocking `stored` when no registered source claims it."""
        owner = self.db.query_one(
            "SELECT source_id FROM sources WHERE upload_path=?", (str(stored),)
        )
        if owner is not None and owner["source_id"] != sid:
            raise OSError(f"upload destination {stored} is owned by {owner['source_id']}")
        stored.unlink(missing_ok=True)

    def _publish_new(self, tmp: Path, stored: Path, sid: str, expected_hash: str) -> None:
        """Publish staged bytes as `stored` without replacing valid existing media.

        Uses an atomic hard link (never a replacing rename). A blocking file is
        cleared only when no registered source claims it; otherwise the attempt
        fails explicitly instead of deleting another source's media.

        Only EXDEV (cross-device) falls back to an exclusive create-and-copy;
        every other link error fails explicitly. In particular there is no
        generic copy fallback: a failed link never leaves a partially
        published canonical file, and `stored` is only ever created atomically
        (hard link or O_CREAT|O_EXCL), never replaced while valid.
        Supported filesystems are those providing atomic hard links within a
        directory. `tmp` and `stored` always share a directory, so EXDEV cannot
        occur in practice and the fallback is purely defensive.
        """
        if self._is_valid_upload(stored, expected_hash):
            return
        if stored.exists():
            self._clear_blocking_file(stored, sid)
        try:
            os.link(tmp, stored)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                self._exclusive_copy(tmp, stored)
            elif exc.errno != errno.EEXIST:
                raise
            # EEXIST: concurrent publication won; validate the winner below.
        if not self._is_valid_upload(stored, expected_hash):
            raise OSError(f"failed to publish upload {stored}")

    def _register_upload(self, sid: str, staged: Path, name: str, expected_hash: str) -> Path:
        """Insert the upload row; on conflict reconcile against the winning row.

        Returns the validated canonical path. Raises instead of returning a
        broken reference, and re-raises integrity errors that are not our own
        primary-key conflict.
        """
        meta = json_dumps({"original_filename": name})
        try:
            self.db.execute(
                """INSERT INTO sources(source_id, kind, upload_path, created_at, title,
                                      meta_json, content_hash)
                   VALUES(?,?,?,?,?,?,?)""",
                (sid, "upload", str(staged), time.time(), name, meta, expected_hash),
            )
            return staged
        except sqlite3.IntegrityError as exc:
            conflict = exc
        winner = self._read_upload_row(sid)
        if winner is None:
            # Not our primary-key conflict — do not swallow it.
            raise conflict
        if winner["upload_path"]:
            winner_path = Path(winner["upload_path"])
            if self._is_valid_upload(winner_path, expected_hash):
                return winner_path
            repaired = self._repair_upload_row(sid, winner_path, staged, expected_hash)
        else:
            repaired = self._repair_upload_row(sid, None, staged, expected_hash)
        if repaired is not None:
            return repaired
        raise OSError(f"upload registration for {sid} points to missing or corrupt media")

    def _repair_upload_row(
        self, sid: str, winner_path: Path | None, staged: Path, expected_hash: str
    ) -> Path | None:
        """Point a stale row at our validated staged file, or reconcile to a rival fix.

        The conditional UPDATE only fires when the row still holds the stale
        value we saw, so a concurrent repair is never clobbered. Returns the
        validated canonical path, or None when the row cannot be repaired.
        """
        if winner_path is not None:
            updated = self.db.execute(
                "UPDATE sources SET upload_path=?, content_hash=? WHERE source_id=? AND upload_path=?",
                (str(staged), expected_hash, sid, str(winner_path)),
            )
        else:
            # The winning row has no path at all: claim it only when it is
            # still unclaimed.
            updated = self.db.execute(
                """UPDATE sources SET upload_path=?, content_hash=? WHERE source_id=?
                   AND (upload_path IS NULL OR upload_path='')""",
                (str(staged), expected_hash, sid),
            )
        if updated.rowcount == 1:
            return staged
        winner = self._read_upload_row(sid)
        if (
            winner is not None
            and winner["upload_path"]
            and self._is_valid_upload(Path(winner["upload_path"]), expected_hash)
        ):
            return Path(winner["upload_path"])
        return None

    def create_upload_source(self, path: Path) -> str:
        """Register an uploaded file idempotently; never return a broken reference."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"upload source not found or not a file: {p}")
        sid = source_id_for_file(p)
        expected_hash = sid.removeprefix("file-")
        stored = self.paths.upload_path(sid, p.name)
        stored.parent.mkdir(parents=True, exist_ok=True)

        canonical = self._canonical_for(sid, expected_hash)
        if canonical is not None:
            # Deduplication: our computed path may be unreferenced crash garbage
            # from an earlier attempt with a different filename — drop it.
            self._retire_loser(stored, canonical)
            return sid

        final = self._stage_upload(p, stored, sid, expected_hash)
        # Remove only files this attempt staged that are not canonical.
        self._retire_loser(stored, final)
        return sid

    def _stage_upload(self, p: Path, stored: Path, sid: str, expected_hash: str) -> Path:
        """Copy, validate, publish and register; returns the validated canonical path."""
        tmp = stored.with_name(f".tmp.{uuid.uuid4().hex}.{stored.name}")
        try:
            shutil.copyfile(p, tmp)
            if not self._is_valid_upload(tmp, expected_hash):
                raise OSError(f"temp file hash mismatch for {sid}")
            self._publish_new(tmp, stored, sid, expected_hash)
            try:
                return self._register_upload(sid, stored, p.name, expected_hash)
            except Exception:
                # Registration failed after staging: drop our staged file when
                # no row references it so failures leave no orphans.
                self._retire_loser(stored, None)
                raise
        finally:
            tmp.unlink(missing_ok=True)

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        return dict(row) if row else None

    def list_sources(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM sources ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # jobs
    def create_job(
        self,
        source_id: str,
        *,
        profile: str | None = None,
        model_overrides: dict[str, str] | None = None,
        priority: int = 100,
        config: dict[str, Any] | None = None,
    ) -> str:
        settings = self.settings
        if profile and profile != settings.profile:
            settings = Settings.load(
                data_dir=settings.paths.root,
                profile=profile,
                device_index=settings.device_index,
                registry=settings.registry_path,
            )
        if model_overrides:
            settings = Settings.load(
                data_dir=settings.paths.root,
                profile=settings.profile,
                device_index=settings.device_index,
                registry=settings.registry_path,
                model_overrides=model_overrides,
            )
        job_id = new_id("job")
        cfg = {"profile": settings.profile, "model_overrides": model_overrides or {}, **(config or {})}
        self.db.execute(
            """INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, priority,
                                 config_json, created_at, updated_at)
               VALUES(?,?,?,?, 'queued', ?,?,?,?)""",
            (
                job_id,
                source_id,
                settings.profile,
                settings.fingerprint(),
                priority,
                json_dumps(cfg),
                time.time(),
                time.time(),
            ),
        )
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            """SELECT j.*, s.title AS source_title, s.kind AS source_kind
               FROM jobs j LEFT JOIN sources s ON s.source_id=j.source_id WHERE j.job_id=?""",
            (job_id,),
        )
        if row is None:
            return None
        out = dict(row)
        out["stages"] = [
            dict(r) for r in self.db.query("SELECT * FROM stages WHERE job_id=? ORDER BY rowid", (job_id,))
        ]
        return out

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            """SELECT j.*, s.title AS source_title FROM jobs j
               LEFT JOIN sources s ON s.source_id=j.source_id
               ORDER BY j.created_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    def request_cancel(self, job_id: str) -> bool:
        cur = self.db.execute(
            "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=? AND state='running'",
            (time.time(), job_id),
        )
        return bool(cur.rowcount)

    def cancel_check(self, job_id: str) -> bool:
        row = self.db.query_one("SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,))
        return bool(row and row["cancel_requested"])

    # ------------------------------------------------------------------
    # preflight
    def preview_url(self, url: str) -> dict[str, Any]:
        try:
            info = probe_url(url)
        except AcquisitionError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            log.warning("url preview failed: %s", exc)
            return {"ok": False, "error": "The video downloader (yt-dlp) could not be run on this server."}
        return {
            "ok": True,
            "video_id": info.video_id,
            "title": info.title,
            "uploader": info.uploader,
            "duration_s": info.duration_s,
            "chapters": info.chapters,
            "license": info.license,
            "has_manual_captions": any(not t.auto for t in info.subtitles),
            "captions": [{"lang": t.lang, "auto": t.auto, "count": len(t.captions)} for t in info.subtitles],
        }

    # ------------------------------------------------------------------
    def run_job(self, job_id: str, **kwargs) -> dict[str, Any]:
        from ..pipeline import run_job

        row = self.db.query_one("SELECT profile, config_json FROM jobs WHERE job_id=?", (job_id,))
        cfg = json_loads(row["config_json"] if row else None, {}) or {}
        settings = Settings.load(
            data_dir=self.paths.root,
            profile=(row["profile"] if row else None) or self.settings.profile,
            device_index=self.settings.device_index,
            registry=self.settings.registry_path,
            model_overrides=cfg.get("model_overrides") or None,
        )
        return run_job(job_id, self.db, settings, cancel_check=lambda: self.cancel_check(job_id), **kwargs)
