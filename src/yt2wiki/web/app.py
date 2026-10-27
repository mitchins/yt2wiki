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

import anyio
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import PROFILE_NAMES, ConfigError, Settings, load_registry
from ..db.schema import Database
from ..services import Service, new_id
from . import auth as operator_auth
from . import inference, views

log = logging.getLogger("yt2wiki.web")

_HERE = Path(__file__).resolve().parent

ACTIVE_STATES = ("queued", "running")

# Shared route/error vocabulary (kept next to the routes that raise them).
_ADD_PAGE = "add.html"
_MSG_JOB_NOT_FOUND = "job not found"
_MSG_ARTICLE_NOT_FOUND = "article not found"
_MSG_INVALID_FILENAME = "invalid filename"
_ARTICLE_BY_ID_SQL = "SELECT * FROM articles WHERE article_id=?"
_CAPTION_SQL = "UPDATE article_assets SET caption=? WHERE article_id=? AND frame_id=?"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# Shared OpenAPI error declarations: only statuses a route actually emits
# are declared, merged via _responses() so route-specific docs win.
_AUTH_RESPONSES = {
    401: {"description": "Operator login required (POST /api/auth/login)."},
    403: {"description": "Missing/invalid CSRF token, or operator password not configured."},
}
_D_NOT_READY = {"description": "Inference services not ready."}
_D_UNKNOWN_ARTICLE = {"description": "Unknown article id."}
_D_INVALID_FILENAME = {"description": "Invalid filename."}
_D_UNKNOWN_JOB = {"description": "Unknown job id."}


def _responses(*groups: dict[int, dict[str, str]]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for group in groups:
        out.update(group)
    return out


async def _route__startup(app) -> None:
    app.state.runner.recover()

async def _route__shutdown(app) -> None:
    app.state.runner.stop()

def _route_page(app, request: Request, name: str, ctx: dict[str, Any], status_code: int=200) -> Any:
    session = operator_auth.session_from_request(app.state.settings.paths, request)
    base = {'request': request, 'nav_active_jobs': len(app.state.runner.live()), 'operator': session is not None, 'auth_configured': operator_auth.password_configured(), 'csrf_token': (session or {}).get('csrf', '')}
    return app.state.templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)

