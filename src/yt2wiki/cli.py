"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import PROFILE_NAMES, ConfigError, Settings
from .db.schema import Database
from .paths import Paths
from .services import Service
from .web import inference as inf


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))
    try:
        if args.command == "info":
            return _cmd_info(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "add":
            return _cmd_add(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "jobs":
            return _cmd_jobs(args)
        if args.command == "models":
            return _cmd_models(args)
        if args.command == "export":
            return _cmd_export(args)
        if args.command == "schemas":
            return _cmd_schemas(args)
        if args.command == "inference":
            return _cmd_inference(args)
        if args.command == "factcheck":
            return _cmd_factcheck(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 0


def _common_parser() -> argparse.ArgumentParser:
    """Shared options, defined once so both the top level and every subcommand
    accept them.  SUPPRESS keeps a subcommand default from clobbering a value
    already given to the top-level parser."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="yt2wiki data directory (or $YT2WIKI_HOME)")
    common.add_argument("--profile", default=None, choices=list(PROFILE_NAMES))
    common.add_argument("--device", type=int, default=argparse.SUPPRESS,
                        help="CUDA device index")
    common.add_argument("--registry", default=argparse.SUPPRESS, help="path to models.yaml")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="yt2wiki",
        description="Compile a local video into an illustrated, evidence-grounded wiki article.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"yt2wiki {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("info", help="show configuration, GPU and registry summary", parents=[common])

    p = sub.add_parser("init", help="create the data directory and database", parents=[common])

    p = sub.add_parser("add", help="add a source (URL or local file) without running it", parents=[common])
    p.add_argument("target", help="YouTube URL or path to a local video file")

    p = sub.add_parser("run", help="run a job for a source", parents=[common])
    p.add_argument("target", help="YouTube URL, local video path, or an existing source id")
    p.add_argument("--stages", default="", help="comma separated subset of stages")
    p.add_argument("--force", action="store_true", help="ignore cached stage outputs")
    p.add_argument("--typed", action="store_true",
                   help="use the V9 publisher with the gated typed editorial path "
                        "(default off; --stages/--force do not apply)")

    p = sub.add_parser("serve", help="start the web application and worker", parents=[common])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--reload", action="store_true")

    p = sub.add_parser("jobs", help="list jobs and their stage states", parents=[common])

    p = sub.add_parser("models", help="print the model registry as JSON", parents=[common])

    p = sub.add_parser("export", help="re-export an article (markdown, html, zip)", parents=[common])
    p.add_argument("article_id")

    p = sub.add_parser("schemas", help="print the JSON schemas used between stages")

    p = sub.add_parser("inference", help="manage inference providers (operator console)",
                       parents=[common])
    isub = p.add_subparsers(dest="inference_command")
    isub.add_parser("show", help="print saved providers, roles and last test status",
                    parents=[common])
    q = isub.add_parser("save", help="validate and save providers/roles from a JSON file",
                        parents=[common])
    q.add_argument("--file", required=True,
                   help="JSON file with {providers: [...], roles: {...}} (same schema as the settings API)")
    q = isub.add_parser("test", help="qualify a saved provider and record the result",
                        parents=[common])
    q.add_argument("--name", required=True, help="provider name as saved")
    q.add_argument("--timeout", type=float, default=6.0, help="seconds for the /models probe")

    p = sub.add_parser("factcheck", help="independent fact-check overlay (explicit, opt-in)",
                       parents=[common])
    fsub = p.add_subparsers(dest="factcheck_command")
    q = fsub.add_parser("run", help="run external corroboration for one article revision",
                        parents=[common])
    q.add_argument("article_id")
    q.add_argument("--provider", default="ddgs", choices=["ddgs", "mock"],
                   help="evidence provider (keyless DDGS search + bounded fetch)")
    q.add_argument("--max-claims", type=int, default=10,
                   help="cap on claims assessed per run (8-12 recommended)")
    q.add_argument("--verifier", default="none", choices=["none", "backend"],
                   help="none: evidence collection only, no categorical factual "
                   "verdict (keyword overlap is not corroboration); backend: "
                   "a configured text backend assesses each passage, still "
                   "citation-gated")
    q.add_argument("--role", default="text",
                   help="inference registry role for --verifier backend")
    q.add_argument("--primary-domains", default="",
                   help="comma-separated issuer/official domains treated as primary records")
    q.add_argument("--no-cache", action="store_true",
                   help="bypass the local fetched-page cache for this run")
    q = fsub.add_parser("show", help="show the current overlay for an article revision",
                        parents=[common])
    q.add_argument("article_id")
    return parser


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _settings(args) -> Settings:
    return Settings.load(
        data_dir=getattr(args, "data_dir", None),
        profile=getattr(args, "profile", None),
        device_index=getattr(args, "device", None),
        registry=getattr(args, "registry", None),
    )


def _service(args) -> tuple[Settings, Database, Service]:
    settings = _settings(args)
    db = Database(settings.paths.db_path)
    db.migrate()
    return settings, db, Service(settings, db)


# ----------------------------------------------------------------------
def _cmd_info(args) -> int:
    from .gpu import cuda_available, current_vram_gb, nvidia_smi_gpus

    settings = _settings(args)
    print(f"yt2wiki {__version__}")
    from .generations import build_info
    _build = build_info()
    print(f"build commit   : {_build['git_commit']}")
    print(f"image digest   : {_build['image_digest']}")
    print(f"data directory : {settings.paths.root}")
    print(f"profile        : {settings.profile} "
          f"(VRAM budget {settings.vram_budget_gb():.1f} GB)")
    print(f"config hash    : {settings.fingerprint()}")
    print(f"cuda available : {cuda_available()}")
    for gpu in nvidia_smi_gpus():
        free = " (free)" if settings.device_index == gpu.index else ""
        print(f"  gpu {gpu.index}: {gpu.name} "
              f"{gpu.total_gb:.1f} GB total, {gpu.free_gb:.1f} GB free{free}")
    if cuda_available():
        print(f"resident VRAM  : {current_vram_gb():.2f} GB")
    for role in ("asr", "vision", "text", "verify"):
        spec = settings.model(role)
        print(f"  {role:<7} -> {spec.repo} ({spec.variant(settings.profile)})")
    return 0


def _cmd_init(args) -> int:
    settings = _settings(args)
    Database(settings.paths.db_path).migrate()
    print(f"initialised {settings.paths.root}")
    return 0


def _cmd_add(args) -> int:
    _, _, service = _service(args)
    target = args.target
    if target.startswith("http://") or target.startswith("https://"):
        sid = service.create_youtube_source(target)
    else:
        path = Path(target)
        if not path.is_file():
            print(f"no such file: {path}", file=sys.stderr)
            return 2
        sid = service.create_upload_source(path)
    print(sid)
    return 0


def _cmd_run(args) -> int:
    from .pipeline import STAGES, run_job

    settings, db, service = _service(args)
    target = args.target
    if target.startswith("http://") or target.startswith("https://"):
        sid = service.create_youtube_source(target)
    else:
        path = Path(target)
        if path.is_file():
            sid = service.create_upload_source(path)
        else:
            sid = target
    job = service.create_job(sid, profile=settings.profile)
    if args.typed:
        # Controlled typed entry point: the same V9 publisher the web
        # worker runs, with the gated typed editorial path enabled.
        # Legacy-pipeline options do not apply here; combining them is a
        # usage error rather than a silent ignore.
        if args.stages or args.force:
            print("--typed cannot be combined with --stages/--force", file=sys.stderr)
            return 2
        from .v9 import run as v9run
        print(f"job {job} source {sid} profile {settings.profile} typed", flush=True)
        started = time.time()
        result = v9run.run_source(settings, db, service, sid,
                                  profile=settings.profile, job_id=job,
                                  enable_typed=True)
        print(json.dumps(result, indent=1, default=str))
        print(f"wall clock: {time.time() - started:.1f}s")
        return 0 if result.get("ok") else 1
    only = {s.strip() for s in args.stages.split(",") if s.strip()} if args.stages else None
    if only:
        unknown = only - set(STAGES)
        if unknown:
            print(f"unknown stages: {sorted(unknown)}", file=sys.stderr)
            return 2
    print(f"job {job} source {sid} profile {settings.profile}", flush=True)
    started = time.time()
    result = run_job(job, db, settings, only=only, force=args.force,
                     cancel_check=lambda: service.cancel_check(job))
    print(json.dumps(result, indent=1, default=str))
    print(f"wall clock: {time.time() - started:.1f}s")
    return 0 if result.get("ok") else 1


def _cmd_serve(args) -> int:
    import uvicorn

    settings = _settings(args)
    from .gpu import cuda_available, nvidia_smi_gpus

    if not cuda_available():
        print("warning: no CUDA device visible; stages that need a model will fail",
              file=sys.stderr)
    db = Database(settings.paths.db_path)
    db.migrate()
    from .web.app import create_app

    app = create_app(settings)
    app.state.runner.start()
    print(f"yt2wiki listening on http://{args.host}:{args.port} "
          f"(data: {settings.paths.root}, profile: {settings.profile})")
    if nvidia_smi_gpus():
        print("gpus: " + ", ".join(f"{g.name} ({g.free_gb:.1f} GB free)" for g in nvidia_smi_gpus()))
    # The inference-settings guard identifies the operator console by the real
    # TCP peer (request.client.host) and never trusts forwarding headers.
    # Uvicorn must not rewrite that peer from X-Forwarded-For either: with its
    # default proxy handling, FORWARDED_ALLOW_IPS=* would let any remote client
    # claim 127.0.0.1 and pass the localhost guard. Disable it explicitly.
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload,
                log_level="info", proxy_headers=False)
    return 0


def _cmd_jobs(args) -> int:
    from .pipeline import STAGES

    _, db, service = _service(args)
    for job in service.list_jobs(30):
        detail = service.get_job(job["job_id"]) or {}
        stages = {s["stage"]: s["state"] for s in detail.get("stages", [])}
        done = sum(1 for v in stages.values() if v == "done")
        print(f"{job['job_id']:<22} {job['state']:<9} {done}/{len(stages) or len(STAGES)} "
              f"{job['source_title'] or job['source_id']}  {job['error'] or ''}")
    return 0


def _cmd_models(args) -> int:
    from .config import load_registry

    settings = _settings(args)
    reg = load_registry(settings.registry_path)
    out = {
        "schema_version": reg.get("schema_version"),
        "profiles": reg.get("profiles"),
        "assignments": reg.get("assignments"),
        "models": {
            k: {
                "role": v.role,
                "engine": v.engine,
                "repo": v.repo,
                "revision": v.revision,
                "license": v.license,
                "params_b": v.params_b,
                "provider": v.provider,
                "vram_gb": v.vram_gb,
                "fallbacks": v.fallbacks,
                "variants": v.variants,
            }
            for k, v in reg["models"].items()
        },
        "current_profile": settings.profile,
        "fingerprint": settings.fingerprint(),
    }
    print(json.dumps(out, indent=1, default=str))
    return 0


def _cmd_export(args) -> int:
    from .pipeline.stages.publish import run as publish

    settings, db, _ = _service(args)
    row = db.query_one("SELECT source_id FROM articles WHERE article_id=?", (args.article_id,))
    if row is None:
        print(f"no such article: {args.article_id}", file=sys.stderr)
        return 2
    from .pipeline import StageContext

    ctx = StageContext(
        job_id="cli-export", source_id=row["source_id"], settings=settings, db=db,
        paths=settings.paths, fingerprint=settings.fingerprint(),
        artifacts={"article_id": args.article_id},
    )
    metrics = publish(ctx)
    print(json.dumps(metrics, indent=1, default=str))
    print(f"markdown : {settings.paths.article_markdown(args.article_id)}")
    print(f"standalone: {settings.paths.article_dir(args.article_id) / 'article.standalone.html'}")
    return 0


def _cmd_factcheck(args) -> int:
    """Explicit fact-check runs. Nothing here runs during generation, viewing or export."""
    from .factcheck import run as _run
    from .factcheck import store as _store

    settings, db, _ = _service(args)
    cmd = getattr(args, "factcheck_command", None)
    if cmd == "show":
        overlay = _store.current_overlay(db, args.article_id)
        if overlay is None:
            print(f"no current overlay for {args.article_id}: "
                  "not checked (nothing has been verified)")
            return 1
        print(f"overlay  : {overlay.overlay_id}")
        print(f"revision : {overlay.article_revision_sha256[:12]}")
        print(f"checked  : {overlay.checked_at} · run {overlay.run_status}")
        mode = overlay.verifier.get("mode", "collection")
        print(f"mode     : {mode} "
              f"(evidence collection only — no factual verdicts)"
              if mode != "assessed" else
              f"mode     : assessed "
              f"(role={overlay.verifier.get('role')} "
              f"model={overlay.verifier.get('model')})")
        cov = overlay.coverage
        skipped = cov.get("skipped_claims", [])
        n_skipped = len(skipped) if isinstance(skipped, list) else skipped
        print(f"coverage : {cov.get('checked_claims')} of "
              f"{cov.get('eligible_claims')} material claims selected; "
              f"{cov.get('retrieved_claims', 0)} with retrieved passages; "
              f"{n_skipped} skipped")
        for verdict, count in (cov.get("by_assessment") or {}).items():
            print(f"  {verdict}: {count}")
        for claim in overlay.claims:
            print(f"  [{claim.assessment}] {claim.quote[:100]}")
        return 0
    if cmd != "run":
        print("usage: yt2wiki factcheck {run|show} article_id", file=sys.stderr)
        return 2
    row = db.query_one("SELECT article_id FROM articles WHERE article_id=?",
                       (args.article_id,))
    if row is None:
        print(f"no such article: {args.article_id}", file=sys.stderr)
        return 2
    max_claims = max(1, min(int(args.max_claims), 25))
    primaries = tuple(d.strip().lower() for d in
                      (args.primary_domains or "").split(",") if d.strip())
    if args.provider == "ddgs":
        from .factcheck import ddgs_provider as _ddgs
        cache_dir = (None if args.no_cache
                     else settings.paths.root / "factcheck-cache")
        provider = _ddgs.DDGSProvider(
            cache=_ddgs.FactcheckCache(directory=cache_dir),
            primary_domains=primaries)
    else:
        from .factcheck import evidence as _ev
        provider = _ev.MockProvider()
    verifier = None
    verifier_role = verifier_model = None
    if args.verifier == "backend":
        from .backends.loader import ModelManager
        from .factcheck import verifier as _vf
        manager = ModelManager(settings)
        loaded: dict = {}
        try:
            loaded = manager.acquire_all(args.role)
            backend = loaded[args.role]
            spec = getattr(backend, "spec", None)
            verifier_role = args.role
            verifier_model = (getattr(spec, "key", None)
                              or getattr(backend, "model_key", None)
                              or type(backend).__name__)
            verifier = _vf.verifier_from_chat(
                lambda messages: backend.chat(  # noqa: E731
                    messages, max_new_tokens=256),
                max_tokens=256, timeout_s=90.0)
        except Exception as exc:  # noqa: BLE001
            print(f"backend verifier unavailable: {exc}", file=sys.stderr)
            return 2
        try:
            report = _run.run_factcheck(
                db, args.article_id, provider=provider,
                max_claims=max_claims, verifier=verifier,
                primary_domains=primaries,
                verifier_role=verifier_role, verifier_model=verifier_model)
        finally:
            manager.release_all(loaded)
    else:
        report = _run.run_factcheck(
            db, args.article_id, provider=provider,
            max_claims=max_claims, primary_domains=primaries)
    shipped = _store.refresh_export_sidecar(db, settings.paths, args.article_id,
                                              report.overlay)
    if shipped is None:
        print("article edited during run: overlay retained as historical for "
              f"revision {report.overlay.article_revision_sha256[:12]}; "
              "no sidecar shipped")
    overlay = report.overlay
    print(f"overlay  : {overlay.overlay_id} ({overlay.run_status})")
    print(f"revision : {overlay.article_revision_sha256[:12]}")
    mode = overlay.verifier.get("mode", "collection")
    if mode == "assessed":
        print(f"mode     : assessed (role={verifier_role} model={verifier_model}; "
              f"{report.verifier_calls} verifier calls, "
              f"budget {report.verifier_budget})")
    else:
        print("mode     : evidence collection only — passages gathered, "
              "no categorical factual verdicts rendered")
    cov = overlay.coverage
    skipped = cov.get("skipped_claims", [])
    n_skipped = len(skipped) if isinstance(skipped, list) else skipped
    print(f"claims   : {cov['checked_claims']} selected / "
          f"{cov['eligible_claims']} eligible; "
          f"{cov.get('retrieved_claims', 0)} with retrieved passages; "
          f"{n_skipped} skipped")
    for verdict, count in (cov.get("by_assessment") or {}).items():
        print(f"  {verdict}: {count}")
    print(f"retrieval: {report.searches} searches, {report.fetches} fetches, "
          f"{report.fetch_bytes} bytes")
    for claim in overlay.claims:
        print(f"  [{claim.assessment}] {claim.quote[:100]}")
    for failure in report.failures[:10]:
        print(f"  failure: {failure}")
    # Core status is never changed by this command; surface that explicitly.
    status = db.query_one("SELECT status FROM articles WHERE article_id=?",
                           (args.article_id,))["status"]
    print(f"article status unchanged: {status}")
    return 0


def _cmd_inference(args) -> int:
    """Operator-console inference management (works inside the container).

    The settings web API refuses non-localhost peers (Docker gateway, SSH
    tunnels, LAN), so Docker deployments configure inference here via
    `docker exec <container> yt2wiki inference ...`. Secrets are never stored:
    providers reference environment variables by name, injected with an
    `env_file` (Compose) or stack variables (Portainer).
    """
    settings = _settings(args)
    cmd = getattr(args, "inference_command", None)
    if cmd == "show":
        return _cmd_inference_show(settings)
    if cmd == "save":
        return _cmd_inference_save(settings, args)
    if cmd == "test":
        return _cmd_inference_test(settings, args)
    print("usage: yt2wiki inference {show|save|test}", file=sys.stderr)
    return 2


def _cmd_inference_show(settings: Settings) -> int:
    cfg = inf.load(settings.paths)
    status = inf.load_status(settings.paths)
    print(json.dumps({"providers": cfg.get("providers", {}),
                      "roles": cfg.get("roles", {}),
                      "status": status}, indent=1, default=str))
    return 0


def _cmd_inference_save(settings: Settings, args) -> int:
    try:
        payload = inf.read_payload_file(settings.paths, args.file)
        cfg = inf.save(settings.paths, payload)
    except inf.InferenceConfigError as exc:
        print(f"invalid inference configuration: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(cfg, indent=1, default=str))
    return 0


def _cmd_inference_test(settings: Settings, args) -> int:
    """Qualify one saved provider; exit nonzero unless its assigned roles qualify.

    Reachability (`/models`) alone is connectivity, not production role
    qualification: every role assigned to this provider must pass its real
    generation probe. Roles assigned to other providers — or to none — do not
    affect this provider's exit code.
    """
    cfg = inf.load(settings.paths)
    prov = (cfg.get("providers") or {}).get(args.name)
    if not prov:
        print(f"no such provider: {args.name}", file=sys.stderr)
        return 2
    result = inf.test_endpoint(prov.get("url") or "", prov.get("api_key_env") or "",
                               timeout=args.timeout, model=prov.get("model") or "")
    inf.record_test(settings.paths, args.name, result)
    print(json.dumps(result, indent=1, default=str))
    if not result.get("ok"):
        return 1
    roles = cfg.get("roles") or {}
    required = [role for role in ("writer", "vision", "asr") if roles.get(role) == args.name]
    if "writer" in required and not result.get("qualified_for_writer"):
        print(f"provider {args.name!r} is assigned the writer role but failed writer qualification",
              file=sys.stderr)
        return 1
    if "vision" in required and not result.get("qualified_for_vision"):
        print(f"provider {args.name!r} is assigned the vision role but failed vision qualification",
              file=sys.stderr)
        return 1
    if "asr" in required and not result.get("qualified_for_asr"):
        print(f"provider {args.name!r} is assigned the transcription role but failed "
              f"transcription qualification", file=sys.stderr)
        return 1
    return 0


def _cmd_schemas(args) -> int:
    from .records import EVIDENCE_KINDS
    from .pipeline import STAGES

    print(json.dumps(
        {
            "stages": list(STAGES),
            "evidence_kinds": list(EVIDENCE_KINDS),
            "stage_outputs": {
                "transcribe": {
                    "segments": ["start", "end", "text", "avg_logprob", "words"],
                    "words": ["start", "end", "word", "probability"],
                },
                "frame_candidates": {
                    "frames": ["frame_id", "t", "path", "kind", "reason", "phash",
                               "scene_score", "duplicate_of"]
                },
                "visual_interpret": {
                    "payload": ["frame_type", "summary", "visible_text", "ui_elements",
                                "settings", "actions_shown", "warnings",
                                "is_instructional", "legibility", "confidence",
                                "crop_tiles"]
                },
                "evidence": {
                    "observations": ["evidence_id", "kind", "text", "normalized", "literals",
                                     "t_start", "t_end", "frame_ids", "confidence",
                                     "model_key", "prompt_version", "status"],
                    "literal": ["kind", "literal", "normalized", "context", "t_start", "t_end"],
                },
                "procedures": {
                    "procedure": ["title", "summary", "prerequisites", "steps", "alternatives",
                                  "corrections", "unresolved", "limitations"],
                    "step": ["position", "text", "detail", "evidence_ids", "frame_ids",
                             "warnings", "expected_result", "alternatives"],
                },
                "compose": {
                    "article": ["title", "summary", "scope", "sections", "steps", "frames",
                                "provenance"]
                },
                "verify": {
                    "findings": ["direction", "severity", "code", "message", "detail"]
                },
                "publish": ["article.md", "article.standalone.html", "article.zip",
                            "verification.json", "provenance.json"],
            },
        },
        indent=2,
    ))
    return 0
