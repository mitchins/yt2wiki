"""Stage 7 — procedure reconstruction.

Turns the evidence pile into logical instructional order: prerequisites before
dependents, failed attempts separated from the working procedure, alternatives
and corrections preserved rather than merged away.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Sequence

from ...backends.loader import ModelManager
from ...records import Evidence, Procedure, ProcedureStep
from .. import StageContext, CancelRequested

log = logging.getLogger("yt2wiki.stage.procedures")

PROMPT_VERSION = "v3"
SYSTEM = (
    "You reconstruct reproducible procedures from observed evidence. "
    "You never invent steps. You keep corrections and alternatives visible "
    "instead of merging them into a single misleading instruction."
)

PROCEDURE_PROMPT = """Below is the consolidated evidence extracted from one tutorial video.

Rebuild it as a procedure a reader could follow WITHOUT watching the video.

Return JSON:
{{
  "title": "short imperative title for the procedure",
  "summary": "1-3 sentences: what this achieves and when it applies",
  "prerequisites": ["things the reader must have/know before starting"],
  "steps": [
    {{
      "text": "imperative instruction, one sentence",
      "detail": "how, exactly, including exact values/commands, or ''",
      "evidence_ids": ["ev-..."],
      "frame_ids": ["f-..."],
      "warnings": ["..."],
      "expected_result": "what should happen, or ''",
      "alternatives": ["other valid way stated by the source, or ''"]
    }}
  ],
  "alternatives": ["distinct alternative approaches mentioned by the source"],
  "corrections": ["places where the source revised a value or fixed an error - describe both versions"],
  "unresolved": ["things the reader cannot determine from the source"],
  "limitations": ["when this procedure will not apply"]
}}

Rules:
- Every step MUST cite at least one evidence_id from the list below.
- Order steps so prerequisites come first and dependent operations follow.
- If the source only explains something without demonstrating it, do NOT create
  a step for it; put it in "limitations" or "unresolved" instead.
- Keep exact values, commands and paths verbatim.
- Do not add steps the evidence does not support.

EVIDENCE:
{evidence}

{overflow}

Return only JSON."""


def _overflow_index(items: Sequence[Evidence], digest: Sequence[dict], *, limit: int) -> str:
    """Tell the model what exists beyond the digest, so it is not blind."""
    shown = {d["evidence_id"] for d in digest}
    extra = [e for e in items if e.evidence_id not in shown]
    if not extra:
        return "None: every observation is included above."
    lines = [
        f"{len(extra)} further observations exist beyond the detail above "
        f"(ids {extra[0].evidence_id}..{extra[-1].evidence_id}, "
        f"{extra[0].t_start:.0f}s-{extra[-1].t_start:.0f}s). "
        "Their kinds: " + ", ".join(f"{k}×{sum(1 for e in extra if e.kind == k)}"
                                    for k in sorted({e.kind for e in extra})) + "."
    ]
    return " ".join(lines)


EXTEND_PROMPT = """Here is a procedure reconstructed from a tutorial, and a list of critical
observations from the same source that NO step in the procedure cites.

Extend the procedure so those observations are covered. Rules:
- only add or amend steps supported by the listed observations,
- cite the observation id in "evidence_ids",
- keep existing steps that are already supported,
- never invent a step for an observation that is merely descriptive,
- keep corrections and alternatives visible.

CURRENT PROCEDURE:
{procedure}

UNCOVERED OBSERVATIONS:
{uncovered}