def _http_auth_error(exc: operator_auth.AuthError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _route__require_operator(app, request: Request, csrf: bool=True) -> None:
    try:
        operator_auth.require_operator(app.state.settings.paths, request, csrf=csrf)
    except operator_auth.AuthError as exc:
        raise _http_auth_error(exc) from exc

def _route__require_operator_form(app, request: Request, csrf_token: str='') -> None:
    """Form-post variant: the CSRF token arrives as a field, not a header."""
    try:
        session = operator_auth.session_from_request(app.state.settings.paths, request)
        if not operator_auth.password_configured():
            raise operator_auth.AuthError(403, f'Operator password is not configured. Set {operator_auth.PASSWORD_ENV} in the container environment and restart yt2wiki, then log in on the Settings page.')
        if session is None:
            raise operator_auth.AuthError(401, 'Operator login required. Log in on the Settings page.')
        if not operator_auth.csrf_valid(session, request, form_field=csrf_token):
            raise operator_auth.AuthError(403, 'Invalid or missing CSRF token. Reload the page and try again.')
    except operator_auth.AuthError as exc:
        raise _http_auth_error(exc) from exc

def _route_active_jobs(app) -> list[dict[str, Any]]:
    live = app.state.runner.live()
    out = []
    for jid in live:
        job = app.state.service.get_job(jid)
        if job and job['state'] in ACTIVE_STATES:
            out.append(views.job_view(job, live=set(live)))
    return out

def _route_library(app, request: Request, q: str=Query('')) -> Any:
    q = q.strip()[:200]
    cards = views.library_cards(app.state.db, app.state.settings.paths, q=q, fts_query=_fts_query(q) if q else '')
    total = app.state.db.query_one('SELECT COUNT(DISTINCT source_id) c FROM articles')['c']
    return _route_page(app, request, 'library.html', {'nav': 'library', 'q': q, 'cards': cards, 'total': total, 'jobs': _route_active_jobs(app), 'error': request.query_params.get('error', '')})

def _route_add_page(app, request: Request, mode: str=Query('url')) -> Any:
    return _route_page(app, request, _ADD_PAGE, {'nav': 'add', 'mode': mode, 'error': '', 'url': '', 'jobs': _route_active_jobs(app)})

def _route_add_url_form(app, request: Request, url: str=Form(''), back: str=Form(''), csrf_token: str=Form('')) -> Any:
    try:
        _route__require_operator(app, request, csrf=False)
        _route__require_operator_form(app, request, csrf_token)
        job_id = _route_submit_url(app, url, '')
    except HTTPException as exc:
        if back == 'library':
            from urllib.parse import quote
            return RedirectResponse(f'/?error={quote(str(exc.detail))}#add', status_code=303)
        return _route_page(app, request, _ADD_PAGE, {'nav': 'add', 'mode': 'url', 'error': exc.detail, 'url': url, 'jobs': _route_active_jobs(app)}, status_code=exc.status_code)
    return RedirectResponse(f'/jobs/{job_id}', status_code=303)

async def _route_add_upload_form(app, request: Request, file: UploadFile=File(None), csrf_token: str=Form('')) -> Any:
    try:
        _route__require_operator(app, request, csrf=False)
        _route__require_operator_form(app, request, csrf_token)
        job_id = await _route_submit_upload(app, file, '')
    except HTTPException as exc:
        return _route_page(app, request, _ADD_PAGE, {'nav': 'add', 'mode': 'upload', 'error': exc.detail, 'url': '', 'jobs': _route_active_jobs(app)}, status_code=exc.status_code)
    return RedirectResponse(f'/jobs/{job_id}', status_code=303)

def _route_jobs_page(app, request: Request) -> Any:
    live = set(app.state.runner.live())
    jobs = [views.job_view(app.state.service.get_job(j['job_id']), live=live) for j in app.state.service.list_jobs(60)]
    published = {r['job_id']: r['article_id'] for r in app.state.db.query('SELECT job_id, article_id FROM articles WHERE job_id IS NOT NULL')}
    latest: set[str] = set()
    for j in jobs:
        j['article_id'] = j.get('article_id') or published.get(j['job_id'])
        if j['source_id'] in latest:
            j['can_retry'] = False
        latest.add(j['source_id'])
    current = [j for j in jobs if j['state'] == 'running']
    queued = [j for j in jobs if j['state'] == 'queued']
    history = [j for j in jobs if j['state'] not in ('running', 'queued')]
    return _route_page(app, request, 'jobs.html', {'nav': 'jobs', 'current': current, 'queued': queued, 'history': history})

def _route_job_page(app, request: Request, job_id: str, error: str=Query('')) -> Any:
    job = app.state.service.get_job(job_id)
    if job is None:
        raise HTTPException(404, _MSG_JOB_NOT_FOUND)
    view = views.job_view(job, live=set(app.state.runner.live()))
    logs = [dict(r) for r in app.state.db.query('SELECT level, message, ts FROM app_log WHERE job_id=? ORDER BY id DESC LIMIT 40', (job_id,))]
    article = None
    row = app.state.db.query_one('SELECT article_id, title FROM articles WHERE article_id=? OR job_id=? LIMIT 1', (view.get('article_id') or '', job_id))
    if row:
        article = dict(row)
    return _route_page(app, request, 'job.html', {'nav': 'jobs', 'job': view, 'logs': logs, 'article': article, 'error': error, 'source': app.state.service.get_source(job['source_id'])})

def _route_job_retry(app, request: Request, job_id: str, csrf_token: str=Form('')) -> Any:
    _route__require_operator(app, request, csrf=False)
    _route__require_operator_form(app, request, csrf_token)
    from urllib.parse import quote as _quote
    try:
        return RedirectResponse(f'/jobs/{_route_retry_job(app, job_id)}', status_code=303)
    except HTTPException as exc:
        return RedirectResponse(f'/jobs/{job_id}?error={_quote(str(exc.detail))}', status_code=303)

def _route_job_cancel_form(app, request: Request, job_id: str, csrf_token: str=Form('')) -> Any:
    _route__require_operator(app, request, csrf=False)
    _route__require_operator_form(app, request, csrf_token)
    app.state.runner.cancel_queued(job_id)
    return RedirectResponse('/jobs', status_code=303)

def _route_read_article(app, request: Request, article_id: str) -> Any:
    ctx = views.article_context(app.state.db, app.state.settings.paths, article_id)
    if ctx is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    source_id = ctx['article']['source_id']
    frames = app.state.db.query('SELECT f.frame_id, f.t, f.kind, f.reason, p.payload_json FROM frames f\n               LEFT JOIN frame_interpretations p ON p.frame_id=f.frame_id\n               WHERE f.source_id=? ORDER BY f.t', (source_id,))
    findings = app.state.db.query('SELECT * FROM findings WHERE article_id=? ORDER BY severity, created_at', (article_id,))
    evidence = app.state.db.query('SELECT evidence_id, kind, text, t_start, t_end, frame_ids_json FROM evidence WHERE source_id=? ORDER BY t_start', (source_id,))
    meta = json.loads((ctx['source'] or {}).get('meta_json') or '{}')
    frames = [f for f in frames if _frame_file(app.state.settings, app.state.db, source_id, f"{f['frame_id']}.jpg") is not None]
    return _route_page(app, request, 'article.html', {**ctx, 'nav': 'library', 'frames': [_frame_out(f) for f in frames], 'findings': [dict(f) for f in findings], 'evidence': [_evidence_out(e) for e in evidence], 'provenance': meta.get('provenance', {})})

def _route_edit_article(app, request: Request, article_id: str) -> Any:
    row = app.state.db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    article = dict(row)
    assets = [dict(a) for a in app.state.db.query('SELECT a.frame_id, a.caption, f.t AS t FROM article_assets a\n               LEFT JOIN frames f ON f.frame_id=a.frame_id WHERE a.article_id=?', (article_id,))]
    order = _image_order(article['markdown'])
    sections = _section_of_images(article['markdown'])
    assets = [a for a in assets if a['frame_id'] in order]
    assets.sort(key=lambda a: order.index(a['frame_id']))
    for a in assets:
        a['section'] = sections.get(a['frame_id'], '')
    revisions = [dict(r) for r in app.state.db.query('SELECT revision_id, version, edit_note, origin, created_at FROM article_revisions WHERE article_id=? ORDER BY version DESC, created_at DESC LIMIT 50', (article_id,))]
    return _route_page(app, request, 'edit.html', {'nav': 'library', 'article': article, 'assets': assets, 'headings': _h2_headings(article['markdown']), 'revisions': revisions})

def _route_settings_page(app, request: Request) -> Any:
    cfg = inference.load(app.state.settings.paths)
    db_size = sum((p.stat().st_size for p in app.state.settings.paths.db_dir.glob('yt2wiki.sqlite3*') if p.is_file()))
    n_articles = app.state.db.query_one('SELECT COUNT(*) c FROM articles')['c']
    providers = cfg.get('providers') or {}
    roles = cfg.get('roles') or {}
    guided_llm = providers.get(roles.get('writer') or '', {}) or {}
    guided_asr = providers.get(roles.get('asr') or '', {}) or {}
    return _route_page(app, request, 'settings.html', {'nav': 'settings', 'inference': cfg, 'status': inference.load_status(app.state.settings.paths), 'roles': inference.ROLES, 'data_dir': str(app.state.settings.paths.root), 'db_path': str(app.state.settings.paths.db_path), 'db_size': db_size, 'n_articles': n_articles, 'readiness': _route_readiness_summary(app), 'guided_llm': guided_llm, 'guided_asr': guided_asr})

def _route_api_auth_state(app, request: Request) -> Any:
    session = operator_auth.session_from_request(app.state.settings.paths, request)
    return {'password_configured': operator_auth.password_configured(), 'authenticated': session is not None, 'csrf_token': (session or {}).get('csrf', '')}

def _route_api_auth_login(app, request: Request, payload: dict=Body(...)) -> Any:
    if not operator_auth.password_configured():
        raise HTTPException(403, f'Operator password is not configured. Set {operator_auth.PASSWORD_ENV} in the container environment and restart yt2wiki first.')
    password = str((payload or {}).get('password') or '')
    if not operator_auth.verify_password(password):
        raise HTTPException(401, 'Wrong password.')
    token, csrf = operator_auth.create_session(app.state.settings.paths, label=str((payload or {}).get('label') or '')[:80])
    from fastapi.responses import JSONResponse
    resp = JSONResponse({'ok': True, 'csrf_token': csrf})
    secure = operator_auth.want_secure_cookie(request.url.scheme)
    resp.set_cookie(operator_auth.SESSION_COOKIE, token, httponly=True, secure=secure, samesite='lax', path='/', max_age=operator_auth.SESSION_TTL_S)
    return resp

def _route_api_auth_logout(app, request: Request) -> Any:
    operator_auth.revoke_session(app.state.settings.paths, request.cookies.get(operator_auth.SESSION_COOKIE))
    from fastapi.responses import JSONResponse
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(operator_auth.SESSION_COOKIE, path='/')
    return resp

def _route_asset(app, article_id: str, filename: str) -> Any:
    if '/' in filename or '..' in filename or '/' in article_id or ('..' in article_id):
        raise HTTPException(400, _MSG_INVALID_FILENAME)
    path = app.state.settings.paths.article_assets(article_id) / filename
    if not path.is_file() or path.suffix.lower() not in _IMAGE_EXTS:
        raise HTTPException(404, 'asset not found')
    return FileResponse(path, headers={'Cache-Control': 'no-cache'})

def _route_source_frame(app, source_id: str, filename: str) -> Any:
    if '/' in filename or '..' in filename or '..' in source_id or (not filename.lower().endswith(_IMAGE_EXTS)):
        raise HTTPException(400, _MSG_INVALID_FILENAME)
    path = _frame_file(app.state.settings, app.state.db, source_id, filename)
    if path is None:
        raise HTTPException(404, 'frame not found')
    return FileResponse(path, headers={'Cache-Control': 'max-age=86400'})

def _route__profile(app, value: str) -> str:
    profile = (value or app.state.settings.profile).strip()
    if profile not in PROFILE_NAMES:
        raise HTTPException(400, f'Unknown profile “{profile}”.')
    try:
        Settings.load(data_dir=app.state.settings.paths.root, profile=profile)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return profile

def _route__require_ready(app, has_captions: bool=False) -> None:
    """Refuse to queue a job its services cannot run. Never queue an
        inevitably failing job or leave a button apparently doing nothing.
        V9 needs all three capabilities: writing, image judging and
        transcription (or usable captions for the transcription leg)."""
    writing = inference.readiness(app.state.settings.paths, 'writer', 'Writing')
    if writing['state'] != 'Ready':
        raise HTTPException(409, f"Writing is not ready ({writing['detail']}) Connect your multimodal LLM on the Settings page first.")
    images = inference.readiness(app.state.settings.paths, 'vision', 'Image judging')
    if images['state'] != 'Ready':
        raise HTTPException(409, f"Image judging is not ready ({images['detail']}) Connect a multimodal LLM that judges images on the Settings page first.")
    transcription = inference.readiness(app.state.settings.paths, 'asr', 'Transcription')
    if transcription['state'] == 'Ready' or has_captions:
        return
    raise HTTPException(409, f"Transcription is not ready ({transcription['detail']}) Connect a Whisper HTTP server on the Settings page first (a video with captions can be used without one).")

def _route__inference_snapshot(app) -> dict[str, Any]:
    """Capture the effective provider/model identities at submission time.

        The snapshot is stored in the job's config_json so that running jobs
        retain their selected configuration even if settings change later, and
        so that later inspection can prove which provider was actually invoked.
        """
    cfg = inference.load(app.state.settings.paths)
    snap: dict[str, Any] = {}
    for role in ('writer', 'vision', 'asr'):
        name = (cfg.get('roles') or {}).get(role)
        if not name:
            continue
        prov = (cfg.get('providers') or {}).get(name, {})
        status = inference.provider_status(app.state.settings.paths, name)
        snap[role] = {'provider': name, 'url': prov.get('url'), 'model': prov.get('model'), 'api_key_env': prov.get('api_key_env'), 'reachable': bool(status.get('ok')), 'qualified_for_writer': bool(status.get('qualified_for_writer')), 'qualified_for_vision': bool(status.get('qualified_for_vision')), 'qualified_for_asr': bool(status.get('qualified_for_asr')), 'models': status.get('models', [])[:5] if status.get('models') else []}
    return snap

def _route_enqueue(app, source_id: str, profile: str, has_captions: bool=False, **config: Any) -> str:
    for live_id in app.state.runner.live():
        row = app.state.db.query_one('SELECT source_id FROM jobs WHERE job_id=?', (live_id,))
        if row and row['source_id'] == source_id:
            return live_id
    _route__require_ready(app, has_captions=has_captions)
    snap = _route__inference_snapshot(app)
    job = app.state.service.create_job(source_id, profile=profile, config={'pipeline': 'v9', 'origin': 'web', 'inference': snap, 'has_captions': has_captions, **config})
    app.state.runner.submit(job)
    return job

def _route_submit_url(app, url: str, profile: str) -> str:
    url = (url or '').strip()
    if not url:
        raise HTTPException(400, 'Paste a YouTube link to make a guide.')
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, 'That doesn’t look like a web link — it should start with https://')
    profile = _route__profile(app, profile)
    preview = app.state.service.preview_url(url)
    if not preview.get('ok'):
        raise HTTPException(400, preview.get('error', 'That video could not be reached.'))
    sid = app.state.service.create_youtube_source(url)
    return _route_enqueue(app, sid, profile, has_captions=bool(preview.get('has_manual_captions')))

