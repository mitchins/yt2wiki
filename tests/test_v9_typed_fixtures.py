"""Synthetic editorial fixtures (handoff acceptance matrix, offline).

Each fixture pins a design decision at the contract layer: valid shapes
pass, invalid shapes fail with the expected gate reason. Model quality
(adjudication against real sources) is measured in the live matrix, not
here.
"""

from __future__ import annotations

from yt2wiki.v9 import article_types as AT
from yt2wiki.v9 import blocks as B


def _plan(tid, slots):
    return {"template_id": tid,
            "sections": [{"section_id": f"sec-{i}", "slot": slot,
                          "heading": f"H{i}",
                          "source_windows": [[float(i * 10), float(i * 10 + 5)]]}
                         for i, slot in enumerate(slots)],
            "coverage": {}}


def test_multi_recipe_two_scoped_units_pass():
    plan = _plan("recipe", ["dish", "ingredients", "method"])
    plan["sections"] += [{"section_id": "sec-3", "slot": "dish",
                          "heading": "Second dish",
                          "source_windows": [[60.0, 70.0]]},
                         {"section_id": "sec-4", "slot": "ingredients",
                          "heading": "Second measures",
                          "source_windows": [[60.0, 70.0]]},
                         {"section_id": "sec-5", "slot": "method",
                          "heading": "Second method",
                          "source_windows": [[70.0, 80.0]]}]
    assert AT.validate_plan(plan, "recipe") == []
    two = {"block": "ingredient_list",
           "dishes": [{"dish": "Soup",
                       "items": [{"amount": "2 cups", "item": "stock"}]},
                      {"dish": "Salad",
                       "items": [{"amount": "1 head", "item": "lettuce"}]}]}
    assert B.validate_block(two) == []
    md = B.render_markdown(two)
    assert "Soup" in md and "Salad" in md


def test_review_with_demo_uses_test_setup_not_steps():
    plan = _plan("review", ["subject", "test_setup", "assessment"])
    assert AT.validate_plan(plan, "review") == []
    # A setup demo is not a reader procedure: no steps slot involved.
    assert all(s["slot"] != "steps" for s in plan["sections"])


def test_discussion_without_speakers_is_valid():
    # Uncertain speakers: viewpoints without false attribution.
    plan = _plan("discussion", ["central_question", "positions",
                                "open_questions"])
    assert AT.validate_plan(plan, "discussion") == []


def test_report_with_uncertainty_slot_is_valid():
    plan = _plan("report", ["lede", "verified_facts", "uncertainty", "as_of"])
    assert AT.validate_plan(plan, "report") == []


def test_experiment_core_is_question_method_results():
    plan = _plan("experiment", ["question", "method", "results"])
    assert AT.validate_plan(plan, "experiment") == []
    bad = _plan("experiment", ["question", "results"])
    assert any("core slot" in e for e in AT.validate_plan(bad, "experiment"))


def test_explainer_rejects_procedure_slot():
    plan = _plan("explainer", ["answer", "steps"])
    errors = AT.validate_plan(plan, "explainer")
    assert any("steps" in e for e in errors)


def test_lead_shape_must_match_type():
    verdict = {"block": "verdict", "statement": "S.", "attribution": "R."}
    assert B.validate_block(verdict) == []
    # Shape validity is type-independent; type binding is enforced by
    # the writer's TYPE_LEAD_SHAPE contract (wrong shape == unsupported).
    from yt2wiki.v9.writer import TYPE_LEAD_SHAPE
    assert TYPE_LEAD_SHAPE["review"] == "verdict"
    assert TYPE_LEAD_SHAPE["procedure"] is None
    assert TYPE_LEAD_SHAPE["recipe"] == "ingredient_list"
    assert TYPE_LEAD_SHAPE["discussion"] == "position_list"
    assert TYPE_LEAD_SHAPE["experiment"] == "measured_comparison"


def test_no_article_decision_shape():
    assert AT.validate_selection({"schema_version": 1,
                                   "template_id": "no_article",
                                   "status": "refused"}) == []
    assert AT.validate_selection({"schema_version": 1,
                                   "template_id": "no_article",
                                   "status": "selected"})
