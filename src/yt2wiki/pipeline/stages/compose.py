"""Stage 8 — article composition.

The article *structure* is assembled deterministically from stored evidence and
the reconstructed procedure; the model is used only to write the short prose
blocks (scope, step details, troubleshooting explanations) and is explicitly
forbidden from introducing facts that are not in the evidence.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

from ...article import Article, FrameRef, Section, format_ts, render_markdown, timestamp_url
from ...backends.loader import ModelManager
from ...db.schema import json_dumps, json_loads
from ...literals import Literal, _command_fragments, extract_literals, normalize_value, values_match
from ...records import Evidence, Procedure
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.compose")

PROMPT_VERSION = "v2"
SYSTEM = (
    "You write the short prose blocks of a technical wiki article. "
    "You may ONLY use facts present in the evidence you are given. "
    "You never add background knowledge, defaults or examples of your own."
)

SCOPE_PROMPT = """Write two short prose blocks for a technical wiki article built from a
tutorial video.

1. "summary": 2-3 sentences answering what the reader will be able to do.
2. "scope": 2-4 sentences on what this covers, what it assumes, and what it
   deliberately does not cover.

Base both ONLY on the material below. Do not invent tools, versions or goals.

TITLE CANDIDATE: {title}
PROCEDURE SUMMARY: {summary}
PREREQUISITES: {prereqs}
EVIDENCE KINDS PRESENT: {kinds}
SAMPLE EVIDENCE: {sample}

Return JSON: {{"summary": "...", "scope": "..."}}"""

STEP_PROSE_PROMPT = """Improve the wording of one instruction step for a technical wiki article.

Constraints:
- Keep every exact value, command, path, flag and identifier EXACTLY as given.
- Do not add anything that is not in the step or its evidence.
- Be concise and imperative. Max 2 sentences for "text" and 3 for "detail".

STEP: {step}
SUPPORTING EVIDENCE: {evidence}