Return the COMPLETE updated procedure as JSON in the same shape."""


def _extend_with_uncovered(
    ctx: StageContext, backend, procedure: Procedure, items: Sequence[Evidence],
    *, max_new: int, temperature: float, limit: int = 40,
) -> int:
    from ...records import CRITICAL_KINDS

    referenced = set()
    for step in procedure.steps:
        referenced.update(step.evidence_ids)
    uncovered = [
        e for e in items
        if e.evidence_id not in referenced and e.kind in CRITICAL_KINDS
    ][:limit]
    if not uncovered:
        return 0
    digest = [
        {"evidence_id": e.evidence_id, "kind": e.kind, "text": e.text,
         "t_start": e.t_start, "literals": [lit.literal for lit in e.literals][:6],
         "frame_ids": e.frame_ids[:3]}
        for e in uncovered
    ]
    try:
        payload = backend.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": EXTEND_PROMPT.format(
                 procedure=json.dumps(procedure.to_dict(), ensure_ascii=False)[:18000],
                 uncovered=json.dumps(digest, ensure_ascii=False)[:12000])}],
            max_new_tokens=max_new + 1024,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("coverage extension failed: %s", exc)
        return 0
    extended = _parse(ctx, payload)
    if not extended.steps:
        return 0
    added = len(extended.steps) - len(procedure.steps)
    procedure.steps = extended.steps
    for key in ("prerequisites", "alternatives", "corrections", "unresolved", "limitations"):
        merged = list(getattr(extended, key))
        for value in getattr(procedure, key):
            if value not in merged:
                merged.append(value)
        setattr(procedure, key, merged)
    if extended.title:
        procedure.title = extended.title
    if extended.summary:
        procedure.summary = extended.summary
    return added


def run(ctx: StageContext) -> dict[str, Any]:
    started = time.time()
    t = ctx.settings.tuning
    items = _load_evidence(ctx)
    if not items:
        raise RuntimeError("no evidence stored; run the evidence stage first")

    ctx.artifacts["procedure_input"] = len(items)
    manager = ModelManager(ctx.settings, recorder=_recorder(ctx))
    procedure: Procedure | None = None
    with manager.acquire("text") as backend:
        digest = _digest(items, limit=int(t.procedure_digest_limit))
        overflow = _overflow_index(items, digest, limit=int(t.procedure_digest_limit))
        try:
            payload = backend.chat_json(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": PROCEDURE_PROMPT.format(
                     evidence=json.dumps(digest, ensure_ascii=False)[:26000],
                     overflow=overflow)}],
                max_new_tokens=int(t.text_max_tokens) + 1024,
                temperature=t.text_temperature,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("procedure reconstruction failed")
            raise RuntimeError(f"procedure reconstruction failed: {exc}") from exc
        procedure = _parse(ctx, payload)

        # Evidence that no step cites is a coverage gap the reader would feel.
        # Ask once, bounded, to extend the procedure with it.
        added = _extend_with_uncovered(
            ctx, backend, procedure, items, max_new=int(t.text_max_tokens),
            temperature=t.text_temperature,
        )

    _validate_against_evidence(procedure, items)
    _persist(ctx, procedure)
    ctx.artifacts["procedure"] = procedure.to_dict()
    out = _write_json(ctx, procedure)
    ctx.record_artifact_hash("procedure", str(out))
    return {
        "steps": len(procedure.steps),
        "prerequisites": len(procedure.prerequisites),
        "alternatives": len(procedure.alternatives),
        "corrections": len(procedure.corrections),
        "unresolved": len(procedure.unresolved),
        "evidence_items": len(items),
        "unsupported_steps_dropped": procedure.to_dict().get("_unsupported_steps", []),
        "demoted_to_unresolved": len(procedure.unresolved),
        "steps_added_by_coverage_pass": added,
        "elapsed_s": round(time.time() - started, 3),
    }


# ----------------------------------------------------------------------
def _recorder(ctx: StageContext):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", "procedures")
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


def _load_evidence(ctx: StageContext) -> list[Evidence]:
    rows = ctx.db.query(
        "SELECT * FROM evidence WHERE source_id=? ORDER BY t_start", (ctx.source_id,)
    )
    return [Evidence.from_row(r) for r in rows]


def _digest(items: Sequence[Evidence], *, limit: int) -> list[dict[str, Any]]:
    out = []
    for e in items[:limit]:
        out.append(
            {
                "evidence_id": e.evidence_id,
                "kind": e.kind,
                "text": e.text,
                "t_start": e.t_start,
                "t_end": e.t_end,
                "frame_ids": e.frame_ids[:3],
                "literals": [lit.literal for lit in e.literals][:8],
            }
        )
    return out


_LIST_FIELDS = ("prerequisites", "alternatives", "corrections", "unresolved", "limitations")


def _parse_step(i: int, raw: Any) -> ProcedureStep | None:
    """One writer step; None when it is not a usable instruction."""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text", "")).strip()
    if len(text) < 4:
        return None
    return ProcedureStep(
        position=i + 1,
        text=text,
        detail=str(raw.get("detail") or "").strip(),
        evidence_ids=[str(x) for x in (raw.get("evidence_ids") or [])],
        frame_ids=[str(x) for x in (raw.get("frame_ids") or [])],
        warnings=[str(x) for x in (raw.get("warnings") or []) if str(x).strip()],
        expected_result=str(raw.get("expected_result") or "").strip(),
        alternatives=[str(x) for x in (raw.get("alternatives") or []) if str(x).strip()],
    )


def _parse(ctx: StageContext, payload: dict[str, Any]) -> Procedure:
    pid = ctx.source_id
    procedure = Procedure(
        procedure_id=pid,
        title=str(payload.get("title") or "Procedure").strip()[:160],
        summary=str(payload.get("summary") or "").strip()[:1200],
    )
    for i, raw in enumerate(payload.get("steps") or []):
        step = _parse_step(i, raw)
        if step is not None:
            procedure.steps.append(step)
    for key in _LIST_FIELDS:
        procedure.__setattr__(
            key, [str(x).strip() for x in (payload.get(key) or []) if str(x).strip()]
        )
    return procedure


def _validate_against_evidence(procedure: Procedure, items: Sequence[Evidence]) -> None:
    """Drop any step that cannot point at an observation that exists.

    Non-negotiable #1: a claim with no evidence record is a bug.  Rather than
    deleting the step silently, it is moved into "unresolved" so the reader is
    told the source demonstrated something the extractor could not pin down.
    """
    # Only observations anchored to a timestamp can support an instruction:
    # a claim must be traceable to a *span* of the source.
    valid = {e.evidence_id for e in items if e.t_start is not None}
    kept: list[Any] = []
    dropped: list[str] = []
    demoted: list[str] = []
    for step in procedure.steps:
        step.evidence_ids = [e for e in step.evidence_ids if e in valid]
        if step.evidence_ids and not any(
            e.evidence_id in step.evidence_ids and e.t_start is not None for e in items
        ):
            step.evidence_ids = []
        step.frame_ids = [f for f in step.frame_ids if str(f).startswith("f-")]
        if not step.evidence_ids:
            dropped.append(f"{step.position}: {step.text[:140]}")
            demoted.append(
                f"The source demonstrates \"{step.text[:160]}\" but no timestamped "
                "observation was recorded for it, so it is not stated as an instruction."
            )
            continue
        kept.append(step)
    if demoted:
        procedure.steps = kept
        for i, step in enumerate(procedure.steps, start=1):
            step.position = i
        for note in demoted:
            if note not in procedure.unresolved:
                procedure.unresolved.append(note)
    procedure.__dict__["_unsupported_steps"] = dropped


def _persist(ctx: StageContext, procedure: Procedure) -> None:
    from ...db.schema import json_dumps

    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO procedures(procedure_id, source_id, title, summary, created_at) "
            "VALUES(?,?,?,?,?)",
            (procedure.procedure_id, ctx.source_id, procedure.title, procedure.summary, time.time()),
        )
        conn.execute("DELETE FROM procedure_steps WHERE procedure_id=?", (procedure.procedure_id,))
        for step in procedure.steps:
            conn.execute(
                "INSERT INTO procedure_steps VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{procedure.procedure_id}:{step.position}", procedure.procedure_id,
                    step.position, step.text, step.detail, json_dumps(step.evidence_ids),
                    json_dumps(step.frame_ids), json_dumps(step.warnings),
                    step.expected_result, json_dumps(step.alternatives), time.time(),
                ),
            )


def _write_json(ctx: StageContext, procedure: Procedure) -> Path:
    out = ctx.work_dir("procedures") / "procedure.json"
    out.write_text(json.dumps(procedure.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.artifacts["procedure_json"] = str(out)
    return out
