"""V9 run orchestration: base stages, writer, discovery, selection, publish."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("yt2wiki.v9.run")

V9_STAGES = ("v9_write", "v9_discover", "v9_select", "v9_caption", "v9_publish")
BASE_STAGES = ("acquisition", "inspect", "transcribe", "frame_candidates")


def _record(db, job_id: str, stage: str, state: str, metrics: dict | None = None,
            error: str | None = None, started: float | None = None,
            artifacts: dict | None = None) -> None:
    now = time.time()
    db.execute(
        """INSERT INTO stages(job_id, stage, state, started_at, finished_at, duration_s,
                              metrics_json, error, artifacts_json)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id, stage) DO UPDATE SET state=excluded.state,
             finished_at=excluded.finished_at, duration_s=excluded.duration_s,
             metrics_json=excluded.metrics_json, error=excluded.error,
             artifacts_json=COALESCE(excluded.artifacts_json, stages.artifacts_json)""",
        (job_id, stage, state, started or now, now,
         (now - started) if started else 0.0,
         json.dumps(metrics or {}), error,
         json.dumps(artifacts) if artifacts is not None else None))


def _set_stage(db, job_id: str, stage: str) -> None:
    db.execute("UPDATE jobs SET stage=?, updated_at=? WHERE job_id=?",
               (stage, time.time(), job_id))


def _finish_job(db, job_id: str, state: str, *, article_id: str | None = None,
                error: str | None = None) -> None:
    now = time.time()
    db.execute(
        "UPDATE jobs SET state=?, article_id=COALESCE(?, article_id), error=?, "
        "finished_at=?, updated_at=? WHERE job_id=?",
        (state, article_id, error, now, now, job_id))


def adjudicate_section_with_judge(vb, sec: dict[str, Any],
                                    frames: list[dict[str, Any]],
                                    used: set[str],
                                    by_id: dict[str, dict[str, Any]] | None = None,
                                    *, cache: dict | None = None,
                                    seed: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One section through rank shortlist + the V9-Z1 rubric judge.

    Returns (chosen picks, decision record). Pure orchestration over the
    vision backend; unit-testable with a stub backend (no GPU).
    """
    from . import judge as v9judge
    from . import select as v9select

    if by_id is None:
        by_id = {f["frame_id"]: f for f in frames}
    if cache is None:
        cache = {}
    cands = v9select.candidates_for_section(frames, sec["t_start"], sec["t_end"])
    cands = [c for c in cands if c["frame_id"] not in used] or cands
    rank = v9select.rank_section(vb, sec, cands)
    order = list(rank["best"][:3])
    ranked = []
    for idx in order:
        f = by_id.get(rank["candidates"][idx]["frame_id"])
        if f is not None:
            ranked.append(f)
    initial = ranked[0] if ranked else None
    alternatives = ranked[1:]
    adj = v9judge.adjudicate_section_z1(
        vb, sec, initial, alternatives, frames,
        max_rounds=2, seed=seed, cache=cache)
    final_ids = list(adj.get("final") or [])[:2]
    chosen = []
    dropped_used = []
    for fid in final_ids:
        if fid in used:
            dropped_used.append(fid)
            continue
        f = by_id.get(fid)
        if f is None:
            continue
        chosen.append({"frame_id": fid, "t": f["t"],
                       "path": f["path"], "caption": ""})
        used.add(fid)
    if dropped_used:
        adj = dict(adj, reason=(adj.get("reason") or "") +
                   f" (dropped already-installed: {','.join(dropped_used)})")
    vision_model = getattr(getattr(vb, "spec", None), "key", type(vb).__name__)
    record = {"section_id": sec["section_id"], "t": [sec["t_start"], sec["t_end"]],
              "vision_model": vision_model,
              "n_candidates": len(rank["candidates"]),
              "candidates": rank["candidates"][:6],
              "initial": adj.get("initial"),
              "rank_reason": rank.get("reason"),
              "z1_fallback": adj.get("z1_fallback"),
              "judge_decision": adj.get("decision"),
              "judge_reason": adj.get("reason"),
              "rounds": adj.get("rounds"),
              "shown": adj.get("shown"),
              "chosen": [c["frame_id"] for c in chosen],
              "frames_inspected": adj.get("frames_inspected"),
              "vlm_calls": adj.get("vlm_calls"),
              "elapsed_s": adj.get("elapsed_s")}
    return chosen, record


