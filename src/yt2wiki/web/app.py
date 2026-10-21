"""FastAPI application.

The article is the product: the library, reader and editor never need a
model.  Inference happens in a single background worker thread so only one
job (and one model set) is ever active, and HTTP requests stay responsive
while it runs.  Job state lives in SQLite, so a restart loses nothing.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import PROFILE_NAMES, ConfigError, Settings, load_registry
from ..db.schema import Database
from ..services import Service, new_id
from . import inference, views

log = logging.getLogger("yt2wiki.web")

_HERE = Path(__file__).resolve().parent

# Profiles offered for new jobs; "enhanced" stays CLI-only.
WEB_PROFILES = (
    ("compact", "Compact", "Fits an 8 GB GPU"),
    ("standard", "Standard", "12–16 GB GPU"),
)
ACTIVE_STATES = ("queued", "running")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    # Model loading happens inside the worker thread, so configure the
    # environment here too before any backend is imported.
    import os as _os

    _os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    _os.environ.setdefault("TQDM_DISABLE", "1")
    _os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    db = Database(settings.paths.db_path)
    db.migrate()
    service = Service(settings, db)
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["ts"] = lambda v: format_ts_filter(v)
    templates.env.filters["date"] = _fmt_date
    templates.env.filters["ago"] = _fmt_ago
    templates.env.filters["duration"] = views.fmt_duration
    templates.env.globals["web_profiles"] = WEB_PROFILES

    app = FastAPI(title="yt2wiki", version="0.2.0", docs_url="/api/docs")
    app.state.settings = settings
    app.state.db = db
    app.state.service = service
    app.state.runner = JobRunner(service)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.runner.recover()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        app.state.runner.stop()

    def page(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200) -> Any:
        base = {"request": request, "nav_active_jobs": len(app.state.runner.live()),
                "default_profile": _default_profile(settings)}
        return templates.TemplateResponse(request, name, {**base, **ctx},
                                          status_code=status_code)

    def active_jobs() -> list[dict[str, Any]]:
        live = app.state.runner.live()
        out = []
        for jid in live:
            job = service.get_job(jid)
            if job and job["state"] in ACTIVE_STATES:
                out.append(views.job_view(job, live=set(live)))
        return out

    # ------------------------------------------------------------------
    # pages
    @app.get("/", response_class=HTMLResponse)
    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, q: str = Query("")) -> Any:
        q = q.strip()[:200]
        cards = views.library_cards(db, settings.paths, q=q, fts_query=_fts_query(q) if q else "")
        total = db.query_one("SELECT COUNT(DISTINCT source_id) c FROM articles")["c"]
        return page(request, "library.html", {
            "nav": "library", "q": q, "cards": cards, "total": total,
            "jobs": active_jobs(), "error": request.query_params.get("error", ""),
        })

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, mode: str = Query("url")) -> Any:
        return page(request, "add.html", {"nav": "add", "mode": mode, "error": "",
                                          "url": "", "jobs": active_jobs()})

    @app.post("/add/url")
    def add_url_form(request: Request, url: str = Form(""), profile: str = Form(""),
                     back: str = Form("")) -> Any:
        try:
            job_id = submit_url(url, profile)
        except HTTPException as exc:
            if back == "library":
                from urllib.parse import quote
                return RedirectResponse(f"/?error={quote(str(exc.detail))}#add", status_code=303)
            return page(request, "add.html", {"nav": "add", "mode": "url", "error": exc.detail,
                                              "url": url, "jobs": active_jobs()},
                        status_code=exc.status_code)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/add/upload")
    async def add_upload_form(request: Request, file: UploadFile = File(None),
                              profile: str = Form("")) -> Any:
        try:
            job_id = await submit_upload(file, profile)
        except HTTPException as exc:
            return page(request, "add.html", {"nav": "add", "mode": "upload",
                                              "error": exc.detail, "url": "",
                                              "jobs": active_jobs()},
                        status_code=exc.status_code)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request) -> Any:
        live = set(app.state.runner.live())
        jobs = [views.job_view(service.get_job(j["job_id"]), live=live)
                for j in service.list_jobs(60)]
        published = {r["job_id"]: r["article_id"] for r in db.query(
            "SELECT job_id, article_id FROM articles WHERE job_id IS NOT NULL")}
        latest: set[str] = set()
        for j in jobs:  # newest first
            j["article_id"] = j.get("article_id") or published.get(j["job_id"])
            # Only the newest job of a source offers Retry; older failures
            # were superseded by whatever ran after them.
            if j["source_id"] in latest:
                j["can_retry"] = False
            latest.add(j["source_id"])
        current = [j for j in jobs if j["state"] == "running"]
        queued = [j for j in jobs if j["state"] == "queued"]
        history = [j for j in jobs if j["state"] not in ("running", "queued")]
        return page(request, "jobs.html", {"nav": "jobs", "current": current,
                                           "queued": queued, "history": history})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> Any:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        view = views.job_view(job, live=set(app.state.runner.live()))
        logs = [dict(r) for r in db.query(
            "SELECT level, message, ts FROM app_log WHERE job_id=? ORDER BY id DESC LIMIT 40",
            (job_id,))]
        article = None
        row = db.query_one(
            "SELECT article_id, title FROM articles WHERE article_id=? OR job_id=? LIMIT 1",
            (view.get("article_id") or "", job_id))
        if row:
            article = dict(row)
        return page(request, "job.html", {"nav": "jobs", "job": view, "logs": logs,
                                          "article": article,
                                          "source": service.get_source(job["source_id"])})

    @app.post("/jobs/{job_id}/retry")
    def job_retry(job_id: str) -> Any:
        return RedirectResponse(f"/jobs/{retry_job(job_id)}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel_form(job_id: str) -> Any:
        app.state.runner.cancel_queued(job_id)
        return RedirectResponse("/jobs", status_code=303)

    @app.get("/articles/{article_id}", response_class=HTMLResponse)
    def read_article(request: Request, article_id: str) -> Any:
        ctx = views.article_context(db, settings.paths, article_id)
        if ctx is None:
            raise HTTPException(404, "article not found")
        source_id = ctx["article"]["source_id"]
        frames = db.query(
            """SELECT f.frame_id, f.t, f.kind, f.reason, p.payload_json FROM frames f
               LEFT JOIN frame_interpretations p ON p.frame_id=f.frame_id
               WHERE f.source_id=? ORDER BY f.t""",
            (source_id,),
        )
        findings = db.query(
            "SELECT * FROM findings WHERE article_id=? ORDER BY severity, created_at",
            (article_id,),
        )
        evidence = db.query(
            "SELECT evidence_id, kind, text, t_start, t_end, frame_ids_json FROM evidence "
            "WHERE source_id=? ORDER BY t_start",
            (source_id,),
        )
        meta = json.loads((ctx["source"] or {}).get("meta_json") or "{}")
        frames = [f for f in frames
                  if _frame_file(settings, db, source_id, f"{f['frame_id']}.jpg") is not None]
        return page(request, "article.html", {
            **ctx, "nav": "library",
            "frames": [_frame_out(f) for f in frames],
            "findings": [dict(f) for f in findings],
            "evidence": [_evidence_out(e) for e in evidence],
            "provenance": meta.get("provenance", {}),
        })

    @app.get("/articles/{article_id}/edit", response_class=HTMLResponse)
    def edit_article(request: Request, article_id: str) -> Any:
        row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        article = dict(row)
        assets = [dict(a) for a in db.query(
            """SELECT a.frame_id, a.caption, f.t AS t FROM article_assets a
               LEFT JOIN frames f ON f.frame_id=a.frame_id WHERE a.article_id=?""",
            (article_id,))]
        order = _image_order(article["markdown"])
        sections = _section_of_images(article["markdown"])
        assets = [a for a in assets if a["frame_id"] in order]
        assets.sort(key=lambda a: order.index(a["frame_id"]))
        for a in assets:
            a["section"] = sections.get(a["frame_id"], "")
        revisions = [dict(r) for r in db.query(
            "SELECT revision_id, version, edit_note, origin, created_at FROM article_revisions "
            "WHERE article_id=? ORDER BY version DESC, created_at DESC LIMIT 50", (article_id,))]
        return page(request, "edit.html", {
            "nav": "library", "article": article, "assets": assets,
            "headings": _h2_headings(article["markdown"]), "revisions": revisions,
        })

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Any:
        cfg = inference.load(settings.paths)
        db_size = sum(p.stat().st_size for p in settings.paths.db_dir.glob("yt2wiki.sqlite3*")
                      if p.is_file())
        n_articles = db.query_one("SELECT COUNT(*) c FROM articles")["c"]
        return page(request, "settings.html", {
            "nav": "settings", "inference": cfg, "status": inference.load_status(settings.paths),
            "roles": inference.ROLES, "data_dir": str(settings.paths.root),
            "db_path": str(settings.paths.db_path), "db_size": db_size,
            "n_articles": n_articles,
        })

    @app.get("/assets/{article_id}/{filename}")
    def asset(article_id: str, filename: str) -> Any:
        if "/" in filename or ".." in filename or "/" in article_id or ".." in article_id:
            raise HTTPException(400, "invalid filename")
        path = settings.paths.article_assets(article_id) / filename
        if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(404, "asset not found")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/frames/{source_id}/{filename}")
    def source_frame(source_id: str, filename: str) -> Any:
        if ("/" in filename or ".." in filename or ".." in source_id
                or not filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            raise HTTPException(400, "invalid filename")
        path = _frame_file(settings, db, source_id, filename)
        if path is None:
            raise HTTPException(404, "frame not found")
        return FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------------
    # submission (shared by the HTML forms and the JSON API)
    def _profile(value: str) -> str:
        profile = (value or _default_profile(settings)).strip()
        if profile not in PROFILE_NAMES:
            raise HTTPException(400, f"Unknown profile “{profile}”.")
        try:
            Settings.load(data_dir=settings.paths.root, profile=profile)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        return profile

    def _inference_snapshot() -> dict[str, Any]:
        """Capture the effective provider/model identities at submission time.

        The snapshot is stored in the job's config_json so that running jobs
        retain their selected configuration even if settings change later, and
        so that later inspection can prove which provider was actually invoked.
        """
        cfg = inference.load(settings.paths)
        st = inference.load_status(settings.paths)
        snap: dict[str, Any] = {}
        for role in ("writer", "vision"):
            name = (cfg.get("roles") or {}).get(role)
            if not name:
                continue
            prov = (cfg.get("providers") or {}).get(name, {})
            status = st.get(name, {})
            snap[role] = {
                "provider": name,
                "url": prov.get("url"),
                "model": prov.get("model"),
                "api_key_env": prov.get("api_key_env"),
                "reachable": bool(status.get("ok")),
                "qualified_for_writer": bool(status.get("qualified_for_writer")),
                "qualified_for_vision": bool(status.get("qualified_for_vision")),
                "models": status.get("models", [])[:5] if status.get("models") else [],
            }
        return snap

    def enqueue(source_id: str, profile: str, **config: Any) -> str:
        for live_id in app.state.runner.live():
            row = db.query_one("SELECT source_id FROM jobs WHERE job_id=?", (live_id,))
            if row and row["source_id"] == source_id:
                return live_id  # already queued or running here: no duplicate work
        snap = _inference_snapshot()
        job = service.create_job(source_id, profile=profile,
                                 config={"pipeline": "v9", "origin": "web",
                                         "inference": snap, **config})
        app.state.runner.submit(job)
        return job

    def submit_url(url: str, profile: str) -> str:
        url = (url or "").strip()
        if not url:
            raise HTTPException(400, "Paste a YouTube link to make a guide.")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "That doesn’t look like a web link — it should start with https://")
        profile = _profile(profile)
        preview = service.preview_url(url)
        if not preview.get("ok"):
            raise HTTPException(400, preview.get("error", "That video could not be reached."))
        sid = service.create_youtube_source(url)
        return enqueue(sid, profile)

    async def submit_upload(file: UploadFile | None, profile: str) -> str:
        if file is None or not file.filename:
            raise HTTPException(400, "Choose a video file to upload.")
        profile = _profile(profile)
        upload_id = new_id("upl")
        dest = settings.paths.upload_path(upload_id, Path(file.filename).name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(dest, "wb") as fh:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                fh.write(chunk)
        if size == 0:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "That file is empty.")
        sid = service.create_upload_source(dest)
        if service.get_source(sid).get("upload_path") != str(dest):
            shutil.rmtree(dest.parent, ignore_errors=True)  # stored under its source id
        return enqueue(sid, profile)

    def retry_job(job_id: str) -> str:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return enqueue(job["source_id"], job["profile"] if job["profile"] in PROFILE_NAMES
                       else _default_profile(settings), retry_of=job_id)

    # ------------------------------------------------------------------
    # api
    @app.post("/api/sources/url")
    def api_add_url(payload: dict = Body(...)) -> Any:
        job = submit_url(payload.get("url") or "", payload.get("profile") or "")
        return {"source_id": service.get_job(job)["source_id"], "job_id": job}

    @app.post("/api/sources/upload")
    async def api_upload(
        file: UploadFile = File(...),
        profile: str = Form(default=""),
    ) -> Any:
        job = await submit_upload(file, profile)
        return {"source_id": service.get_job(job)["source_id"], "job_id": job}

    @app.get("/api/jobs/active")
    def api_jobs_active() -> Any:
        return {"jobs": [_job_json(j) for j in active_jobs()]}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> Any:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        stages = {s["stage"]: s for s in job["stages"]}
        out = {k: v for k, v in job.items() if k != "stages"}
        out["stages"] = stages
        out["view"] = _job_json(views.job_view(job, live=set(app.state.runner.live())))
        out["log"] = [
            dict(r)
            for r in db.query(
                "SELECT level, message, ts FROM app_log WHERE job_id=? ORDER BY id DESC LIMIT 25",
                (job_id,),
            )
        ]
        return out

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel(job_id: str) -> Any:
        return {"cancelled": app.state.runner.cancel_queued(job_id)
                or service.request_cancel(job_id)}

    @app.post("/api/jobs/{job_id}/reprocess")
    def api_reprocess(job_id: str, payload: dict = Body(default={})) -> Any:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        profile = _profile(payload.get("profile") or job["profile"])
        if payload.get("pipeline") == "legacy":
            force = bool(payload.get("force"))
            new_job = service.create_job(
                job["source_id"], profile=profile,
                model_overrides=payload.get("model_overrides") or {},
                config={"force": force, "pipeline": "legacy", "origin": "web"})
            app.state.runner.submit(new_job, force=force)
        else:
            new_job = enqueue(job["source_id"], profile, retry_of=job_id)
        return {"job_id": new_job, "source_id": job["source_id"]}

    @app.post("/api/settings/inference")
    def api_save_inference(payload: dict = Body(...)) -> Any:
        try:
            cfg = inference.save(settings.paths, payload)
        except inference.InferenceConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "config": cfg}

    @app.post("/api/settings/inference/test")
    def api_test_inference(payload: dict = Body(...)) -> Any:
        url = str(payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "Enter an http:// or https:// base URL first.")
        result = inference.test_endpoint(url, str(payload.get("api_key_env") or "").strip())
        name = str(payload.get("name") or "").strip().lower()
        if name and name in inference.load(settings.paths)["providers"]:
            inference.record_test(settings.paths, name, result)
        return result

    @app.post("/api/preview")
    def api_preview(payload: dict = Body(...)) -> Any:
        article_id = str(payload.get("article_id") or "")
        md = payload.get("markdown")
        if not isinstance(md, str):
            raise HTTPException(400, "markdown is required")
        if db.query_one("SELECT 1 FROM articles WHERE article_id=?", (article_id,)) is None:
            raise HTTPException(404, "article not found")
        view = views.render_reader(settings.paths, article_id, md)
        return {"title": view["title"], "lead_html": view["lead_html"],
                "body_html": view["body_html"]}

    @app.post("/api/articles/{article_id}/images/move")
    def api_image_move(article_id: str, payload: dict = Body(...)) -> Any:
        fid = (payload.get("frame_id") or "").strip()
        heading = (payload.get("section") or "").strip()
        row = db.query_one("SELECT markdown FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        try:
            md = _move_image(row["markdown"], fid, heading)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        version = _save_article_markdown(settings, db, article_id, md,
                                         f"image moved to “{heading}” by user")
        return {"ok": True, "version": version}

    @app.get("/api/articles/{article_id}/export.md")
    def api_export_md(article_id: str) -> Any:
        path = settings.paths.article_markdown(article_id)
        if not path.is_file():
            raise HTTPException(404, "article not found")
        return FileResponse(path, media_type="text/markdown",
                            filename=f"{article_id}.md")

    @app.get("/api/articles/{article_id}/export.zip")
    def api_export_zip(article_id: str) -> Any:
        path = settings.paths.article_dir(article_id) / "article.zip"
        if not path.is_file():
            raise HTTPException(404, "bundle not found")
        return FileResponse(path, media_type="application/zip",
                            filename=f"{article_id}.zip")

    @app.get("/api/articles/{article_id}/standalone")
    def api_export_standalone(article_id: str) -> Any:
        path = settings.paths.article_dir(article_id) / "article.standalone.html"
        if not path.is_file():
            raise HTTPException(404, "standalone not found")
        return FileResponse(path, media_type="text/html",
                            filename=f"{article_id}.html")

    @app.post("/api/articles/{article_id}")
    def api_edit_article(article_id: str, payload: dict = Body(...)) -> Any:
        markdown = payload.get("markdown")
        note = (payload.get("note") or "").strip()
        if not isinstance(markdown, str) or len(markdown) < 20:
            raise HTTPException(400, "markdown is required")
        row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        version = _save_article_markdown(settings, db, article_id, markdown,
                                          note or "edited by user")
        return {"ok": True, "version": version, "note": note}

    @app.get("/api/articles/{article_id}/revisions")
    def api_revisions(article_id: str) -> Any:
        rows = db.query(
            "SELECT * FROM article_revisions WHERE article_id=? ORDER BY version DESC LIMIT 50",
            (article_id,),
        )
        return [dict(r) for r in rows]

    @app.post("/api/articles/{article_id}/revert")
    def api_revert(article_id: str, payload: dict = Body(...)) -> Any:
        revision_id = payload.get("revision_id")
        row = db.query_one(
            "SELECT * FROM article_revisions WHERE revision_id=? AND article_id=?",
            (revision_id, article_id),
        )
        if row is None:
            raise HTTPException(404, "revision not found")
        # Restoring is itself an edit: the current text is snapshotted first,
        # and search, exports and the version number all move forward.
        version = _save_article_markdown(settings, db, article_id, row["markdown"],
                                         f"restored version {row['version']}")
        return {"ok": True, "version": version}

    @app.get("/api/articles/{article_id}/revisions/{revision_id}")
    def api_revision(article_id: str, revision_id: str) -> Any:
        row = db.query_one(
            "SELECT revision_id, version, markdown, edit_note, origin, created_at "
            "FROM article_revisions WHERE revision_id=? AND article_id=?",
            (revision_id, article_id))
        if row is None:
            raise HTTPException(404, "revision not found")
        return dict(row)

    @app.get("/api/v9/articles/{article_id}/sections")
    def api_v9_sections(article_id: str) -> Any:
        row = db.query_one("SELECT outline_json FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        import json as _json
        try:
            secs = _json.loads(row["outline_json"] or "[]")
        except Exception:  # noqa: BLE001
            secs = []
        return [{"section_id": s.get("section_id"), "heading": s.get("heading"),
                 "t_start": s.get("t_start"), "t_end": s.get("t_end")} for s in secs]

    @app.get("/api/v9/articles/{article_id}/alternatives")
    def api_v9_alternatives(article_id: str, section_id: str = Query(""),
                                  limit: int = Query(8)) -> Any:
        row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        import json as _json
        secs = []
        try:
            secs = _json.loads(row["outline_json"] or "[]")
        except Exception:  # noqa: BLE001
            secs = []
        sec = next((s for s in secs if s.get("section_id") == section_id),
                   secs[0] if secs else None)
        if sec is None and not section_id:
            rows = db.query(
                "SELECT frame_id, t, kind FROM frames WHERE source_id=? ORDER BY t",
                (row["source_id"],))
            rows = [r for r in rows
                    if _frame_file(settings, db, row["source_id"], f"{r['frame_id']}.jpg")]
            if len(rows) > limit:
                step = len(rows) / limit
                rows = [rows[int(i * step)] for i in range(limit)]
            return {"section_id": "", "window": None,
                    "frames": [dict(r) for r in rows]}
        if sec is None:
            raise HTTPException(404, "section not found")
        lo, hi = float(sec.get("t_start", 0)) - 20.0, float(sec.get("t_end", 0)) + 20.0
        rows = db.query(
            """SELECT f.frame_id, f.t, f.kind FROM frames f
               WHERE f.source_id=? AND f.t BETWEEN ? AND ? ORDER BY f.t""",
            (row["source_id"], lo, hi))
        rows = [r for r in rows
                if _frame_file(settings, db, row["source_id"], f"{r['frame_id']}.jpg")]
        if len(rows) > limit:
            step = len(rows) / limit
            rows = [rows[int(i * step)] for i in range(limit)]
        return {"section_id": sec.get("section_id"), "window": [lo, hi],
                "frames": [dict(r) for r in rows]}

    @app.post("/api/articles/{article_id}/images/replace")
    def api_image_replace(article_id: str, payload: dict = Body(...)) -> Any:
        old = (payload.get("old_frame_id") or "").strip()
        new = (payload.get("new_frame_id") or "").strip()
        caption = payload.get("caption")
        if not old or not new:
            raise HTTPException(400, "old_frame_id and new_frame_id required")
        return _swap_article_image(settings, db, article_id, old, new,
                                   caption=caption, note="image replaced by user")

    @app.post("/api/articles/{article_id}/images/remove")
    def api_image_remove(article_id: str, payload: dict = Body(...)) -> Any:
        old = (payload.get("frame_id") or "").strip()
        if not old:
            raise HTTPException(400, "frame_id required")
        return _swap_article_image(settings, db, article_id, old, None,
                                   note="image removed by user")

    @app.post("/api/articles/{article_id}/images/caption")
    def api_image_caption(article_id: str, payload: dict = Body(...)) -> Any:
        fid = (payload.get("frame_id") or "").strip()
        caption = (payload.get("caption") or "").strip()
        if not fid:
            raise HTTPException(400, "frame_id required")
        row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
        if row is None:
            raise HTTPException(404, "article not found")
        md = row["markdown"]
        md = _set_caption(md, fid, caption)
        import re as _re2
        md = _re2.sub(
            rf"!\[[^\]]*\]\(assets/{_re2.escape(fid)}\.jpg\)",
            f"![{caption}](assets/{fid}.jpg)" if caption else f"![Source frame](assets/{fid}.jpg)",
            md)
        _save_article_markdown(settings, db, article_id, md, "caption edited by user")
        with db.transaction() as conn:
            conn.execute("UPDATE article_assets SET caption=? WHERE article_id=? AND frame_id=?",
                         (caption, article_id, fid))
        return {"ok": True}

    @app.get("/api/search")
    def api_search(q: str = Query(""), scope: str = Query("articles")) -> Any:
        q = (q or "").strip()
        if not q:
            return {"results": []}
        terms = _fts_query(q)
        if scope == "evidence":
            sql = ("SELECT e.evidence_id, e.kind, e.text, e.t_start, s.title "
                   "FROM evidence_fts f JOIN evidence e ON e.evidence_id=f.evidence_id "
                   "JOIN sources s ON s.source_id=e.source_id WHERE evidence_fts MATCH ? "
                   "ORDER BY rank LIMIT 50")
        else:
            sql = ("SELECT a.article_id, a.title, a.updated_at, s.uploader "
                   "FROM articles_fts f JOIN articles a ON a.article_id=f.article_id "
                   "JOIN sources s ON s.source_id=a.source_id WHERE articles_fts MATCH ? "
                   "ORDER BY rank LIMIT 50")
        rows = db.query(sql, (terms,))
        return {"results": [dict(r) for r in rows], "query": terms}

    @app.get("/api/registry")
    def api_registry() -> Any:
        reg = load_registry(settings.registry_path)
        models = {}
        for key, spec in reg["models"].items():
            models[key] = {
                "role": spec.role,
                "repo": spec.repo,
                "license": spec.license,
                "params_b": spec.params_b,
                "provider": spec.provider,
                "notes": spec.notes,
                "vram_gb": spec.vram_gb,
                "fallbacks": spec.fallbacks,
                "variants": spec.variants,
            }
        return {
            "profiles": reg["profiles"],
            "assignments": reg["assignments"],
            "models": models,
            "current_profile": settings.profile,
            "fingerprint": settings.fingerprint(),
        }

    @app.get("/api/gpu")
    def api_gpu() -> Any:
        from ..gpu import nvidia_smi_gpus, cuda_available

        return {
            "cuda": cuda_available(),
            "gpus": [g.__dict__ for g in nvidia_smi_gpus()],
            "vram_budget_gb": settings.vram_budget_gb(),
        }

    @app.get("/api/sources/{source_id}/evidence")
    def api_evidence(source_id: str) -> Any:
        rows = db.query(
            "SELECT * FROM evidence WHERE source_id=? ORDER BY t_start", (source_id,)
        )
        return [_evidence_out(r) for r in rows]

    @app.get("/api/sources/{source_id}/transcript")
    def api_transcript(source_id: str) -> Any:
        rows = db.query(
            "SELECT idx, start, end, text FROM transcript_segments WHERE source_id=? "
            "ORDER BY start",
            (source_id,),
        )
        return [dict(r) for r in rows]

    @app.get("/api/sources/{source_id}/frames")
    def api_frames(source_id: str) -> Any:
        rows = db.query(
            """SELECT f.frame_id, f.t, f.kind, f.reason, p.payload_json FROM frames f
               LEFT JOIN frame_interpretations p ON p.frame_id=f.frame_id
               WHERE f.source_id=? ORDER BY f.t""",
            (source_id,),
        )
        return [_frame_out(r) for r in rows]

    @app.post("/api/sources/{source_id}/reprocess")
    def api_reprocess_source(source_id: str, payload: dict = Body(default={})) -> Any:
        if service.get_source(source_id) is None:
            raise HTTPException(404, "source not found")
        profile = _profile(payload.get("profile") or "")
        if payload.get("pipeline") == "legacy":
            force = bool(payload.get("force"))
            job = service.create_job(source_id, profile=profile,
                                     model_overrides=payload.get("model_overrides") or {},
                                     config={"force": force, "pipeline": "legacy",
                                             "origin": "web"})
            app.state.runner.submit(job, force=force)
            return {"job_id": job}
        return {"job_id": enqueue(source_id, profile)}

    @app.get("/api/articles_list")
    def api_articles_list() -> Any:
        rows = db.query(
            """SELECT a.article_id, a.title, a.status, a.updated_at, s.uploader
               FROM articles a JOIN sources s ON s.source_id=a.source_id
               ORDER BY a.updated_at DESC LIMIT 200"""
        )
        return [dict(r) for r in rows]

    @app.get("/media/{source_id}/{filename}")
    def media(source_id: str, filename: str) -> Any:
        if "/" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        row = db.query_one("SELECT source_id FROM sources WHERE source_id=?", (source_id,))
        if row is None:
            raise HTTPException(404, "unknown source")
        path = settings.paths.source_dir(source_id) / filename
        if not path.is_file() or path.suffix.lower() not in (".mp4", ".webm", ".mkv"):
            raise HTTPException(404, "media not found")
        return FileResponse(path)

    @app.get("/health")
    def health() -> Any:
        return {"ok": True, "profile": settings.profile, "data": str(settings.paths.root)}

    return app


# ----------------------------------------------------------------------
class _RunnerHolder:
    instance: "JobRunner | None" = None


def _save_article_markdown(settings, db, article_id: str, markdown: str, note: str) -> int:
    from ..article import markdown_to_html, render_html
    from ..article import Article
    from ..services import new_id

    from ..reader import split_markdown

    row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
    title = split_markdown(markdown).title or row["title"]
    article = Article(source_id=row["source_id"], title=title)
    html = render_html(article, markdown_to_html(markdown), embed_images=False,
                       asset_dir=settings.paths.article_assets(article_id))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO article_revisions(revision_id, article_id, version, markdown, html, "
            "edit_note, origin, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("rev"), article_id, int(row["version"]), row["markdown"], row["html"],
             "automatic pre-edit snapshot", "user", time.time()))
        conn.execute(
            "UPDATE articles SET title=?, markdown=?, html=?, version=version+1, updated_at=?, "
            "edit_note=? WHERE article_id=?",
            (title, markdown, html, time.time(), note, article_id))
        conn.execute("DELETE FROM articles_fts WHERE article_id=?", (article_id,))
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
                     (article_id, title, markdown))
    settings.paths.article_dir(article_id).mkdir(parents=True, exist_ok=True)
    settings.paths.article_markdown(article_id).write_text(markdown, encoding="utf-8")
    settings.paths.article_html(article_id).write_text(html, encoding="utf-8")
    import zipfile as _zipfile
    assets = settings.paths.article_assets(article_id)
    zp = settings.paths.article_dir(article_id) / "article.zip"
    with _zipfile.ZipFile(zp, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.write(settings.paths.article_markdown(article_id), "article.md")
        for f in sorted(assets.glob("*")):
            if f.is_file():
                zf.write(f, f"assets/{f.name}")
    (settings.paths.article_dir(article_id) / "article.standalone.html").write_text(
        render_html(article, markdown_to_html(markdown), embed_images=True, asset_dir=assets),
        encoding="utf-8")
    return int(row["version"]) + 1


def _set_caption(markdown: str, frame_id: str, caption: str) -> str:
    import re
    lines = markdown.split("\n")
    out = []
    i = 0
    changed = False
    while i < len(lines):
        out.append(lines[i])
        if re.match(rf"!\[.*\]\(assets/{re.escape(frame_id)}\.jpg\)$", lines[i].strip()):
            if i + 1 < len(lines) and lines[i + 1].strip() == "":
                out.append("")
                i += 2
                if i < len(lines) and lines[i].strip().startswith("*"):
                    i += 1  # drop the superseded caption line
                capline = f"*{caption}*" if caption else "*Source frame*"
                out.append(capline)
                changed = True
                continue
        i += 1
    return "\n".join(out) if changed else markdown


def _swap_article_image(settings, db, article_id: str, old_fid: str,
                        new_fid: str | None, caption: str | None = None,
                        note: str = "image edited by user") -> dict[str, Any]:
    import shutil
    row = db.query_one("SELECT * FROM articles WHERE article_id=?", (article_id,))
    if row is None:
        from fastapi import HTTPException as _HE
        raise _HE(404, "article not found")
    md = row["markdown"]
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    if new_fid:
        frow = db.query_one("SELECT path FROM frames WHERE frame_id=?", (new_fid,))
        if frow is None or not Path(frow["path"]).is_file():
            from fastapi import HTTPException as _HE
            raise _HE(404, "replacement frame not found")
        shutil.copy2(frow["path"], assets / f"{new_fid}.jpg")
        md = md.replace(f"assets/{old_fid}.jpg", f"assets/{new_fid}.jpg")
        with db.transaction() as conn:
            conn.execute("DELETE FROM article_assets WHERE article_id=? AND frame_id=?",
                         (article_id, old_fid))
            conn.execute(
                """INSERT OR REPLACE INTO article_assets(asset_id, article_id, frame_id, path,
                                                          caption, alt_text, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (f"asset-{new_fid}", article_id, new_fid, str(assets / f"{new_fid}.jpg"),
                 caption or "", "", time.time()))
        (assets / f"{old_fid}.jpg").unlink(missing_ok=True)
    else:
        import re as _re
        raw = md.split("\n")
        lines = []
        skip_next_caption = False
        for l in raw:
            if f"assets/{old_fid}.jpg" in l:
                skip_next_caption = True
                continue
            if skip_next_caption:
                if not l.strip():
                    continue
                skip_next_caption = False
                if l.strip().startswith("*"):
                    continue
            lines.append(l)
        # drop the caption line that immediately followed the removed image
        md = "\n".join(lines)
        with db.transaction() as conn:
            conn.execute("DELETE FROM article_assets WHERE article_id=? AND frame_id=?",
                         (article_id, old_fid))
        (assets / f"{old_fid}.jpg").unlink(missing_ok=True)
    if new_fid:
        md = _set_caption(md, new_fid, caption if caption is not None else "")
        import re as _re2
        md = _re2.sub(
            rf"!\[[^\]]*\]\(assets/{_re2.escape(new_fid)}\.jpg\)",
            f"![{caption}](assets/{new_fid}.jpg)" if caption else f"![Source frame](assets/{new_fid}.jpg)",
            md)
        db.execute("UPDATE article_assets SET caption=? WHERE article_id=? AND frame_id=?",
                   (caption or "", article_id, new_fid))
    version = _save_article_markdown(settings, db, article_id, md, note)
    return {"ok": True, "version": version}