Return JSON: {{"text": "...", "detail": "...", "expected_result": "..."}}"""


def run(ctx: StageContext) -> dict[str, Any]:
    started = time.time()
    t = ctx.settings.tuning
    procedure = _load_procedure(ctx)
    evidence = _load_evidence(ctx)
    if not evidence:
        raise RuntimeError("no evidence available; run the evidence stage first")
    frames = _load_frames(ctx)

    article = Article(
        source_id=ctx.source_id,
        title=procedure.title or "Procedure from source video",
        provenance=_provenance(ctx),
    )

    manager = ModelManager(ctx.settings, recorder=_recorder(ctx))
    with manager.acquire("text") as backend:
        prose = _scope_prose(ctx, backend, procedure, evidence)
        article.summary = prose.get("summary", "") or procedure.summary
        article.scope = prose.get("scope", "")

    # Step wording is taken verbatim from the validated procedure: the model
    # already produced it under the "cite evidence or drop the step" rule, and
    # re-rewriting it here would add a second chance to invent instructions.

    _assemble(ctx, article, procedure, evidence, frames)

    article_id = f"art-{ctx.source_id}"
    md = render_markdown(article, asset_prefix="assets")
    ctx.artifacts["article_frames"] = len(article_frames(md))
    _store(ctx, article, article_id, md)
    ctx.artifacts["article_id"] = article_id
    ctx.artifacts["article_markdown_chars"] = len(md)
    return {
        "title": article.title,
        "markdown_chars": len(md),
        "sections": len(article.sections),
        "steps": len(procedure.steps),
        "frames_used": len(article_frames(md)),
        "evidence_items": len(evidence),
        "elapsed_s": round(time.time() - started, 3),
    }


# ----------------------------------------------------------------------
def _recorder(ctx: StageContext):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", "compose")
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


def _load_procedure(ctx: StageContext) -> Procedure:
    row = ctx.db.query_one(
        "SELECT * FROM procedures WHERE source_id=?", (ctx.source_id,)
    )
    if row is None:
        raise RuntimeError("no procedure stored; run the procedures stage first")
    from ...records import Procedure, ProcedureStep

    procedure = Procedure(
        procedure_id=row["procedure_id"], title=row["title"] or "", summary=row["summary"] or ""
    )
    steps = ctx.db.query(
        "SELECT * FROM procedure_steps WHERE procedure_id=? ORDER BY position",
        (row["procedure_id"],),
    )
    for s in steps:
        procedure.steps.append(
            ProcedureStep(
                position=s["position"],
                text=s["text"],
                detail=s["detail"] or "",
                evidence_ids=json_loads(s["evidence_ids_json"], []),
                frame_ids=json_loads(s["frame_ids_json"], []),
                warnings=json_loads(s["warnings_json"], []),
                expected_result=s["expected_result"] or "",
                alternatives=json_loads(s["alternatives_json"], []),
            )
        )
    return procedure


def _load_evidence(ctx: StageContext) -> list[Evidence]:
    rows = ctx.db.query(
        "SELECT * FROM evidence WHERE source_id=? ORDER BY t_start", (ctx.source_id,)
    )
    return [Evidence.from_row(r) for r in rows]


def _load_frames(ctx: StageContext) -> dict[str, dict[str, Any]]:
    rows = ctx.db.query(
        """SELECT f.frame_id, f.t, f.path, f.kind, p.payload_json
           FROM frames f LEFT JOIN frame_interpretations p ON p.frame_id=f.frame_id
           WHERE f.source_id=? ORDER BY f.t""",
        (ctx.source_id,),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r["frame_id"]] = {
            "frame_id": r["frame_id"],
            "t": float(r["t"]),
            "path": Path(r["path"]),
            "kind": r["kind"],
            "summary": (json_loads(r["payload_json"], {}) or {}).get("summary", ""),
        }
    return out


def _provenance(ctx: StageContext) -> dict[str, Any]:
    row = ctx.db.query_one("SELECT * FROM sources WHERE source_id=?", (ctx.source_id,))
    meta = json_loads(row["meta_json"], {}) or {}
    prov = meta.get("provenance") or {}
    kind = row["kind"] or "youtube"
    url = prov.get("webpage_url") or row["origin_url"]
    if kind in ("upload", "local"):
        # A local file has no public URL, but the reader still needs to be able
        # to jump to the span the statement came from, so keep the timestamp.
        url = None
        upload = meta.get("upload") or {}
        local_name = upload.get("filename") or Path(row["title"] or "video").name
    else:
        local_name = None
    attribution = f"Compiled from the video **{row['title']}**"
    if row["uploader"]:
        attribution += f" by {row['uploader']}"
    elif local_name:
        attribution = f"Compiled from the local video file **{local_name}**"
    return {
        "url": url,
        "title": row["title"],
        "uploader": row["uploader"],
        "duration_s": row["duration_s"],
        "license": prov.get("license"),
        "retrieved_at": prov.get("retrieved_at"),
        "profile": ctx.settings.profile,
        "fingerprint": ctx.settings.fingerprint(),
        "attribution_line": attribution,
        "video_id": prov.get("video_id"),
        "kind": kind,
        "local_file": local_name,
    }


def _scope_prose(ctx: StageContext, backend, procedure: Procedure, evidence: Sequence[Evidence]) -> dict[str, Any]:
    from ...records import EVIDENCE_KINDS

    kinds = sorted({e.kind for e in evidence})
    sample = "\n".join(
        f"- ({e.kind} @{format_ts(e.t_start)}) {e.text}" for e in evidence[:14]
    )
    prompt = SCOPE_PROMPT.format(
        title=procedure.title,
        summary=procedure.summary,
        prereqs="; ".join(procedure.prerequisites[:8]) or "none stated",
        kinds=", ".join(kinds),
        sample=sample,
    )
    try:
        return backend.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            max_new_tokens=600,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scope prose failed: %s", exc)
        return {}


def _step_evidence_text(ids: Sequence[str], evidence: Sequence[Evidence]) -> str:
    index = {e.evidence_id: e for e in evidence}
    out = []
    for i in ids:
        e = index.get(i)
        if e is not None:
            out.append(f"[{e.kind} @{format_ts(e.t_start)}] {e.text}")
    return "\n".join(out)


def _fallback_step(step, evidence: Sequence[Evidence]) -> dict[str, Any]:
    """Deterministic wording: the procedure already encodes validated text."""
    return {
        "text": step.text,
        "detail": step.detail,
        "expected_result": step.expected_result,
    }


# ----------------------------------------------------------------------
def _assemble(
    ctx: StageContext,
    article: Article,
    procedure: Procedure,
    evidence: Sequence[Evidence],
    frames: dict[str, dict[str, Any]],
) -> None:
    prov = article.provenance
    url = prov.get("url")

    if article.scope:
        # Scope prose summarises the whole procedure, so it is bound to the
        # procedure's full evidence span rather than any single observation.
        step_ids = [i for s in procedure.steps for i in (s.evidence_ids or [])]
        refs = [e for e in evidence if e.evidence_id in step_ids]
        times = [e.t_start for e in refs if e.t_start is not None]
        scope_body = [article.scope]
        if refs and times:
            t_min, t_max = min(times), max(times)
            link = timestamp_url(url, t_min)
            if link:
                scope_body.append(f"*Source: [{format_ts(t_min)}–{format_ts(t_max)}]({link})*")
            scope_body.append(_cite_comment(
                step="scope", ev_ids=[e.evidence_id for e in refs],
                t_min=t_min, t_max=t_max))
        article.sections.append(
            Section(heading="Scope and applicability", level=2, body=scope_body)
        )

    if procedure.prerequisites:
        body = [f"- {p}" for p in procedure.prerequisites]
        article.sections.append(Section(heading="Prerequisites", level=2, body=body))

    warnings = _collect_warnings(procedure, evidence)
    if warnings:
        body: list[str] = []
        for text, ev_ids in warnings:
            body.append(f"> **Warning** — {text}")
            refs = [e for e in evidence if e.evidence_id in ev_ids]
            times = [e.t_start for e in refs if e.t_start is not None]
            if refs:
                t_min = min(times) if times else None
                link = timestamp_url(url, t_min)
                if link:
                    body.append(f"> *Source: [{format_ts(t_min)}]({link})*")
                body.append(_cite_comment(
                    step="warnings", ev_ids=[e.evidence_id for e in refs],
                    t_min=t_min if refs else None,
                    t_max=max(times) if times else None))
        article.sections.append(Section(heading="Before you start", level=2, body=body))

    if procedure.steps:
        steps_section = Section(heading="Procedure", level=2)
        for step in procedure.steps:
            block = _render_step(step, evidence, frames, url)
            steps_section.body.append(block)
        article.sections.append(steps_section)

    commands = _collect_commands(ctx, evidence, procedure)
    if commands:
        body = ["Copy and paste these in order. Each entry is quoted verbatim from the source; "
                "text in angle brackets or square brackets is a placeholder you fill in.",
                "```bash", *commands, "```"]
        article.sections.append(Section(heading="Commands", level=2, body=body))

    configs = _collect_configs(evidence)
    if configs:
        body = ["Exact configuration values shown in the source:",
                "```ini", *configs, "```"]
        article.sections.append(Section(heading="Configuration values", level=2, body=body))

    expected = _collect_expected(procedure, evidence)
    if expected:
        body = [f"- {e}" for e in expected]
        article.sections.append(Section(heading="Expected outcome", level=2, body=body))

    troubleshooting = _collect_troubleshooting(evidence)
    if troubleshooting:
        body = []
        for item in troubleshooting:
            body.append(f"**Problem:** {item['problem']}")
            if item.get("resolution"):
                body.append(f"**What the source does about it:** {item['resolution']}")
        article.sections.append(Section(heading="Troubleshooting", level=2, body=body))

    if procedure.alternatives:
        body = [f"- {a}" for a in procedure.alternatives]
        article.sections.append(Section(heading="Alternative approaches", level=2, body=body))

    if procedure.corrections:
        body = []
        for c in procedure.corrections:
            body.append(f"> {c}")
        article.sections.append(Section(heading="Corrections in the source", level=2, body=body))

    unresolved = list(procedure.unresolved) + _unresolved_evidence(evidence)
    if unresolved:
        body = ["These points could not be determined from the source and should be "
                "verified against the official documentation:"]
        body += [f"- {u}" for u in unresolved]
        article.sections.append(Section(heading="Unresolved details", level=2, body=body))

    if procedure.limitations:
        body = [f"- {l}" for l in procedure.limitations]
        article.sections.append(Section(heading="Limitations", level=2, body=body))

    gallery = _gallery_section(procedure, evidence, frames, url)
    if gallery.body:
        article.sections.append(gallery)

    article.sections.append(_provenance_section(ctx, article))


def _cite_comment(*, step: int | str, ev_ids: Sequence[str],
                  t_min: float | None, t_max: float | None,
                  frame_ids: Sequence[str] = ()) -> str:
    """Machine-readable provenance binding (M7-A §4).

    Human readers use the visible *Source:* timestamp link; the verifier and
    auditors use this comment, which carries the exact evidence IDs, the
    timestamp range and the frame IDs.  It survives paraphrasing, splitting,
    merging and reordering because it travels with the block it describes.
    """
    ev = ",".join(ev_ids) if ev_ids else "-"
    fr = ",".join(frame_ids) if frame_ids else "-"
    span = "-" if t_min is None else (
        f"{t_min:.1f}-{t_max:.1f}" if t_max is not None and t_max != t_min
        else f"{t_min:.1f}")
    return f"<!-- cite: step={step} ev={ev} t={span} frames={fr} -->"


def _render_step(
    step,
    evidence: Sequence[Evidence],
    frames: dict[str, dict[str, Any]],
    url: str | None,
) -> str:
    index = {e.evidence_id: e for e in evidence}
    lines = [f"{step.position}. **{step.text}**"]
    if step.detail:
        detail = step.detail
        cmds = _commands_in_text(detail)
        rest = _strip_command_narration(detail, cmds)
        if rest:
            lines.append(f"   {_indent(rest)}")
        if cmds:
            lines.append("   ```bash")
            lines.extend(f"   {c}" for c in cmds)
            lines.append("   ```")
    refs = [index[i] for i in step.evidence_ids if i in index]
    if refs:
        times = [e.t_start for e in refs if e.t_start is not None]
        t_min = min(times) if times else None
        t_max = max(times) if times else None
        link = timestamp_url(url, t_min)
        if link:
            lines.append(f"   *Source: [{format_ts(t_min)}]({link})*")
        elif t_min is not None:
            # no URL (local file): the timestamp is still the evidence anchor
            lines.append(f"   *Source: {format_ts(t_min)} of the source video*")
        lines.append("   " + _cite_comment(
            step=step.position,
            ev_ids=[e.evidence_id for e in refs],
            t_min=t_min, t_max=t_max,
            frame_ids=list(step.frame_ids or [])))
    for w in step.warnings:
        cleaned = " ".join(str(w).split()).strip(" .;:-")
        # an evidence id is a citation, not a warning the reader can act on
        if len(cleaned) < 8 or re.fullmatch(r"ev-[0-9a-f]{4,}", cleaned):
            continue
        lines.append(f"   > **Warning** — {cleaned}")
    if step.expected_result:
        lines.append(f"   *Expected:* {step.expected_result}")
    for alt in step.alternatives:
        lines.append(f"   *Alternative:* {alt}")
    for fid in step.frame_ids:
        frame = frames.get(fid)
        if frame is None:
            continue
        ref = _frame_ref(frame, evidence, fid)
        link = timestamp_url(url, frame["t"])
        if link:
            caption = ref.caption + f" — source at [{format_ts(frame['t'])}]({link})"
        else:
            caption = ref.caption + f" — source at {format_ts(frame['t'])} of the source video"
        lines.append(f"   ![{_alt(ref.caption)}](assets/{fid}.jpg)\n\n   *{caption}*")
    return "\n".join(lines)


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


def _alt(caption: str) -> str:
    """Sanitise a caption for use as Markdown alt text (never HTML-escaped)."""
    text = " ".join(str(caption).split())[:160]
    return text.replace("]", ")").replace("[", "(")


def _frame_caption(frame: dict[str, Any]) -> str:
    """Use the frame's own description, never its raw model reply."""
    summary = frame.get("summary") or ""
    if not isinstance(summary, str) or not summary.strip() or summary.lstrip().startswith("{"):
        return "Frame from the source video"
    return summary.strip()[:300]