def _ensure_base(settings, db, service, source_id: str,
                 job_id: str | None = None) -> dict[str, Any]:
    """Run acquisition/inspect/transcribe/frame_candidates only where missing.

    With `job_id`, the base stages are recorded under that job (so one job
    row shows the whole V9 run); without it a separate base job is created.
    """
    from ..pipeline import StageContext
    from ..pipeline.stages import acquisition, frame_candidates, inspect, transcribe

    counts = {}
    n_trans = db.query_one(
        "SELECT COUNT(*) c FROM transcript_segments WHERE source_id=?", (source_id,))["c"]
    counts["transcript_segments"] = n_trans
    n_frames = db.query_one(
        "SELECT COUNT(*) c FROM frames WHERE source_id=?", (source_id,))["c"]
    counts["frames"] = n_frames
    if n_trans > 0 and n_frames > 0:
        if job_id:
            for name in BASE_STAGES:
                _record(db, job_id, name, "skipped", {"reused": True})
        return {"reused": True, **counts}
    from pathlib import Path as _P
    video_ok = _P(settings.paths.source_video(source_id)).is_file()
    from ..services import Service  # noqa: F401
    job = job_id or service.create_job(source_id, profile=settings.profile)
    ctx = StageContext(job_id=job, source_id=source_id, settings=settings, db=db,
                       paths=settings.paths, fingerprint=settings.fingerprint())
    for name, mod in (("acquisition", acquisition), ("inspect", inspect),
                      ("transcribe", transcribe), ("frame_candidates", frame_candidates)):
        if ((name == "acquisition" and n_trans > 0 and video_ok)
                or (name in ("inspect", "transcribe") and n_trans > 0)
                or (name == "frame_candidates" and n_frames > 0)):
            if job_id:
                _record(db, job_id, name, "skipped", {"reused": True})
            continue
        ctx.current_stage = name
        t = time.time()
        if job_id:
            _set_stage(db, job_id, name)
            _record(db, job_id, name, "running")
        # The returned metrics and stage artifacts carry per-job provenance
        # (e.g. transcribe's asr_server / asr_unsupported_options / asr_warnings);
        # record them instead of dropping them.
        out = mod.run(ctx) or {}
        if job_id:
            _record(db, job_id, name, "done",
                    out if isinstance(out, dict) else {},
                    started=t, artifacts=dict(ctx.artifacts))
    return {"reused": False, "job": job,
            "transcript_segments": db.query_one(
                "SELECT COUNT(*) c FROM transcript_segments WHERE source_id=?", (source_id,))["c"],
            "frames": db.query_one(
                "SELECT COUNT(*) c FROM frames WHERE source_id=?", (source_id,))["c"]}