def _frame_file(settings: Settings, db: Database, source_id: str, filename: str) -> Path | None:
    """A source frame on disk: under this data root, else where its row says.

    Frame rows store absolute paths, which can point into the library the
    frames were extracted in; only a path recorded for that exact frame id
    and source is ever served.
    """
    path = settings.paths.source_frames(source_id) / filename
    if path.is_file():
        return path
    row = db.query_one("SELECT path FROM frames WHERE source_id=? AND frame_id=?",
                       (source_id, Path(filename).stem))
    if row and row["path"]:
        recorded = Path(row["path"])
        if recorded.name == filename and recorded.is_file():
            return recorded
    return None


def _default_profile(settings: Settings) -> str:
    names = [p[0] for p in WEB_PROFILES]
    return settings.profile if settings.profile in names else names[0]


def _job_json(view: dict[str, Any]) -> dict[str, Any]:
    keep = ("job_id", "source_id", "source_title", "state", "state_label", "tone",
            "progress", "current_label", "elapsed_s", "article_id", "error", "steps",
            "can_retry", "can_cancel")
    return {k: view.get(k) for k in keep}


def _fmt_date(ts: Any) -> str:
    if not ts:
        return ""
    t = time.localtime(float(ts))
    return f"{t.tm_mday} {time.strftime('%b %Y', t)}"