def _frame_ref(frame, evidence, fid) -> FrameRef:
    best = None
    for e in evidence:
        if fid in e.frame_ids and (e.kind in ("action", "command", "config", "measurement")):
            best = e
            break
    caption = _frame_caption(frame)
    if best is not None:
        caption = f"{caption} — {best.text[:160]}"
    return FrameRef(frame_id=fid, path=frame["path"], caption=caption, t=frame["t"])


def _collect_warnings(procedure: Procedure,
                      evidence: Sequence[Evidence]) -> list[tuple[str, list[str]]]:
    """Collected warnings with the evidence IDs behind each one.

    Step warnings inherit their step's evidence; evidence-kind warnings
    carry their own record.  Provenance travels with the text so the
    verifier never has to guess which observation a callout came from.
    """
    out: list[tuple[str, list[str]]] = []

    def push(text: str, ev_ids: Sequence[str]) -> None:
        cleaned = " ".join(str(text).split()).strip(" .;:-")
        if len(cleaned) < 8:
            return
        # an evidence id is a citation, not a warning
        if re.fullmatch(r"ev-[0-9a-f]{4,}", cleaned):
            return
        key = cleaned.lower().rstrip(".")
        if any(existing.lower().rstrip(".") == key for existing, _ in out):
            return
        out.append((cleaned, [i for i in ev_ids]))

    index = {e.evidence_id: e for e in evidence}
    for step in procedure.steps:
        for w in step.warnings:
            push(w, [i for i in step.evidence_ids if i in index])
    for e in evidence:
        if e.kind == "warning":
            push(e.text, [e.evidence_id])
    return out


