"""Typed article contracts, v1 (PR A).

The catalogue of article *types* an autonomous editorial pass may choose
from. Types are article shapes, not YouTube genres: the source's venue
never decides the type. Names, slots and the template version are
immutable at runtime — the agent selects, never invents.

``no_article`` is a *result*, not an eighth type: it covers music-only /
no-speech sources, unusable evidence, and cases where no safe article
can be composed.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_VERSION = 1
SCHEMA_VERSION = 1

#: The closed set of selectable article types.
ARTICLE_TYPES = (
    "procedure",
    "recipe",
    "report",
    "explainer",
    "review",
    "discussion",
    "experiment",
)

#: Refusal result: meaningful when no type can be safely composed.
NO_ARTICLE = "no_article"

#: Reader-facing badge labels. Old V9 `kind` values map onto these where
#: the meaning is stable; anything else renders as a neutral "Article".
READER_LABELS = {
    "procedure": "How-to",
    "recipe": "Recipe",
    "report": "Report",
    "explainer": "Explainer",
    "review": "Review",
    "discussion": "Discussion",
    "experiment": "Experiment",
}

#: Legacy `v9_write` kind values that resolve to a typed label for
#: articles published before typed selection existed. `experimental` is
#: the historical read-compatible alias of `experiment`. `comparison`
#: and `teardown` were patterns *within* review/explainer/experiment,
#: so they resolve to no fabricated type (neutral label).
KIND_FALLBACK = {
    "procedure": "procedure",
    "explainer": "explainer",
    "experimental": "experiment",
    "experiment": "experiment",
    "review": "review",
}

_TYPE_DEFS: dict[str, dict[str, Any]] = {
    "procedure": {
        "reader_promise": "A reproducible task",
        "core_slots": ("goal", "steps"),
        "optional_slots": ("requirements", "verification", "alternatives",
                            "warnings", "failed_paths", "troubleshooting"),
        "disallowed": ("commentary_as_instruction",
                       "broll_as_instruction",
                       "speculative_suggestion_as_instruction",
                       "misordered_dependent_steps"),
        "lead_block": None,
    },
    "recipe": {
        "reader_promise": "Cookable, complete dish or dishes",
        "core_slots": ("dish", "ingredients", "method"),
        "optional_slots": ("yield", "prep_time", "cook_time", "equipment",
                            "temperature", "technique", "substitutions",
                            "storage", "variants"),
        "disallowed": ("guessed_quantities", "guessed_units",
                       "guessed_timing", "guessed_temperature",
                       "merged_dishes"),
        "lead_block": "ingredient_list",
    },
    "report": {
        "reader_promise": "What happened, what is known, and when",
        "core_slots": ("lede", "verified_facts", "as_of"),
        "optional_slots": ("timeline", "participants", "causes",
                            "responses", "uncertainty", "consequences"),
        "disallowed": ("instructions", "graphics_as_steps",
                       "hindsight_as_contemporary"),
        "lead_block": "key_facts",
    },
    "explainer": {
        "reader_promise": "Understand a subject or mechanism",
        "core_slots": ("answer", "explanation"),
        "optional_slots": ("background", "evidence", "comparisons",
                            "mechanisms", "tradeoffs", "limits",
                            "implications"),
        "disallowed": ("fake_procedure", "invented_causal_certainty",
                       "bare_labels_as_values"),
        "lead_block": "key_facts",
    },
    "review": {
        "reader_promise": "An evaluation of a product or service",
        "core_slots": ("subject", "assessment"),
        "optional_slots": ("verdict", "test_setup", "claims_vs_tests",
                            "strengths", "limitations", "comparisons",
                            "audience", "value"),
        "disallowed": ("opinion_as_independent_fact",
                       "demo_as_tutorial", "invented_score"),
        "lead_block": "verdict",
    },
    "discussion": {
        "reader_promise": "Understand distinct viewpoints and disagreements",
        "core_slots": ("central_question", "positions"),
        "optional_slots": ("participants", "agreements", "disputes",
                            "arguments", "outcomes", "open_questions"),
        "disallowed": ("sponsor_as_topic", "sponsor_as_instruction",
                       "imaginary_attribution", "false_consensus"),
        "lead_block": "position_list",
    },
    "experiment": {
        "reader_promise": "Understand a test and what it establishes",
        "core_slots": ("question", "method", "results"),
        "optional_slots": ("conditions", "measurements", "failure_modes",
                            "analysis", "limitations", "reproducibility"),
        "disallowed": ("guaranteed_procedure", "overclaimed_results"),
        "lead_block": "measured_comparison",
    },
}


def type_definition(template_id: str) -> dict[str, Any] | None:
    """The immutable v1 contract for one type, or None when unknown.

    Returns a copy: the catalogue itself can never be mutated through
    this accessor at runtime.
    """
    import copy

    tdef = _TYPE_DEFS.get(template_id)
    return copy.deepcopy(tdef) if tdef is not None else None


def reader_label(template_id: str | None) -> str:
    """Badge text for a typed article; neutral for unknown/legacy."""
    if template_id in READER_LABELS:
        return READER_LABELS[template_id]
    return "Article"


def resolve_legacy_kind(kind: str | None) -> str | None:
    """Map a pre-typed V9 `kind` to an article type for display only.

    Returns None when the old label names no type, so the reader shows
    the neutral label instead of a fabricated badge. Never rewrites
    stored content.
    """
    if not kind:
        return None
    return KIND_FALLBACK.get(str(kind).strip().lower())


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_selection(decision: Any) -> list[str]:
    """Validate an autonomous type decision against the closed enum.

    Returns a list of human-readable problems (empty == valid). Pure
    function of the decision dict; never touches the network or models.
    """
    errors: list[str] = []
    if not isinstance(decision, dict):
        return ["selection must be a JSON object"]
    if not _is_int(decision.get("schema_version")) \
            or decision.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    tid = decision.get("template_id")
    if tid == NO_ARTICLE:
        if decision.get("status") != "refused":
            errors.append("no_article result requires status 'refused'")
        return errors
    if tid not in ARTICLE_TYPES:
        errors.append(f"template_id must be one of {', '.join(ARTICLE_TYPES)}")
    if not _is_int(decision.get("template_version")) \
            or decision.get("template_version") != TEMPLATE_VERSION:
        errors.append(f"template_version must be {TEMPLATE_VERSION}")
    if decision.get("selection_mode") not in ("auto", "override"):
        errors.append("selection_mode must be 'auto' or 'override'")
    if not isinstance(decision.get("rationale"), str) or \
            not decision["rationale"].strip():
        errors.append("rationale must be a non-empty string")
    wins = decision.get("supporting_windows")
    if not _valid_window_list(wins):
        errors.append("supporting_windows must be a non-empty [start, end] list")
    excl = decision.get("excluded_windows") or []
    if not isinstance(excl, list):
        errors.append("excluded_windows must be a list")
    else:
        for w in excl:
            if not isinstance(w, dict) or not _is_num(w.get("start")) \
                    or not _is_num(w.get("end")) or w["end"] <= w["start"] \
                    or w["start"] < 0 or not isinstance(w.get("reason"), str) \
                    or not w["reason"].strip():
                errors.append("excluded_windows entries need start/end with "
                              "0 <= start < end plus a reason")
                break
    if decision.get("status") not in ("selected", "needs_review", "refused"):
        errors.append("status must be 'selected', 'needs_review' or 'refused'")
    alts = decision.get("alternatives_rejected") or []
    if not isinstance(alts, list):
        errors.append("alternatives_rejected must be a list")
    else:
        for alt in alts:
            if not isinstance(alt, dict) \
                    or alt.get("type") not in (*ARTICLE_TYPES, NO_ARTICLE):
                errors.append("alternatives_rejected entries need a known type")
                break
    return errors


def _valid_window_list(value: Any) -> bool:
    """A non-empty list of [start, end] evidence spans.

    Empty lists are invalid: a section with no evidence span contradicts
    the evidence-grounding rule, so it fails here rather than downstream.
    """
    if not isinstance(value, list) or not value:
        return False
    for w in value:
        if (not isinstance(w, (list, tuple)) or len(w) != 2
                or not all(_is_num(v) and v >= 0 for v in w) or w[1] <= w[0]):
            return False
    return True


def validate_plan(plan: Any, template_id: str) -> list[str]:
    """Validate an editorial plan against the frozen type's slot contract.

    The plan's own template_id must equal the frozen decision: a plan
    claiming another type is a silent retype and fails. Required (core)
    slots must be covered by at least one section each; every section
    slot must be allowed for the type; section identity must be stable
    (not derived from heading text alone); windows must be non-empty,
    well-formed [start, end] lists. Returns problems, empty == valid.
    """
    errors: list[str] = []
    tdef = _TYPE_DEFS.get(template_id)
    if tdef is None:
        return [f"unknown template_id {template_id!r}"]
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    if plan.get("template_id") != template_id:
        errors.append(f"plan template_id {plan.get('template_id')!r} does not "
                      f"match frozen type {template_id!r}")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        return errors + ["plan.sections must be a non-empty list"]
    allowed = set(tdef["core_slots"]) | set(tdef["optional_slots"])
    seen_ids: set[str] = set()
    covered: set[str] = set()
    for i, sec in enumerate(sections):
        where = f"sections[{i}]"
        if not isinstance(sec, dict):
            errors.append(f"{where} must be an object")
            continue
        sid = sec.get("section_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{where}.section_id must be a non-empty string")
        elif sid in seen_ids:
            errors.append(f"duplicate section_id {sid!r}")
        else:
            seen_ids.add(sid)
        slot = sec.get("slot")
        if not isinstance(slot, str) or slot not in allowed:
            errors.append(f"{where}.slot {slot!r} is not allowed for {template_id}")
        else:
            covered.add(slot)
        if not _valid_window_list(sec.get("source_windows")):
            errors.append(f"{where}.source_windows must be a non-empty [start, end] list")
        if not isinstance(sec.get("heading"), str) or not sec["heading"].strip():
            errors.append(f"{where}.heading must be a non-empty string")
    for core in tdef["core_slots"]:
        if core not in covered:
            errors.append(f"core slot {core!r} has no covering section")
    cov = plan.get("coverage") or {}
    if not isinstance(cov, dict):
        errors.append("coverage must be an object")
    else:
        for key in ("included_window_ids", "excluded_window_ids",
                    "unassigned_substantive_window_ids"):
            if key in cov and not isinstance(cov[key], list):
                errors.append(f"coverage.{key} must be a list")
    return errors