async def _route_submit_upload(app, file: UploadFile | None, profile: str) -> str:
    if file is None or not file.filename:
        raise HTTPException(400, 'Choose a video file to upload.')
    profile = _route__profile(app, profile)
    upload_id = new_id('upl')
    dest = app.state.settings.paths.upload_path(upload_id, Path(file.filename).name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    async with await anyio.open_file(dest, 'wb') as fh:
        while (chunk := (await file.read(1 << 20))):
            size += len(chunk)
            await fh.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, 'That file is empty.')
    try:
        sid = await anyio.to_thread.run_sync(lambda: app.state.service.create_upload_source(dest))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    else:
        try:
            stored = app.state.service.get_source(sid).get('upload_path') if sid else None
        except Exception:
            stored = None
        if stored != str(dest):
            dest.unlink(missing_ok=True)
    return _route_enqueue(app, sid, profile)

def _route__job_captions(job: dict[str, Any]) -> bool:
    try:
        prev_cfg = json.loads(job.get('config_json') or '{}')
    except ValueError:
        return False
    return bool(prev_cfg.get('has_captions'))

def _route_retry_job(app, job_id: str) -> str:
    job = app.state.service.get_job(job_id)
    if job is None:
        raise HTTPException(404, _MSG_JOB_NOT_FOUND)
    return _route_enqueue(app, job['source_id'], job['profile'] if job['profile'] in PROFILE_NAMES else _default_profile(app.state.settings), has_captions=_route__job_captions(job), retry_of=job_id)