_CUE_NOISE = re.compile(
    r"^(?:\s*(?:the\s+command|the\s+commands|command|commands|run|running|type|typing|"
    r"execute|executing)[:,]?\s*)+", re.IGNORECASE)
_NARRATION_ONLY = {
    "run", "running", "the", "command", "commands", "type", "typing", "enter",
    "execute", "executing", "and", "then", "use", "using", "it", "this", "that",
    "following", "as", "via", "with", "to", "of",
}


def _strip_command_narration(detail: str, commands: Sequence[str]) -> str:
    """Remove the extracted commands and the narration that only introduced them."""
    rest = detail
    for cmd in commands:
        rest = rest.replace(cmd, " ")
    rest = rest.strip(" \n\t-")
    for _ in range(3):
        stripped = _CUE_NOISE.sub("", rest, count=1).strip()
        if not stripped or stripped == rest:
            break
        rest = stripped
    words = re.findall(r"[A-Za-z0-9_'./-]+", rest)
    if words and all(w.lower() in _NARRATION_ONLY for w in words):
        return ""
    return rest.strip(" \n\t")


def _commands_in_text(text: str) -> list[str]:
    from ...literals import _command_fragments

    out = []
    for line in text.splitlines():
        out.extend(_command_fragments(line))
    return out


def _collect_commands(
    ctx: StageContext, evidence: Sequence[Evidence], procedure: Procedure
) -> list[str]:
    """Commands the article may show: only those with an evidence record.

    A command with no observation behind it is an unsupported actionable claim,
    so it is suppressed here (and counted) rather than published and then
    flagged by the verifier.
    """
    supported: list[str] = []
    for e in evidence:
        if e.kind not in ("command", "code"):
            continue
        supported.extend(lit.literal for lit in e.literals if lit.kind == "command")
        if e.kind == "command":
            supported.append(e.text)
        for frag in _command_fragments(e.text):
            supported.append(frag)

    cmds: list[str] = []
    seen: set[str] = set()
    for candidate in supported:
        for frag in _command_fragments(candidate):
            key = normalize_value(frag).lower()
            if key in seen:
                continue
            seen.add(key)
            cmds.append(frag)
    suppressed = [
        c
        for step in procedure.steps
        for c in _commands_in_text(step.detail) + _commands_in_text(step.text)
        if not _values_supported(c, supported)
    ]
    if suppressed:
        ctx.progress(
            f"suppressed {len(suppressed)} command(s) with no supporting observation"
        )
        ctx.artifacts["suppressed_commands"] = [s[:120] for s in suppressed[:20]]
        ctx.artifacts["suppressed_command_count"] = len(suppressed)
    return cmds