def _fmt_ago(ts: Any) -> str:
    if not ts:
        return ""
    d = time.time() - float(ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 7 * 86400:
        n = int(d // 86400)
        return "yesterday" if n == 1 else f"{n} days ago"
    return _fmt_date(ts)


_IMG_LINE = r"!\[[^\]]*\]\(assets/([^)\s/]+)\.jpg\)"


def _image_order(markdown: str) -> list[str]:
    import re

    return re.findall(_IMG_LINE, markdown or "")


def _h2_headings(markdown: str) -> list[str]:
    return [ln[3:].strip() for ln in (markdown or "").split("\n") if ln.startswith("## ")]


def _section_of_images(markdown: str) -> dict[str, str]:
    import re

    out: dict[str, str] = {}
    current = ""
    for ln in (markdown or "").split("\n"):
        if ln.startswith("## "):
            current = ln[3:].strip()
        m = re.match(_IMG_LINE + r"\s*$", ln.strip())
        if m:
            out[m.group(1)] = current
    return out


def _move_image(markdown: str, frame_id: str, heading: str) -> str:
    """Move an image (and its caption line) to the end of another section."""
    import re

    lines = markdown.split("\n")
    pat = re.compile(r"^!\[[^\]]*\]\(assets/" + re.escape(frame_id) + r"\.jpg\)\s*$")
    at = next((i for i, ln in enumerate(lines) if pat.match(ln.strip())), None)
    if at is None:
        raise ValueError("That image is not in the article.")
    if heading not in _h2_headings(markdown):
        raise ValueError("Unknown section.")
    end = at + 1
    k = end
    while k < len(lines) and not lines[k].strip():
        k += 1
    if k < len(lines) and re.match(r"^\*.+\*$", lines[k].strip()):
        end = k + 1
    block = [ln for ln in lines[at:end] if ln.strip()]
    rest = lines[:at] + lines[end:]
    start = next(i for i, ln in enumerate(rest) if ln.startswith("## ") and ln[3:].strip() == heading)
    stop = next((i for i in range(start + 1, len(rest)) if rest[i].startswith("## ")), len(rest))
    while stop > start + 1 and not rest[stop - 1].strip():
        stop -= 1
    insert = [""] + [x for pair in zip(block, [""] * len(block)) for x in pair]
    out = rest[:stop] + insert + rest[stop:]
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def _frame_out(row: Any) -> dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}") if row["payload_json"] else {}
    return {
        "frame_id": row["frame_id"],
        "t": row["t"],
        "kind": row["kind"],
        "summary": payload.get("summary", ""),
        "frame_type": payload.get("frame_type", ""),
        "visible_text": payload.get("visible_text", []),
        "legibility": payload.get("legibility", ""),
        "reason": row["reason"] or "",
    }


def _evidence_out(row: Any) -> dict[str, Any]:
    return {
        "evidence_id": row["evidence_id"],
        "kind": row["kind"],
        "text": row["text"],
        "t_start": row["t_start"],
        "t_end": row["t_end"],
        "frame_ids": json.loads(row["frame_ids_json"] or "[]"),
    }


def _fts_query(q: str) -> str:
    terms = [t for t in q.split() if t.strip()]
    safe = []
    for t in terms:
        t = t.replace('"', "")
        safe.append(f'"{t}"' if not t.isalnum() else f"{t}*")
    return " AND ".join(safe) if safe else q


def format_ts_filter(value: Any) -> str:
    from ..article import format_ts

    return format_ts(value)


# ----------------------------------------------------------------------
_WORKER_ID = f"{__import__('socket').gethostname()}:{__import__('os').getpid()}"


def _worker_alive(worker: str | None) -> bool:
    """True when `worker` (host:pid) is another live process on this host.

    Workers on other hosts are treated as alive: their liveness cannot be
    checked from here, and leaving a job alone is the safe default.
    """
    import os
    import socket

    if not worker or ":" not in worker:
        return False
    host, _, pid = worker.rpartition(":")
    if worker == _WORKER_ID:
        return False
    if host != socket.gethostname():
        return True
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


class JobRunner:
    """Single worker thread: one job at a time, one model set resident.

    The queue is in memory but every job it holds is a `queued` row in
    SQLite, so `recover()` can rebuild it after a restart.
    """

    def __init__(self, service: Service):
        self.service = service
        self.settings = service.settings
        self._queue: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: str | None = None
        self._lock = threading.Lock()
        self._pending: list[str] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="yt2wiki-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit(self, job_id: str, force: bool = False) -> None:
        with self._lock:
            if job_id in self._pending or job_id == self.current:
                return
            self._pending.append(job_id)
        # Claim the job so another service instance's recover() leaves it alone.
        self.service.db.execute(
            "UPDATE jobs SET config_json=json_set(config_json, '$.worker', ?) WHERE job_id=?",
            (_WORKER_ID, job_id))
        self.start()
        self._queue.put((job_id, force))

    def live(self) -> list[str]:
        """Job ids this worker owns: the running one first, then the queue."""
        with self._lock:
            return ([self.current] if self.current else []) + list(self._pending)

    def cancel_queued(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._pending:
                return False
            self._pending.remove(job_id)
        now = time.time()
        self.service.db.execute(
            "UPDATE jobs SET state='cancelled', finished_at=?, updated_at=? "
            "WHERE job_id=? AND state='queued'", (now, now, job_id))
        return True

    def recover(self) -> dict[str, int]:
        """Reconcile web-owned jobs after a restart.

        A job that was running when the service stopped is marked
        `interrupted` (retrying reuses its finished stages); queued web jobs
        are re-queued in their original order.  CLI jobs are left alone.
        """
        db = self.service.db
        now = time.time()
        interrupted = 0
        requeued = 0
        for row in db.query("SELECT job_id, state, config_json FROM jobs "
                            "WHERE state IN ('running','queued') ORDER BY created_at"):
            cfg = json.loads(row["config_json"] or "{}")
            if cfg.get("origin") != "web" or row["job_id"] in self.live():
                continue
            if _worker_alive(cfg.get("worker")):
                continue  # owned by another running instance: not ours to touch
            if row["state"] == "running":
                db.execute(
                    "UPDATE jobs SET state='interrupted', error=?, finished_at=?, updated_at=? "
                    "WHERE job_id=?",
                    ("The service stopped while this job was running.", now, now, row["job_id"]))
                db.execute("UPDATE stages SET state='failed', error='interrupted' "
                           "WHERE job_id=? AND state='running'", (row["job_id"],))
                interrupted += 1
            else:
                self.submit(row["job_id"])
                requeued += 1
        return {"interrupted": interrupted, "requeued": requeued}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id, force = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                if job_id not in self._pending:
                    continue  # cancelled while queued
                self._pending.remove(job_id)
                self.current = job_id
            try:
                row = self.service.db.query_one(
                    "SELECT state, config_json FROM jobs WHERE job_id=?", (job_id,))
                if row is None or row["state"] != "queued":
                    continue
                logging.getLogger("yt2wiki.worker").info("running job %s", job_id)
                if json.loads(row["config_json"] or "{}").get("pipeline") == "legacy":
                    self.service.run_job(job_id, force=force)
                else:
                    self._run_v9(job_id)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("yt2wiki.worker").exception("job %s crashed", job_id)
                self.service.db.log("error", f"job crashed: {exc}", job_id=job_id)
                now = time.time()
                self.service.db.execute(
                    "UPDATE jobs SET state='failed', error=?, finished_at=?, updated_at=? "
                    "WHERE job_id=? AND state IN ('queued','running')",
                    (str(exc)[:500], now, now, job_id))
            finally:
                with self._lock:
                    self.current = None
                try:
                    from ..gpu import free_cuda_memory

                    free_cuda_memory()
                except Exception:  # noqa: BLE001
                    pass

    def _run_v9(self, job_id: str) -> dict[str, Any]:
        """The same V9 publisher the CLI runs, adopting this job row."""
        from ..v9 import run as v9run

        job = self.service.get_job(job_id)
        settings = Settings.load(
            data_dir=self.settings.paths.root, profile=job["profile"],
            device_index=self.settings.device_index, registry=self.settings.registry_path)
        service = Service(settings, self.service.db)
        return v9run.run_source(settings, self.service.db, service, job["source_id"],
                                profile=settings.profile, job_id=job_id)

    def status(self) -> dict[str, Any]:
        return {"current": self.current, "queued": list(self._pending)}


def _rewrite_assets(html_text: str, article_id: str) -> str:
    """Point relative asset paths at the server's asset route."""
    import re

    return re.sub(
        r'(<img[^>]+src=")assets/',
        rf'\1/assets/{article_id}/',
        html_text,
    )


def ctx_meta(db: Database, source_id: str) -> dict[str, Any]:
    import json

    row = db.query_one("SELECT meta_json FROM sources WHERE source_id=?", (source_id,))
    return json.loads(row["meta_json"] or "{}") if row else {}
