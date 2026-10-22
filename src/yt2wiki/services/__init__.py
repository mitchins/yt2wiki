"""Service layer: creating sources, jobs, and running the pipeline."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..db.schema import Database, json_dumps, json_loads
from ..paths import Paths, file_hash, source_id_for_file, source_id_for_youtube
from ..acquisition import AcquisitionError, probe_url, probe_upload

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
        # Race-safe: concurrent inserts for same sid must not raise 500 for legitimate duplicate.
        import sqlite3

        try:
            self.db.execute(
                """INSERT INTO sources(source_id, kind, origin_url, created_at, title, meta_json)
                   VALUES(?,?,?,?,?,?)""",
                (sid, "youtube", url, time.time(), "pending acquisition", json_dumps({})),
            )
        except sqlite3.IntegrityError:
            # Another thread inserted the same source_id concurrently — idempotent.
            pass
        return sid

    def create_upload_source(self, path: Path) -> str:
        # S8707: validate before hashing / copying (sink is file_hash -> open)
        # Race-safe, idempotent registration for concurrent duplicate uploads.
        import errno
        import os
        import shutil
        import sqlite3
        import uuid as _uuid

        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"upload source not found or not a file: {p}")
        sid = source_id_for_file(p)
        expected_hash = sid.removeprefix("file-")
        stored = self.paths.upload_path(sid, p.name)
        stored.parent.mkdir(parents=True, exist_ok=True)

        # Fast path: if source already registered and its file is valid, dedup.
        existing = self.db.query_one(
            "SELECT source_id, upload_path FROM sources WHERE source_id=?", (sid,)
        )
        if existing and existing["upload_path"]:
            existing_path = Path(existing["upload_path"])
            try:
                if existing_path.is_file() and file_hash(existing_path) == expected_hash:
                    return sid
            except (FileNotFoundError, OSError):
                pass
            # If the canonical file is missing/corrupt, fall through to ensure `stored` is valid.
            # For concurrent duplicates with same filename, `stored == existing_path`, so recreating `stored` restores it.

        # If stored already exists and is valid, ensure DB and return (handles file-race without DB race).
        if stored.exists():
            try:
                if file_hash(stored) == expected_hash:
                    try:
                        self.db.execute(
                            """INSERT INTO sources(source_id, kind, upload_path, created_at, title, meta_json)
                               VALUES(?,?,?,?,?,?)""",
                            (sid, "upload", str(stored), time.time(), p.name, json_dumps({})),
                        )
                    except sqlite3.IntegrityError:
                        pass
                    return sid
                else:
                    stored.unlink(missing_ok=True)
            except (FileNotFoundError, OSError):
                pass

        # Need to atomically publish `p` -> `stored` without overwriting a valid winner and without leaving orphans.
        tmp = stored.with_name(f".tmp.{_uuid.uuid4().hex}.{stored.name}")
        try:
            shutil.copyfile(p, tmp)
            try:
                if file_hash(tmp) != expected_hash:
                    tmp.unlink(missing_ok=True)
                    raise OSError(f"temp file hash mismatch for {sid}")
            except FileNotFoundError:
                raise OSError(f"temp file vanished for {sid}") from None
            # Atomic publish: hard-link fails if exists (no overwrite), falls back to rename.
            try:
                os.link(tmp, stored)
            except FileExistsError:
                pass
            except OSError as e:
                if e.errno == errno.EEXIST:
                    pass
                else:
                    if not stored.exists():
                        try:
                            tmp.rename(stored)
                        except FileExistsError:
                            pass
                        except OSError:
                            if stored.exists():
                                pass
                            else:
                                raise
            if not stored.exists() and tmp.exists():
                try:
                    tmp.rename(stored)
                except FileExistsError:
                    pass
                except OSError:
                    pass
            if not stored.exists():
                raise OSError(f"failed to publish upload {stored}")
            # DB insert: only IntegrityError is swallowed for legitimate duplicate.
            try:
                self.db.execute(
                    """INSERT INTO sources(source_id, kind, upload_path, created_at, title, meta_json)
                       VALUES(?,?,?,?,?,?)""",
                    (sid, "upload", str(stored), time.time(), p.name, json_dumps({})),
                )
            except sqlite3.IntegrityError:
                pass
            return sid
        finally:
            try:
                if tmp.exists():
                    # Hard-link case: tmp and stored share inode; unlinking tmp keeps stored.
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass

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
                data_dir=settings.paths.root, profile=profile,
                device_index=settings.device_index, registry=settings.registry_path,
            )
        if model_overrides:
            settings = Settings.load(
                data_dir=settings.paths.root, profile=settings.profile,
                device_index=settings.device_index, registry=settings.registry_path,
                model_overrides=model_overrides,
            )
        job_id = new_id("job")
        cfg = {"profile": settings.profile, "model_overrides": model_overrides or {}, **(config or {})}
        self.db.execute(
            """INSERT INTO jobs(job_id, source_id, profile, fingerprint, state, priority,
                                 config_json, created_at, updated_at)
               VALUES(?,?,?,?, 'queued', ?,?,?,?)""",
            (
                job_id, source_id, settings.profile, settings.fingerprint(), priority,
                json_dumps(cfg), time.time(), time.time(),
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
            dict(r) for r in self.db.query(
                "SELECT * FROM stages WHERE job_id=? ORDER BY rowid", (job_id,)
            )
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
            "captions": [
                {"lang": t.lang, "auto": t.auto, "count": len(t.captions)} for t in info.subtitles
            ],
        }

    # ------------------------------------------------------------------
    def run_job(self, job_id: str, **kwargs) -> dict[str, Any]:
        from ..pipeline import run_job

        row = self.db.query_one("SELECT profile, config_json FROM jobs WHERE job_id=?", (job_id,))
        cfg = json_loads(row["config_json"] if row else None, {}) or {}
        settings = Settings.load(
            data_dir=self.paths.root,
            profile=(row["profile"] if row else None) or self.settings.profile,
            device_index=self.settings.device_index, registry=self.settings.registry_path,
            model_overrides=cfg.get("model_overrides") or None,
        )
        return run_job(job_id, self.db, settings, cancel_check=lambda: self.cancel_check(job_id), **kwargs)