def _values_supported(candidate: str, supported: Sequence[str]) -> bool:
    return any(values_match(candidate, s) for s in supported)


def _collect_configs(evidence: Sequence[Evidence]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for e in evidence:
        if e.kind in ("config", "measurement", "identifier"):
            for lit in e.literals:
                if lit.kind in ("keyvalue", "measurement", "version", "path", "flag", "port",
                                "ip_address", "mac_address", "uuid"):
                    val = lit.literal if lit.kind == "keyvalue" else f"{lit.literal}"
                    key = normalize_value(val).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(val)
    return out[:60]


def _collect_expected(procedure: Procedure, evidence: Sequence[Evidence]) -> list[str]:
    out = [s.expected_result for s in procedure.steps if s.expected_result.strip()]
    for e in evidence:
        if e.kind == "expected_result":
            out.append(e.text)
    seen: set[str] = set()
    deduped = []
    for o in out:
        key = normalize_value(o).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    return deduped


def _collect_troubleshooting(evidence: Sequence[Evidence]) -> list[dict[str, str]]:
    errors = [e for e in evidence if e.kind in ("error", "correction")]
    out: list[dict[str, str]] = []
    for e in errors:
        out.append({"problem": e.text, "resolution": _find_resolution(e, evidence)})
    return out


def _find_resolution(error: Evidence, evidence: Sequence[Evidence]) -> str:
    for e in evidence:
        if e.kind == "correction" and abs((e.t_start or 0) - (error.t_start or 0)) < 90:
            return e.text
    return ""


def _unresolved_evidence(evidence: Sequence[Evidence]) -> list[str]:
    return [e.text for e in evidence if e.kind == "unresolved"]


def _gallery_section(
    procedure: Procedure, evidence: Sequence[Evidence], frames: dict[str, dict[str, Any]],
    url: str | None,
) -> Section:
    used: set[str] = set()
    for s in procedure.steps:
        used.update(s.frame_ids)
    ranked = sorted(
        (f for f in frames.values() if f["frame_id"] not in used),
        key=lambda f: _frame_score(f, evidence),
        reverse=True,
    )
    body: list[str] = []
    refs: list[FrameRef] = []
    for f in ranked[:8]:
        best = _best_evidence_for_frame(f["frame_id"], evidence)
        caption = _frame_caption(f)
        if best is not None:
            caption = f"{caption} — {best.text[:200]}"
        link = timestamp_url(url, f["t"])
        cap = caption + (
            f" — source at [{format_ts(f['t'])}]({link})" if link
            else f" — source at {format_ts(f['t'])} of the source video"
        )
        body.append(f"![{_alt(caption)}](assets/{f['frame_id']}.jpg)\n\n*{cap}*")
        refs.append(FrameRef(frame_id=f["frame_id"], path=f["path"], caption=caption, t=f["t"]))
    if not body:
        return Section(heading="", level=2, body=[])
    return Section(heading="Other useful frames", level=2, body=body, frames=refs)


def article_frames(markdown: str) -> list[str]:
    """Frame filenames the article references."""
    return re.findall(r"\]\(assets/([^)]+)\)", markdown)


def _frame_score(frame: dict[str, Any], evidence: Sequence[Evidence]) -> int:
    score = 0
    for e in evidence:
        if frame["frame_id"] in e.frame_ids:
            score += 3 if e.kind in ("action", "command", "config", "measurement") else 1
    return score


def _best_evidence_for_frame(fid: str, evidence: Sequence[Evidence]) -> Evidence | None:
    best = None
    best_rank = -1
    rank = {"command": 5, "config": 5, "measurement": 4, "action": 4, "code": 4, "identifier": 3}
    for e in evidence:
        if fid in e.frame_ids:
            r = rank.get(e.kind, 1)
            if r > best_rank:
                best, best_rank = e, r
    return best


def _provenance_section(ctx: StageContext, article: Article) -> Section:
    prov = article.provenance
    body = [
        f"- **Source:** {prov.get('title') or 'unknown'}"
        + (f" by {prov['uploader']}" if prov.get("uploader") else ""),
        (f"- **Source URL:** {prov.get('url')}" if prov.get("url")
         else f"- **Source file:** {prov.get('local_file') or 'local video file'}"),
        f"- **Compiled with local profile:** `{prov.get('profile')}`",
        f"- **Configuration fingerprint:** `{prov.get('fingerprint')}`",
        "- **Pipeline:** local speech recognition, frame selection, visual "
        "interpretation, evidence extraction, procedure reconstruction and "
        "verification. No external inference service was used.",
    ]
    if prov.get("retrieved_at"):
        import datetime

        body.append(
            "- **Source retrieved:** "
            + datetime.datetime.utcfromtimestamp(float(prov["retrieved_at"])).isoformat() + "Z"
        )
    return Section(heading="Source and provenance", level=2, body=body)


# ----------------------------------------------------------------------
def _store(ctx: StageContext, article: Article, article_id: str, md: str) -> None:
    from ...article import markdown_to_html, render_html

    ctx.paths.article_dir(article_id).mkdir(parents=True, exist_ok=True)
    md_path = ctx.paths.article_markdown(article_id)
    assets = ctx.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    body_html = markdown_to_html(md)
    html = render_html(article, body_html, embed_images=False, asset_dir=assets)
    md_path.write_text(md, encoding="utf-8")
    ctx.paths.article_html(article_id).write_text(html, encoding="utf-8")
    outline = [{"heading": s.heading, "level": s.level} for s in article.sections if s.heading != "__skip__"]
    with ctx.db.transaction() as conn:
        conn.execute(
            """INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version,
                                     status, fingerprint, outline_json, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?, 'draft',?,?,?,?)
               ON CONFLICT(article_id) DO UPDATE SET
                 title=excluded.title, markdown=excluded.markdown, html=excluded.html,
                 version=excluded.version, fingerprint=excluded.fingerprint,
                 outline_json=excluded.outline_json, updated_at=excluded.updated_at""",
            (
                article_id, ctx.source_id, ctx.job_id, article.title, md, html, 1,
                ctx.settings.fingerprint(), json_dumps(outline), time.time(), time.time(),
            ),
        )
        conn.execute("DELETE FROM articles_fts WHERE article_id=?", (article_id,))
        conn.execute(
            "INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
            (article_id, article.title, md),
        )
    _copy_assets(ctx, article_id, article, md)
    ctx.artifacts["article_html_chars"] = len(html)
    ctx.record_artifact_hash("markdown", str(md_path))


def _copy_assets(ctx: StageContext, article_id: str, article: Article, md: str) -> None:
    import re
    import shutil

    assets = ctx.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    used = re.findall(r"\]\(assets/([^)]+)\)", md)
    for name in used:
        fid = name.rsplit(".", 1)[0]
        row = ctx.db.query_one("SELECT path FROM frames WHERE frame_id=?", (fid,))
        if not row:
            continue
        src = Path(row["path"])
        if src.is_file():
            shutil.copy2(src, assets / name)
            ctx.db.execute(
                """INSERT OR REPLACE INTO article_assets(asset_id, article_id, frame_id, path, caption, alt_text, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (f"{article_id}-asset-{fid}", article_id, fid, str(assets / name), "", "", time.time()),
            )