def _route_api_add_url(app, request: Request, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    job = _route_submit_url(app, payload.get('url') or '', payload.get('profile') or '')
    return {'source_id': app.state.service.get_job(job)['source_id'], 'job_id': job}

async def _route_api_upload(app, request: Request, file: UploadFile=File(...), profile: str=Form(default='')) -> Any:
    _route__require_operator(app, request)
    job = await _route_submit_upload(app, file, profile)
    return {'source_id': app.state.service.get_job(job)['source_id'], 'job_id': job}

def _route_api_jobs_active(app) -> Any:
    return {'jobs': [_job_json(j) for j in _route_active_jobs(app)]}

def _route_api_job(app, job_id: str) -> Any:
    job = app.state.service.get_job(job_id)
    if job is None:
        raise HTTPException(404, _MSG_JOB_NOT_FOUND)
    stages = {s['stage']: s for s in job['stages']}
    out = {k: v for k, v in job.items() if k != 'stages'}
    out['stages'] = stages
    out['view'] = _job_json(views.job_view(job, live=set(app.state.runner.live())))
    out['log'] = [dict(r) for r in app.state.db.query('SELECT level, message, ts FROM app_log WHERE job_id=? ORDER BY id DESC LIMIT 25', (job_id,))]
    return out

def _route_api_cancel(app, request: Request, job_id: str) -> Any:
    _route__require_operator(app, request)
    return {'cancelled': app.state.runner.cancel_queued(job_id) or app.state.service.request_cancel(job_id)}

def _route_api_reprocess(app, request: Request, job_id: str, payload: dict=Body(default={})) -> Any:
    _route__require_operator(app, request)
    job = app.state.service.get_job(job_id)
    if job is None:
        raise HTTPException(404, _MSG_JOB_NOT_FOUND)
    profile = _route__profile(app, payload.get('profile') or job['profile'])
    if payload.get('pipeline') == 'legacy':
        force = bool(payload.get('force'))
        new_job = app.state.service.create_job(job['source_id'], profile=profile, model_overrides=payload.get('model_overrides') or {}, config={'force': force, 'pipeline': 'legacy', 'origin': 'web'})
        app.state.runner.submit(new_job, force=force)
    else:
        new_job = _route_enqueue(app, job['source_id'], profile, has_captions=_route__job_captions(job), retry_of=job_id)
    return {'job_id': new_job, 'source_id': job['source_id']}

def _route_readiness_summary(app) -> dict[str, Any]:
    return {role: inference.readiness(app.state.settings.paths, role, label) for role, label in inference.ROLES}

def _route_api_readiness(app) -> Any:
    return _route_readiness_summary(app)

def _route_api_save_inference(app, request: Request, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    try:
        cfg = inference.save(app.state.settings.paths, payload)
    except inference.InferenceConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'ok': True, 'config': cfg}

def _route_api_test_inference(app, request: Request, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    url = str(payload.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, 'Enter an http:// or https:// base URL first.')
    try:
        api_key_env = inference.validate_api_key_env(payload.get('api_key_env'))
    except inference.InferenceConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    result = inference.test_endpoint(url, api_key_env, model=str(payload.get('model') or '').strip())
    name = str(payload.get('name') or '').strip().lower()
    if name:
        result = dict(result)
        result['model'] = str(payload.get('model') or '').strip()
        result['api_key_env'] = api_key_env
        inference.record_test(app.state.settings.paths, name, result)
    return result

def _route_api_save_test_inference(app, request: Request, payload: dict=Body(...)) -> Any:
    """Guided setup: save one endpoint, test the actual server, assign
        roles whose qualification passed. The sequence is explicit and
        reliable: an endpoint is only marked usable after its own test.
        """
    _route__require_operator(app, request)
    try:
        saved = inference.save_one(app.state.settings.paths, payload)
    except inference.InferenceConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    result = inference.test_endpoint(saved['url'], saved['api_key_env'], model=saved['model'])
    try:
        assigned = inference.record_qualified(app.state.settings.paths, saved['name'], result, tested=saved)
    except inference.StaleIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'ok': bool(result.get('ok')), 'provider': saved['name'], 'result': result, 'roles': assigned}

def _route_api_preview(app, payload: dict=Body(...)) -> Any:
    article_id = str(payload.get('article_id') or '')
    md = payload.get('markdown')
    if not isinstance(md, str):
        raise HTTPException(400, 'markdown is required')
    if app.state.db.query_one('SELECT 1 FROM articles WHERE article_id=?', (article_id,)) is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    view = views.render_reader(app.state.settings.paths, article_id, md)
    return {'title': view['title'], 'lead_html': view['lead_html'], 'body_html': view['body_html']}

def _route_api_image_move(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    fid = (payload.get('frame_id') or '').strip()
    heading = (payload.get('section') or '').strip()
    row = app.state.db.query_one('SELECT markdown FROM articles WHERE article_id=?', (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    try:
        md = _move_image(row['markdown'], fid, heading)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    version = _save_article_markdown(app.state.settings, app.state.db, article_id, md, f'image moved to “{heading}” by user')
    return {'ok': True, 'version': version}

def _route_api_export_md(app, article_id: str) -> Any:
    path = app.state.settings.paths.article_markdown(article_id)
    if not path.is_file():
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    return FileResponse(path, media_type='text/markdown', filename=f'{article_id}.md')

def _route_api_export_zip(app, article_id: str) -> Any:
    path = app.state.settings.paths.article_dir(article_id) / 'article.zip'
    if not path.is_file():
        raise HTTPException(404, 'bundle not found')
    return FileResponse(path, media_type='application/zip', filename=f'{article_id}.zip')

def _route_api_export_standalone(app, article_id: str) -> Any:
    path = app.state.settings.paths.article_dir(article_id) / 'article.standalone.html'
    if not path.is_file():
        raise HTTPException(404, 'standalone not found')
    return FileResponse(path, media_type='text/html', filename=f'{article_id}.html')

def _route_api_edit_article(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    markdown = payload.get('markdown')
    note = (payload.get('note') or '').strip()
    if not isinstance(markdown, str) or len(markdown) < 20:
        raise HTTPException(400, 'markdown is required')
    row = app.state.db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    version = _save_article_markdown(app.state.settings, app.state.db, article_id, markdown, note or 'edited by user')
    return {'ok': True, 'version': version, 'note': note}

def _route_api_revisions(app, article_id: str) -> Any:
    rows = app.state.db.query('SELECT * FROM article_revisions WHERE article_id=? ORDER BY version DESC LIMIT 50', (article_id,))
    return [dict(r) for r in rows]


def _caption_from_markdown(markdown: str, fid: str) -> str:
    import re as _re2
    lines = markdown.split('\n')
    for i, ln in enumerate(lines):
        if f'assets/{fid}.jpg' in ln:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith('*'):
                cap = lines[j].strip().strip('*').strip()
                return _re2.sub(r'\s+—\s+source at \[.*$', '', cap).strip()
    return ''


def _restore_missing_asset(app, article_id: str, assets, fid: str, restored_md: str) -> None:
    """Restore one image file + asset row from frames when revert needs it."""
    from pathlib import Path as _Path

    dest = assets / f'{fid}.jpg'
    if dest.is_file():
        return
    frow = app.state.db.query_one('SELECT path FROM frames WHERE frame_id=?', (fid,))
    if not (frow and frow['path'] and _Path(frow['path']).is_file()):
        return
    try:
        shutil.copy2(frow['path'], dest)
    except OSError:
        return
    cap = _caption_from_markdown(restored_md, fid)
    with app.state.db.transaction() as conn:
        conn.execute('INSERT OR REPLACE INTO article_assets(asset_id, article_id, frame_id, path,\n                                                                 caption, alt_text, created_at)\n                           VALUES(?,?,?,?,?,?,?)', (f'{article_id}-asset-{fid}', article_id, fid, str(dest), cap, '', time.time()))


def _sync_restored_captions(app, article_id: str, assets, restored_md: str, existing: set[str]) -> None:
    """Refresh captions for restored markdown without changing installed files."""
    for fid in _image_order(restored_md):
        cap = _caption_from_markdown(restored_md, fid)
        if fid in existing:
            app.state.db.execute(_CAPTION_SQL, (cap, article_id, fid))
        elif (assets / f'{fid}.jpg').is_file():
            app.state.db.execute('INSERT OR REPLACE INTO article_assets(asset_id, article_id, frame_id, path,\n                                                             caption, alt_text, created_at)\n                       VALUES(?,?,?,?,?,?,?)', (f'{article_id}-asset-{fid}', article_id, fid, str(assets / f'{fid}.jpg'), cap, '', time.time()))


def _route_api_revert(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    revision_id = payload.get('revision_id')
    row = app.state.db.query_one('SELECT * FROM article_revisions WHERE revision_id=? AND article_id=?', (revision_id, article_id))
    if row is None:
        raise HTTPException(404, 'revision not found')
    restored_md = row['markdown']
    assets = app.state.settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)

    # caption lookup is module-level
    for fid in _image_order(restored_md):
        _restore_missing_asset(app, article_id, assets, fid, restored_md)
    existing = {r['frame_id'] for r in app.state.db.query('SELECT frame_id FROM article_assets WHERE article_id=?', (article_id,))}
    _sync_restored_captions(app, article_id, assets, restored_md, existing)
    version = _save_article_markdown(app.state.settings, app.state.db, article_id, restored_md, f"restored version {row['version']}")
    return {'ok': True, 'version': version}

def _route_api_revision(app, article_id: str, revision_id: str) -> Any:
    row = app.state.db.query_one('SELECT revision_id, version, markdown, edit_note, origin, created_at FROM article_revisions WHERE revision_id=? AND article_id=?', (revision_id, article_id))
    if row is None:
        raise HTTPException(404, 'revision not found')
    return dict(row)

def _route_api_v9_sections(app, article_id: str) -> Any:
    row = app.state.db.query_one('SELECT outline_json FROM articles WHERE article_id=?', (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    import json as _json
    try:
        secs = _json.loads(row['outline_json'] or '[]')
    except Exception:
        secs = []
    return [{'section_id': s.get('section_id'), 'heading': s.get('heading'), 't_start': s.get('t_start'), 't_end': s.get('t_end')} for s in secs]

def _readable_frames(app, source_id: str, rows, limit: int):
    rows = [r for r in rows if _frame_file(app.state.settings, app.state.db, source_id, f"{r['frame_id']}.jpg")]
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


def _route_api_v9_alternatives(app, article_id: str, section_id: str=Query(''), limit: int=Query(8)) -> Any:
    row = app.state.db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    import json as _json
    secs = []
    try:
        secs = _json.loads(row['outline_json'] or '[]')
    except Exception:
        secs = []
    sec = next((s for s in secs if s.get('section_id') == section_id), secs[0] if secs else None)
    if sec is None and (not section_id):
        rows = app.state.db.query('SELECT frame_id, t, kind FROM frames WHERE source_id=? ORDER BY t', (row['source_id'],))
        rows = _readable_frames(app, row['source_id'], rows, limit)
        return {'section_id': '', 'window': None, 'frames': [dict(r) for r in rows]}
    if sec is None:
        raise HTTPException(404, 'section not found')
    lo, hi = (float(sec.get('t_start', 0)) - 20.0, float(sec.get('t_end', 0)) + 20.0)
    rows = app.state.db.query('SELECT f.frame_id, f.t, f.kind FROM frames f\n               WHERE f.source_id=? AND f.t BETWEEN ? AND ? ORDER BY f.t', (row['source_id'], lo, hi))
    rows = _readable_frames(app, row['source_id'], rows, limit)
    return {'section_id': sec.get('section_id'), 'window': [lo, hi], 'frames': [dict(r) for r in rows]}

def _route_api_image_replace(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    old = (payload.get('old_frame_id') or '').strip()
    new = (payload.get('new_frame_id') or '').strip()
    caption = payload.get('caption')
    if not old or not new:
        raise HTTPException(400, 'old_frame_id and new_frame_id required')
    return _swap_article_image(app.state.settings, app.state.db, article_id, old, new, caption=caption, note='image replaced by user')

def _route_api_image_remove(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    old = (payload.get('frame_id') or '').strip()
    if not old:
        raise HTTPException(400, 'frame_id required')
    return _swap_article_image(app.state.settings, app.state.db, article_id, old, None, note='image removed by user')

def _route_api_image_caption(app, request: Request, article_id: str, payload: dict=Body(...)) -> Any:
    _route__require_operator(app, request)
    fid = (payload.get('frame_id') or '').strip()
    caption = (payload.get('caption') or '').strip()
    if not fid:
        raise HTTPException(400, 'frame_id required')
    row = app.state.db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
    if row is None:
        raise HTTPException(404, _MSG_ARTICLE_NOT_FOUND)
    md = row['markdown']
    md = _set_caption(md, fid, caption)
    import re as _re2
    md = _re2.sub(f'!\\[[^\\]]*\\]\\(assets/{_re2.escape(fid)}\\.jpg\\)', f'![{caption}](assets/{fid}.jpg)' if caption else f'![Source frame](assets/{fid}.jpg)', md)
    _save_article_markdown(app.state.settings, app.state.db, article_id, md, 'caption edited by user')
    with app.state.db.transaction() as conn:
        conn.execute(_CAPTION_SQL, (caption, article_id, fid))
    return {'ok': True}

def _route_api_search(app, q: str=Query(''), scope: str=Query('articles')) -> Any:
    q = (q or '').strip()
    if not q:
        return {'results': []}
    terms = _fts_query(q)
    if scope == 'evidence':
        sql = 'SELECT e.evidence_id, e.kind, e.text, e.t_start, s.title FROM evidence_fts f JOIN evidence e ON e.evidence_id=f.evidence_id JOIN sources s ON s.source_id=e.source_id WHERE evidence_fts MATCH ? ORDER BY rank LIMIT 50'
    else:
        sql = 'SELECT a.article_id, a.title, a.updated_at, s.uploader FROM articles_fts f JOIN articles a ON a.article_id=f.article_id JOIN sources s ON s.source_id=a.source_id WHERE articles_fts MATCH ? ORDER BY rank LIMIT 50'
    rows = app.state.db.query(sql, (terms,))
    return {'results': [dict(r) for r in rows], 'query': terms}

def _route_api_registry(app) -> Any:
    reg = load_registry(app.state.settings.registry_path)
    models = {}
    for key, spec in reg['models'].items():
        models[key] = {'role': spec.role, 'repo': spec.repo, 'license': spec.license, 'params_b': spec.params_b, 'provider': spec.provider, 'notes': spec.notes, 'vram_gb': spec.vram_gb, 'fallbacks': spec.fallbacks, 'variants': spec.variants}
    return {'profiles': reg['profiles'], 'assignments': reg['assignments'], 'models': models, 'current_profile': app.state.settings.profile, 'fingerprint': app.state.settings.fingerprint()}

def _route_api_gpu(app) -> Any:
    from ..gpu import cuda_available, nvidia_smi_gpus
    return {'cuda': cuda_available(), 'gpus': [g.__dict__ for g in nvidia_smi_gpus()], 'vram_budget_gb': app.state.settings.vram_budget_gb()}

def _route_api_evidence(app, source_id: str) -> Any:
    rows = app.state.db.query('SELECT * FROM evidence WHERE source_id=? ORDER BY t_start', (source_id,))
    return [_evidence_out(r) for r in rows]

def _route_api_transcript(app, source_id: str) -> Any:
    rows = app.state.db.query('SELECT idx, start, end, text FROM transcript_segments WHERE source_id=? ORDER BY start', (source_id,))
    return [dict(r) for r in rows]

def _route_api_frames(app, source_id: str) -> Any:
    rows = app.state.db.query('SELECT f.frame_id, f.t, f.kind, f.reason, p.payload_json FROM frames f\n               LEFT JOIN frame_interpretations p ON p.frame_id=f.frame_id\n               WHERE f.source_id=? ORDER BY f.t', (source_id,))
    return [_frame_out(r) for r in rows]

def _route_api_reprocess_source(app, request: Request, source_id: str, payload: dict=Body(default={})) -> Any:
    _route__require_operator(app, request)
    if app.state.service.get_source(source_id) is None:
        raise HTTPException(404, 'source not found')
    profile = _route__profile(app, payload.get('profile') or '')
    if payload.get('pipeline') == 'legacy':
        force = bool(payload.get('force'))
        job = app.state.service.create_job(source_id, profile=profile, model_overrides=payload.get('model_overrides') or {}, config={'force': force, 'pipeline': 'legacy', 'origin': 'web'})
        app.state.runner.submit(job, force=force)
        return {'job_id': job}
    return {'job_id': _route_enqueue(app, source_id, profile)}

def _route_api_articles_list(app) -> Any:
    rows = app.state.db.query('SELECT a.article_id, a.title, a.status, a.updated_at, s.uploader\n               FROM articles a JOIN sources s ON s.source_id=a.source_id\n               ORDER BY a.updated_at DESC LIMIT 200')
    return [dict(r) for r in rows]

def _route_media(app, source_id: str, filename: str) -> Any:
    if '/' in filename or '..' in filename:
        raise HTTPException(400, _MSG_INVALID_FILENAME)
    row = app.state.db.query_one('SELECT source_id FROM sources WHERE source_id=?', (source_id,))
    if row is None:
        raise HTTPException(404, 'unknown source')
    path = app.state.settings.paths.source_dir(source_id) / filename
    if not path.is_file() or path.suffix.lower() not in ('.mp4', '.webm', '.mkv'):
        raise HTTPException(404, 'media not found')
    return FileResponse(path)

def _route_health(app) -> Any:
    return {'ok': True, 'profile': app.state.settings.profile, 'data': str(app.state.settings.paths.root)}


def create_app(settings: Settings | None=None) -> FastAPI:
    settings = settings or Settings.load()
    import os as _os
    _os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
    _os.environ.setdefault('TQDM_DISABLE', '1')
    _os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'error')
    db = Database(settings.paths.db_path)
    db.migrate()
    service = Service(settings, db)
    templates = Jinja2Templates(directory=str(_HERE / 'templates'))
    templates.env.filters['ts'] = lambda v: format_ts_filter(v)
    templates.env.filters['date'] = _fmt_date
    templates.env.filters['ago'] = _fmt_ago
    templates.env.filters['duration'] = views.fmt_duration
    app = FastAPI(title='yt2wiki', version='0.2.0', docs_url='/api/docs')
    app.state.settings = settings
    app.state.db = db
    app.state.service = service
    app.state.runner = JobRunner(service)
    app.state.templates = templates
    app.mount('/static', StaticFiles(directory=str(_HERE / 'static')), name='static')

    @app.on_event('startup')
    async def _startup() -> None:
        return await _route__startup(app)

    @app.on_event('shutdown')
    async def _shutdown() -> None:
        return await _route__shutdown(app)

    @app.get('/', response_class=HTMLResponse)
    @app.get('/library', response_class=HTMLResponse)
    def library(request: Request, q: str=Query('')) -> Any:
        return _route_library(app, request, q)

    @app.get('/add', response_class=HTMLResponse)
    def add_page(request: Request, mode: str=Query('url')) -> Any:
        return _route_add_page(app, request, mode)

    @app.post('/add/url', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Missing/invalid URL, unknown profile or unreachable video.'}, 409: _D_NOT_READY}))
    def add_url_form(request: Request, url: str=Form(''), back: str=Form(''), csrf_token: str=Form('')) -> Any:
        return _route_add_url_form(app, request, url, back, csrf_token)

    @app.post('/add/upload', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Missing/empty file or unknown profile.'}, 409: _D_NOT_READY}))
    async def add_upload_form(request: Request, file: UploadFile=File(None), csrf_token: str=Form('')) -> Any:
        return await _route_add_upload_form(app, request, file, csrf_token)

    @app.get('/jobs', response_class=HTMLResponse)
    def jobs_page(request: Request) -> Any:
        return _route_jobs_page(app, request)

    @app.get('/jobs/{job_id}', response_class=HTMLResponse, responses={404: _D_UNKNOWN_JOB})
    def job_page(request: Request, job_id: str, error: str=Query('')) -> Any:
        return _route_job_page(app, request, job_id, error)

    @app.post('/jobs/{job_id}/retry', responses=_AUTH_RESPONSES)
    def job_retry(request: Request, job_id: str, csrf_token: str=Form('')) -> Any:
        return _route_job_retry(app, request, job_id, csrf_token)

    @app.post('/jobs/{job_id}/cancel', responses=_AUTH_RESPONSES)
    def job_cancel_form(request: Request, job_id: str, csrf_token: str=Form('')) -> Any:
        return _route_job_cancel_form(app, request, job_id, csrf_token)

    @app.get('/articles/{article_id}', response_class=HTMLResponse, responses={404: _D_UNKNOWN_ARTICLE})
    def read_article(request: Request, article_id: str) -> Any:
        return _route_read_article(app, request, article_id)

    @app.get('/articles/{article_id}/edit', response_class=HTMLResponse, responses={404: _D_UNKNOWN_ARTICLE})
    def edit_article(request: Request, article_id: str) -> Any:
        return _route_edit_article(app, request, article_id)

    @app.get('/settings', response_class=HTMLResponse)
    def settings_page(request: Request) -> Any:
        return _route_settings_page(app, request)

    @app.get('/api/auth/state')
    def api_auth_state(request: Request) -> Any:
        return _route_api_auth_state(app, request)

    @app.post('/api/auth/login', responses={403: {'description': 'Operator password is not configured.'}, 401: {'description': 'Wrong password.'}})
    def api_auth_login(request: Request, payload: dict=Body(...)) -> Any:
        return _route_api_auth_login(app, request, payload)

    @app.post('/api/auth/logout')
    def api_auth_logout(request: Request) -> Any:
        return _route_api_auth_logout(app, request)

    @app.get('/assets/{article_id}/{filename}', responses={400: _D_INVALID_FILENAME, 404: {'description': 'Asset missing or not an image.'}})
    def asset(article_id: str, filename: str) -> Any:
        return _route_asset(app, article_id, filename)

    @app.get('/frames/{source_id}/{filename}', responses={400: _D_INVALID_FILENAME, 404: {'description': 'Frame not found.'}})
    def source_frame(source_id: str, filename: str) -> Any:
        return _route_source_frame(app, source_id, filename)

    @app.post('/api/sources/url', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Missing/invalid URL, unknown profile or unreachable video.'}, 409: _D_NOT_READY}))
    def api_add_url(request: Request, payload: dict=Body(...)) -> Any:
        return _route_api_add_url(app, request, payload)

    @app.post('/api/sources/upload', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Missing/empty file or unknown profile.'}, 409: _D_NOT_READY}))
    async def api_upload(request: Request, file: UploadFile=File(...), profile: str=Form(default='')) -> Any:
        return await _route_api_upload(app, request, file, profile)

    @app.get('/api/jobs/active')
    def api_jobs_active() -> Any:
        return _route_api_jobs_active(app)

    @app.get('/api/jobs/{job_id}', responses={404: _D_UNKNOWN_JOB})
    def api_job(job_id: str) -> Any:
        return _route_api_job(app, job_id)

    @app.post('/api/jobs/{job_id}/cancel', responses=_AUTH_RESPONSES)
    def api_cancel(request: Request, job_id: str) -> Any:
        return _route_api_cancel(app, request, job_id)

    @app.post('/api/jobs/{job_id}/reprocess', responses=_responses(_AUTH_RESPONSES, {404: _D_UNKNOWN_JOB, 400: {'description': 'Unknown profile.'}, 409: _D_NOT_READY}))
    def api_reprocess(request: Request, job_id: str, payload: dict=Body(default={})) -> Any:
        return _route_api_reprocess(app, request, job_id, payload)

    @app.get('/api/settings/readiness')
    def api_readiness() -> Any:
        return _route_api_readiness(app)

    @app.post('/api/settings/inference', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Invalid inference configuration.'}}))
    def api_save_inference(request: Request, payload: dict=Body(...)) -> Any:
        return _route_api_save_inference(app, request, payload)

    @app.post('/api/settings/inference/test', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Missing/invalid base URL or API-key variable.'}}))
    def api_test_inference(request: Request, payload: dict=Body(...)) -> Any:
        return _route_api_test_inference(app, request, payload)

    @app.post('/api/settings/inference/save-test', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Invalid inference configuration.'}, 409: {'description': 'Stale provider identity: retest before assigning roles.'}}))
    def api_save_test_inference(request: Request, payload: dict=Body(...)) -> Any:
        return _route_api_save_test_inference(app, request, payload)

    @app.post('/api/preview', responses={400: {'description': 'markdown is required.'}, 404: _D_UNKNOWN_ARTICLE})
    def api_preview(payload: dict=Body(...)) -> Any:
        return _route_api_preview(app, payload)

    @app.post('/api/articles/{article_id}/images/move', responses=_responses(_AUTH_RESPONSES, {404: _D_UNKNOWN_ARTICLE, 400: {'description': 'Move rejected: image missing or invalid target section.'}}))
    def api_image_move(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_image_move(app, request, article_id, payload)

    @app.get('/api/articles/{article_id}/export.md', responses={404: {'description': 'Article markdown not found.'}})
    def api_export_md(article_id: str) -> Any:
        return _route_api_export_md(app, article_id)

    @app.get('/api/articles/{article_id}/export.zip', responses={404: {'description': 'Export bundle not found.'}})
    def api_export_zip(article_id: str) -> Any:
        return _route_api_export_zip(app, article_id)

    @app.get('/api/articles/{article_id}/standalone', responses={404: {'description': 'Standalone HTML not found.'}})
    def api_export_standalone(article_id: str) -> Any:
        return _route_api_export_standalone(app, article_id)

    @app.post('/api/articles/{article_id}', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'markdown is required (min length 20).'}, 404: _D_UNKNOWN_ARTICLE}))
    def api_edit_article(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_edit_article(app, request, article_id, payload)

    @app.get('/api/articles/{article_id}/revisions')
    def api_revisions(article_id: str) -> Any:
        return _route_api_revisions(app, article_id)

    @app.post('/api/articles/{article_id}/revert', responses=_responses(_AUTH_RESPONSES, {404: {'description': 'Unknown revision for this article.'}}))
    def api_revert(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_revert(app, request, article_id, payload)

    @app.get('/api/articles/{article_id}/revisions/{revision_id}', responses={404: {'description': 'Unknown revision for this article.'}})
    def api_revision(article_id: str, revision_id: str) -> Any:
        return _route_api_revision(app, article_id, revision_id)

    @app.get('/api/v9/articles/{article_id}/sections', responses={404: _D_UNKNOWN_ARTICLE})
    def api_v9_sections(article_id: str) -> Any:
        return _route_api_v9_sections(app, article_id)

    @app.get('/api/v9/articles/{article_id}/alternatives', responses={404: {'description': 'Unknown article id or section id.'}})
    def api_v9_alternatives(article_id: str, section_id: str=Query(''), limit: int=Query(8)) -> Any:
        return _route_api_v9_alternatives(app, article_id, section_id, limit)

    @app.post('/api/articles/{article_id}/images/replace', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'Invalid frame identifiers or image not in article.'}, 404: {'description': 'Unknown article or replacement frame.'}}))
    def api_image_replace(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_image_replace(app, request, article_id, payload)

    @app.post('/api/articles/{article_id}/images/remove', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'frame_id required or invalid.'}, 404: _D_UNKNOWN_ARTICLE}))
    def api_image_remove(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_image_remove(app, request, article_id, payload)

    @app.post('/api/articles/{article_id}/images/caption', responses=_responses(_AUTH_RESPONSES, {400: {'description': 'frame_id required.'}, 404: _D_UNKNOWN_ARTICLE}))
    def api_image_caption(request: Request, article_id: str, payload: dict=Body(...)) -> Any:
        return _route_api_image_caption(app, request, article_id, payload)

    @app.get('/api/search')
    def api_search(q: str=Query(''), scope: str=Query('articles')) -> Any:
        return _route_api_search(app, q, scope)

    @app.get('/api/registry')
    def api_registry() -> Any:
        return _route_api_registry(app)

    @app.get('/api/gpu')
    def api_gpu() -> Any:
        return _route_api_gpu(app)

    @app.get('/api/sources/{source_id}/evidence')
    def api_evidence(source_id: str) -> Any:
        return _route_api_evidence(app, source_id)

    @app.get('/api/sources/{source_id}/transcript')
    def api_transcript(source_id: str) -> Any:
        return _route_api_transcript(app, source_id)

    @app.get('/api/sources/{source_id}/frames')
    def api_frames(source_id: str) -> Any:
        return _route_api_frames(app, source_id)

    @app.post('/api/sources/{source_id}/reprocess', responses=_responses(_AUTH_RESPONSES, {404: {'description': 'Unknown source id.'}, 400: {'description': 'Unknown profile.'}, 409: _D_NOT_READY}))
    def api_reprocess_source(request: Request, source_id: str, payload: dict=Body(default={})) -> Any:
        return _route_api_reprocess_source(app, request, source_id, payload)

    @app.get('/api/articles_list')
    def api_articles_list() -> Any:
        return _route_api_articles_list(app)

    @app.get('/media/{source_id}/{filename}', responses={400: _D_INVALID_FILENAME, 404: {'description': 'Unknown source or media file missing.'}})
    def media(source_id: str, filename: str) -> Any:
        return _route_media(app, source_id, filename)

    @app.get('/health')
    def health() -> Any:
        return _route_health(app)
    return app

class _RunnerHolder:
    instance: JobRunner | None = None


def _save_article_markdown(settings, db, article_id: str, markdown: str, note: str) -> int:
    from ..article import Article, markdown_to_html, render_html
    from ..reader import split_markdown
    from ..services import new_id

    row = db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
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
        for fid in _image_order(markdown):
            for ext in _IMAGE_EXTS:
                p = assets / f"{fid}{ext}"
                if p.is_file():
                    zf.write(p, f"assets/{p.name}")
                    break
    (settings.paths.article_dir(article_id) / "article.standalone.html").write_text(
        render_html(article, markdown_to_html(markdown), embed_images=True, asset_dir=assets),
        encoding="utf-8")
    return int(row["version"]) + 1


def _caption_slot(lines: list[str], i: int, frame_id: str, caption: str) -> tuple[int, list[str]] | None:
    """Consume the blank + caption lines after an image, returning cursor + replacement.

    Returns None when this image line has no rewritable caption slot
    (no match, or no trailing blank line), preserving the caller loop shape.
    """
    import re

    if not re.match(rf"!\[.*\]\(assets/{re.escape(frame_id)}\.jpg\)$", lines[i].strip()):
        return None
    if not (i + 1 < len(lines) and lines[i + 1].strip() == ""):
        return None
    j = i + 2
    if j < len(lines) and lines[j].strip().startswith("*"):
        j += 1  # drop the superseded caption line
    capline = f"*{caption}*" if caption else "*Source frame*"
    return j, ["", capline]


def _set_caption(markdown: str, frame_id: str, caption: str) -> str:
    lines = markdown.split("\n")
    out = []
    i = 0
    changed = False
    while i < len(lines):
        out.append(lines[i])
        slot = _caption_slot(lines, i, frame_id, caption)
        if slot is not None:
            i, extra = slot
            out.extend(extra)
            changed = True
            continue
        i += 1
    return "\n".join(out) if changed else markdown


def _install_replacement(db, article_id: str, old_fid: str, new_fid: str,
                         caption: str | None, assets, md: str) -> str:
    import shutil
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
            (f"{article_id}-asset-{new_fid}", article_id, new_fid, str(assets / f"{new_fid}.jpg"),
             caption or "", "", time.time()))
    return md


def _drop_image_markdown(md: str, old_fid: str) -> str:
    raw = md.split("\n")
    lines = []
    skip_next_caption = False
    for line in raw:
        if f"assets/{old_fid}.jpg" in line:
            skip_next_caption = True
            continue
        if skip_next_caption:
            if not line.strip():
                continue
            skip_next_caption = False
            if line.strip().startswith("*"):
                continue
        lines.append(line)
    return "\n".join(lines)


def _check_swap_ids(old_fid: str | None, new_fid: str | None) -> None:
    import re as _re

    from fastapi import HTTPException as _HE

    if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", old_fid or ""):
        raise _HE(400, "Invalid frame identifier.")
    if new_fid and not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", new_fid):
        raise _HE(400, "Invalid replacement frame identifier.")


def _ensure_inside(assets, old_fid: str, new_fid: str | None) -> None:
    try:
        (assets / f"{old_fid}.jpg").resolve().relative_to(assets.resolve())
        if new_fid:
            (assets / f"{new_fid}.jpg").resolve().relative_to(assets.resolve())
    except ValueError as exc:
        from fastapi import HTTPException as _HE

        raise _HE(400, "Invalid frame identifier.") from exc


def _apply_new_caption(md: str, new_fid: str, caption: str | None) -> str:
    import re as _re2

    md = _set_caption(md, new_fid, caption if caption is not None else "")
    return _re2.sub(
        rf"!\[[^\]]*\]\(assets/{_re2.escape(new_fid)}\.jpg\)",
        f"![{caption}](assets/{new_fid}.jpg)" if caption else f"![Source frame](assets/{new_fid}.jpg)",
        md)


def _swap_article_image(settings, db, article_id: str, old_fid: str,
                        new_fid: str | None, caption: str | None = None,
                        note: str = "image edited by user") -> dict[str, Any]:
    row = db.query_one(_ARTICLE_BY_ID_SQL, (article_id,))
    if row is None:
        from fastapi import HTTPException as _HE
        raise _HE(404, _MSG_ARTICLE_NOT_FOUND)
    md = row["markdown"]
    _check_swap_ids(old_fid, new_fid)
    if old_fid not in _image_order(md):
        from fastapi import HTTPException as _HE
        raise _HE(400, "That image is not in the article.")
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    _ensure_inside(assets, old_fid, new_fid)
    if new_fid:
        md = _install_replacement(db, article_id, old_fid, new_fid, caption, assets, md)
    else:
        md = _drop_image_markdown(md, old_fid)
        with db.transaction() as conn:
            conn.execute("DELETE FROM article_assets WHERE article_id=? AND frame_id=?",
                         (article_id, old_fid))
    if new_fid:
        md = _apply_new_caption(md, new_fid, caption)
        db.execute(_CAPTION_SQL,
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
    name = settings.profile
    return name if name in PROFILE_NAMES else PROFILE_NAMES[0]


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


def _locate_image_block(lines: list[str], frame_id: str) -> tuple[int, int]:
    """Locate an image line and its trailing caption, returning (at, end)."""
    import re

    pat = re.compile(r"^!\[[^\]]*\]\(assets/" + re.escape(frame_id) + r"\.jpg\)\s*$")
    at = next((i for i, ln in enumerate(lines) if pat.match(ln.strip())), None)
    if at is None:
        raise ValueError("That image is not in the article.")
    end = at + 1
    k = end
    while k < len(lines) and not lines[k].strip():
        k += 1
    if k < len(lines) and re.match(r"^\*.+\*$", lines[k].strip()):
        end = k + 1
    return at, end


def _section_insert_range(rest: list[str], heading: str) -> tuple[int, int]:
    start = next(i for i, ln in enumerate(rest) if ln.startswith("## ") and ln[3:].strip() == heading)
    stop = next((i for i in range(start + 1, len(rest)) if rest[i].startswith("## ")), len(rest))
    while stop > start + 1 and not rest[stop - 1].strip():
        stop -= 1
    return start, stop


def _move_image(markdown: str, frame_id: str, heading: str) -> str:
    """Move an image (and its caption line) to the end of another section."""
    import re

    lines = markdown.split("\n")
    at, end = _locate_image_block(lines, frame_id)
    if heading not in _h2_headings(markdown):
        raise ValueError("Unknown section.")
    block = [ln for ln in lines[at:end] if ln.strip()]
    rest = lines[:at] + lines[end:]
    start, stop = _section_insert_range(rest, heading)
    insert = [""] + [x for pair in zip(block, [""] * len(block)) for x in pair]
    out = rest[:stop] + insert + rest[stop:]
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def _frame_out(row: Any) -> dict[str, Any]:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}  # S2583: remove redundant fallback
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
        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue()
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
                self._execute_queued(job_id, force)
            finally:
                with self._lock:
                    self.current = None
                try:
                    from ..gpu import free_cuda_memory

                    free_cuda_memory()
                except Exception:  # noqa: BLE001
                    pass

    def _execute_queued(self, job_id: str, force: bool) -> None:
        try:
            row = self.service.db.query_one(
                "SELECT state, config_json FROM jobs WHERE job_id=?", (job_id,))
            if row is None or row["state"] != "queued":
                return
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