def run_source(settings, db, service, source_id: str, *,
               enable_captions: bool = False, enable_enrichment: bool = False,
               profile: str | None = None, job_id: str | None = None,
               inference_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the V9 publisher for one source.

    `job_id` adopts an existing queued job (the web worker's); otherwise a
    new job row is created (the CLI). Either way the job row carries live
    stage, final state and the published article id.
    """
    from ..backends.loader import ModelManager
    from . import frames as v9frames
    from . import publish as v9publish
    from . import select as v9select
    from . import writer as v9writer

    # Resolve inference snapshot early so writer and selector both honour it
    if inference_snapshot is None and job_id is not None:
        try:
            snap_row = db.query_one("SELECT config_json FROM jobs WHERE job_id=?", (job_id,))
            snap_cfg = json.loads(snap_row["config_json"] or "{}") if snap_row else {}
            inference_snapshot = snap_cfg.get("inference")
        except Exception:  # noqa: BLE001
            inference_snapshot = None
    # Fallback: live config for CLI jobs that have no snapshot
    if inference_snapshot is None:
        try:
            from ..web import inference as _inf
            snap = {}
            cfg = _inf.load(settings.paths)
            st = _inf.load_status(settings.paths)
            for role in ("writer", "vision", "asr"):
                name = (cfg.get("roles") or {}).get(role)
                if name:
                    prov = (cfg.get("providers") or {}).get(name, {})
                    status = st.get(name, {})
                    snap[role] = {
                        "provider": name, "url": prov.get("url"), "model": prov.get("model"),
                        "api_key_env": prov.get("api_key_env"),
                        "reachable": bool(status.get("ok")),
                        "qualified_for_writer": bool(status.get("qualified_for_writer")),
                        "qualified_for_vision": bool(status.get("qualified_for_vision")),
                        "qualified_for_asr": bool(status.get("qualified_for_asr")),
                    }
            inference_snapshot = snap or None
        except Exception:  # noqa: BLE001
            inference_snapshot = None

    t0 = time.time()
    result: dict[str, Any] = {"source_id": source_id, "stages": {}}
    v9job = job_id or service.create_job(source_id, profile=profile or settings.profile,
                                         config={"inference": inference_snapshot} if inference_snapshot else {})
    result["v9_job_id"] = v9job
    db.execute("UPDATE jobs SET state='running', started_at=?, updated_at=?, error=NULL "
               "WHERE job_id=?", (t0, t0, v9job))

    def stage(name):
        _set_stage(db, v9job, name)
        _record(db, v9job, name, "running")

    try:
        # --- base media/ASR ---
        base = _ensure_base(settings, db, service, source_id, job_id=v9job)
        result["base"] = base

        # --- write ---
        t = time.time()

        def _v9_rec(stage_name: str):
            def _rec(entry: dict) -> None:
                entry.setdefault("job_id", v9job)
                entry.setdefault("source_id", source_id)
                entry.setdefault("stage", stage_name)
                db.record_model_load(entry)

            return _rec

        stage("v9_write")
        article = v9writer.write_article(settings, db, source_id,
                                         inference_snapshot=inference_snapshot,
                                         recorder=_v9_rec("v9_write"))
        _record(db, v9job, "v9_write", "done",
                {"sections": len(article["sections"]), "kind": article["kind"],
                 "coverage": article["coverage"]}, started=t)
        result["article"] = {k: v for k, v in article.items() if k != "sections"}
        sections = article["sections"]

        # --- discover ---
        t = time.time()
        stage("v9_discover")
        disc = v9frames.discover(settings, db, source_id)
        _record(db, v9job, "v9_discover", "done", disc["coverage"], started=t)
        result["coverage"] = disc["coverage"]
        frames = disc["frames"]

        # --- select (V9-Z1 rubric judge over comparative VLM ranking) ---
        # Flow per section: rank_section proposes an ordered shortlist from
        # the selected vision backend; adjudicate_section_z1 then runs the
        # frozen editorial judge on it (batched veto pre-filter, comparative
        # 0-2x4 rubric scoring, KEEP/REPLACE/EXPAND/USE_PAIR/OMIT policy,
        # directed refinement, re-verification). The judge's finals are what
        # get installed -- the rank order alone never publishes an image.
        # Resolve inference snapshot: explicit arg wins, else job's stored snapshot
        if inference_snapshot is None and job_id is not None:
            try:
                snap_row = db.query_one("SELECT config_json FROM jobs WHERE job_id=?", (job_id,))
                snap_cfg = json.loads(snap_row["config_json"] or "{}") if snap_row else {}
                inference_snapshot = snap_cfg.get("inference")
            except Exception:  # noqa: BLE001
                inference_snapshot = None
        t = time.time()
        stage("v9_select")
        manager = ModelManager(settings, recorder=_v9_rec("v9_select"),
                               inference_snapshot=inference_snapshot)
        picks: dict[str, list[dict[str, Any]]] = {}
        decisions = []
        used: set[str] = set()
        judge_cache: dict[str, Any] = {}
        by_id = {f["frame_id"]: f for f in frames}
        with manager.acquire("vision") as vb:
            for sec in sections:
                chosen, record = adjudicate_section_with_judge(
                    vb, sec, frames, used, by_id,
                    cache=judge_cache, seed=settings.deterministic_seed)
                picks[sec["section_id"]] = chosen
                decisions.append(record)
        _record(db, v9job, "v9_select", "done",
                {"sections_with_image": sum(1 for v in picks.values() if v),
                 "images": sum(len(v) for v in picks.values()),
                 "decisions": decisions}, started=t)
        result["selection"] = decisions
        # Caption policy (A3/A4): reuse judge explanations as the "why" and
        # section context as visible grounding, without asserting unseen text.
        # This does not call the vision model anew; fresh descriptions would
        # use the already-assigned provider (not enabled by default until
        # the A5 sample qualifies). Preserve author edits for same frame ids.
        decision_by_section = {d["section_id"]: d for d in decisions}
        # Preserve edited captions/alt for frames that stay installed
        prev_captions: dict[str, dict[str, str]] = {}
        try:
            prev_rows = db.query(
                "SELECT frame_id, caption, alt_text FROM article_assets "
                "WHERE article_id=?", (f"art9-{source_id}",))
            for r in prev_rows:
                # Only treat non-generic captions as edits worth keeping
                cap = (r["caption"] or "").strip()
                alt = (r["alt_text"] or "").strip()
                if cap and cap.lower() != "source frame":
                    prev_captions[r["frame_id"]] = {"caption": cap, "alt_text": alt}
        except Exception:  # noqa: BLE001
            pass
        for sec in sections:
            plist = picks.get(sec["section_id"]) or []
            if not plist:
                continue
            dec = decision_by_section.get(sec["section_id"]) or {}
            # Judge reason is pixel-grounded; rank reason is fallback
            why = (dec.get("judge_reason") or dec.get("rank_reason") or "").strip()
            # First sentence of why, truncated for caption
            why_sent = why.split(".")[0].strip()[:160] if why else ""
            for pick in plist:
                if pick["frame_id"] in prev_captions:
                    pick["caption"] = prev_captions[pick["frame_id"]]["caption"]
                    pick["alt_text"] = prev_captions[pick["frame_id"]].get("alt_text") or ""
                    pick["caption_source"] = "edited"
                    continue
                if enable_captions and pick.get("caption"):
                    # Already captioned via vision in a prior run; keep but ensure alt
                    if not pick.get("alt_text"):
                        pick["alt_text"] = (pick["caption"].split(".")[0] or pick["caption"])[:120]
                    pick["caption_source"] = pick.get("caption_source") or "vision"
                    continue
                # Auto caption from judge + section heading (no new inference)
                heading = sec.get("heading") or "Illustration"
                if why_sent:
                    # Caption expresses visible content + why it illustrates
                    cap = f"{why_sent} — illustrates {heading.lower()}"
                else:
                    cap = f"Illustration for {heading}"
                # Keep concise, one sentence where possible
                if len(cap) > 180:
                    cap = cap[:177].rsplit(" ", 1)[0] + "…"
                pick["caption"] = cap
                pick["caption_source"] = "auto"
                # Alt: concise visible description, no timestamps, no generic duplicate
                alt = why_sent or heading
                alt = alt.split(".")[0][:120]
                # Strip timestamps if any slipped in
                import re as _re
                alt = _re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]", "", alt).strip(" –-")
                pick["alt_text"] = alt if alt and alt.lower() != "source frame" else heading[:100]
        if not enable_captions:
            # Captions above are derived from already-available judge outputs,
            # not fresh vision calls. The old per-frame caption generator stays
            # opt-in until the A5 sample qualifies.
            pass

        # --- captions (optional feature, opt-in; failure never fatal) ---
        t = time.time()
        stage("v9_caption")
        caption_failures = 0
        if enable_captions:
            manager.recorder = _v9_rec("v9_caption")
            with manager.acquire("vision") as vb:
                for plist in picks.values():
                    for pick in plist:
                        # Preserve edited captions, but allow auto captions to be
                        # replaced by the vision model when opt-in is enabled.
                        if pick.get("caption") and pick.get("caption_source") != "auto":
                            continue
                        cap = v9select.caption_frame(vb, pick["path"])
                        if cap:
                            pick["caption"] = cap
                            pick["caption_source"] = "vision"
                        else:
                            caption_failures += 1
        _record(db, v9job, "v9_caption", "done",
                {"captioned": sum(1 for v in picks.values() for p in v if p.get("caption")),
                 "failures": caption_failures}, started=t)

        # --- enrichment (optional, default off) ---
        if enable_enrichment:
            from .select import enrich_paragraph  # noqa
            log.info("enrichment enabled (experimental)")

        # --- publish ---
        t = time.time()
        stage("v9_publish")
        srow = db.query_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        provenance = {"url": srow["origin_url"], "uploader": srow["uploader"],
                      "title": srow["title"], "duration_s": srow["duration_s"],
                      "profile": settings.profile,
                      "fingerprint": settings.fingerprint(),
                      "attribution_line": (f"Compiled from the video **{srow['title']}** by {srow['uploader']}" if srow["uploader"] else f"Compiled from the local video file **{srow['title']}**")}
        md, _assets = v9publish.assemble_markdown(
            article["title"], article["summary"], sections, picks, provenance)
        report = {"profile": settings.profile,
                  "settings_fingerprint": settings.fingerprint(),
                  "device_index": settings.device_index,
                  "device_map": _device_map(),
                  "models": {r: {"repo": settings.model(r).repo,
                                  "revision": settings.model(r).revision}
                             for r in ("asr", "vision", "text")},
                  "git_commit": _git_commit(),
                  "timings": {"total_s": round(time.time() - t0, 3)},
                  "vram": _vram_snapshot(),
                  "coverage": article["coverage"],
                  "frame_coverage": disc["coverage"],
                  "selection": decisions,
                  "captions_enabled": enable_captions,
                  }
        article_id = f"art9-{source_id}"
        pub = v9publish.persist(settings, db, article_id=article_id, source_id=source_id,
                                job_id=v9job, title=article["title"], markdown=md,
                                sections=sections, picks=picks,
                                provenance=provenance, report=report)
        _record(db, v9job, "v9_publish", "done", pub, started=t)
        result.update({"article_id": article_id, "ok": True,
                       "wall_s": round(time.time() - t0, 3), "report": report,
                       "publish": pub})
        _finish_job(db, v9job, "done", article_id=article_id)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("v9 run failed for %s", source_id)
        db.execute("UPDATE stages SET state='failed', error=?, finished_at=? "
                   "WHERE job_id=? AND state='running'", (str(exc)[:500], time.time(), v9job))
        _record(db, v9job, "v9_failed", "failed", {}, error=str(exc)[:500])
        _finish_job(db, v9job, "failed", error=str(exc)[:500])
        result.update({"ok": False, "error": str(exc)[:500]})
        return result


def _device_map() -> dict[str, str]:
    try:
        import torch
        return {str(i): torch.cuda.get_device_name(i)
                for i in range(torch.cuda.device_count())} if torch.cuda.is_available() else {}
    except Exception:  # noqa: BLE001
        return {}


def _vram_snapshot() -> dict[str, Any]:
    try:
        import torch
        if not torch.cuda.is_available():
            return {"cuda": False}
        return {"cuda": True,
                "per_device_peak_gb": {str(i): round(torch.cuda.max_memory_allocated(i) / 1024**3, 3)
                                       for i in range(torch.cuda.device_count())}}
    except Exception:  # noqa: BLE001
        return {"cuda": False, "error": "unavailable"}


def _git_commit() -> str:
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=Path(__file__).resolve().parents[2],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"
