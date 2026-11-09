"""Typed article contracts v1: catalogue, selection and plan validation.

Deterministic only: no models, no network, no GPU. Covers the closed
type enum, the no_article refusal result, the frozen template version,
and valid/invalid selection + plan fixtures for every article type.
"""

from __future__ import annotations

import pytest

from yt2wiki.v9 import article_types as AT


def _valid_plan_for(template_id, slots):
    sections = [
        {"section_id": f"sec-{i}", "slot": slot,
         "heading": f"Heading {i}",
         "source_windows": [[float(i * 10), float(i * 10 + 5)]]}
        for i, slot in enumerate(slots)
    ]
    return {"template_id": template_id, "sections": sections, "coverage": {}}


def _selection(template_id="discussion", **over):
    d = {
        "schema_version": 1,
        "template_id": template_id,
        "template_version": 1,
        "selection_mode": "auto",
        "rationale": "multi-party technical debate",
        "supporting_windows": [[0.0, 340.0], [1550.0, 1760.0]],
        "alternatives_rejected": [{"type": "procedure",
                                   "reason": "terminal views are a sponsor segment"}],
        "excluded_windows": [{"start": 420.0, "end": 510.0, "reason": "sponsor"}],
        "status": "selected",
    }
    d.update(over)
    return d


def _plan_pair(slot_a="central_question", slot_b="positions"):
    return {
        "template_id": "discussion",
        "sections": [
            {"section_id": "sec-q", "slot": slot_a,
             "heading": "Are we close to RSI?",
             "source_windows": [[0.0, 340.0]]},
            {"section_id": "sec-p", "slot": slot_b,
             "heading": "What the panel argued",
             "source_windows": [[1550.0, 1760.0], [5100.0, 5450.0]]},
        ],
        "coverage": {"included_window_ids": ["w1"],
                     "excluded_window_ids": ["w9"],
                     "unassigned_substantive_window_ids": []},
    }


def test_catalogue_has_seven_immutable_types():
    assert set(AT.ARTICLE_TYPES) == {"procedure", "recipe", "report",
                                     "explainer", "review", "discussion",
                                     "experiment"}
    for tid in AT.ARTICLE_TYPES:
        tdef = AT.type_definition(tid)
        assert tdef["reader_promise"]
        assert tdef["core_slots"]
        assert tdef["optional_slots"]
        assert tdef["disallowed"]
    assert AT.type_definition("how-to") is None
    assert AT.TEMPLATE_VERSION == 1


def test_selection_accepts_every_type():
    for tid in AT.ARTICLE_TYPES:
        assert validate_ok(_selection(tid)) == []


def validate_ok(decision):
    return AT.validate_selection(decision)


@pytest.mark.parametrize("tid", list(AT.ARTICLE_TYPES))
def test_selection_rejects_template_version_drift(tid):
    assert AT.validate_selection(_selection(tid, template_version=2))


def test_selection_rejects_invented_type():
    errors = AT.validate_selection(_selection("how-to-video"))
    assert any("template_id" in e for e in errors)


def test_selection_rejects_empty_rationale_and_windows():
    errors = AT.validate_selection(
        _selection(rationale="  ", supporting_windows=[]))
    assert len(errors) >= 2


def test_selection_rejects_bad_windows():
    bad = _selection(supporting_windows=[[300.0, 100.0]])
    assert any("supporting_windows" in e for e in AT.validate_selection(bad))


def test_no_article_is_a_refusal_not_a_type():
    assert AT.NO_ARTICLE not in AT.ARTICLE_TYPES
    assert AT.validate_selection({
        "schema_version": 1, "template_id": AT.NO_ARTICLE,
        "status": "refused"}) == []
    errors = AT.validate_selection({
        "schema_version": 1, "template_id": AT.NO_ARTICLE,
        "status": "selected"})
    assert errors


def test_plan_accepts_core_coverage():
    assert AT.validate_plan(_plan_pair(), "discussion") == []


def test_plan_rejects_missing_core_slot():
    plan = _plan_pair(slot_a="central_question", slot_b="agreements")
    assert any("core slot" in e for e in AT.validate_plan(plan, "discussion"))


def test_plan_rejects_foreign_slot_and_dup_ids():
    plan = _plan_pair()
    plan["sections"][1]["slot"] = "ingredients"
    plan["sections"][1]["section_id"] = "sec-q"
    errors = AT.validate_plan(plan, "discussion")
    assert any("ingredients" in e for e in errors)
    assert any("duplicate section_id" in e for e in errors)


def test_plan_rejects_bad_windows_and_headings():
    plan = _plan_pair()
    plan["sections"][0]["source_windows"] = [[-5.0, 10.0]]
    plan["sections"][1]["heading"] = "   "
    errors = AT.validate_plan(plan, "discussion")
    assert len(errors) >= 2


def test_plan_rejects_unknown_template():
    assert AT.validate_plan(_plan_pair(), "tutorial")


def test_reader_labels_neutral_not_fabricated():
    assert AT.reader_label("discussion") == "Discussion"
    assert AT.reader_label("comparison") == "Article"
    assert AT.reader_label(None) == "Article"


def test_legacy_kind_fallback():
    assert AT.resolve_legacy_kind("procedure") == "procedure"
    assert AT.resolve_legacy_kind("experimental") == "experiment"
    assert AT.resolve_legacy_kind("comparison") is None
    assert AT.resolve_legacy_kind("teardown") is None
    assert AT.resolve_legacy_kind("") is None


@pytest.mark.parametrize("tid", list(AT.ARTICLE_TYPES))
def test_plan_accepts_core_slots_for_every_type(tid):
    tdef = AT.type_definition(tid)
    assert AT.validate_plan(_valid_plan_for(tid, list(tdef["core_slots"])), tid) == []


def test_plan_rejects_empty_source_windows():
    plan = _plan_pair()
    plan["sections"][0]["source_windows"] = []
    assert any("source_windows" in e
               for e in AT.validate_plan(plan, "discussion"))


def test_plan_rejects_template_mismatch():
    plan = _valid_plan_for("review", ["subject", "assessment"])
    errors = AT.validate_plan(plan, "discussion")
    assert any("does not match frozen type" in e for e in errors)


def test_plan_survives_unhashable_slot_and_coverage():
    plan = _plan_pair()
    plan["sections"][0]["slot"] = ["central_question"]
    plan["coverage"] = ["not", "a", "dict"]
    errors = AT.validate_plan(plan, "discussion")
    assert any("slot" in e for e in errors)
    assert any("coverage must be an object" in e for e in errors)


def test_selection_rejects_bool_versions():
    assert AT.validate_selection(
        _selection(schema_version=True, template_version=True))


def test_selection_validates_excluded_windows():
    bad = _selection(excluded_windows=[{"start": 5.0}])
    assert any("excluded_windows" in e for e in AT.validate_selection(bad))
    good = _selection(excluded_windows=[{"start": 1.0, "end": 2.0,
                                         "reason": "sponsor"}])
    assert AT.validate_selection(good) == []


def test_catalogue_is_immutable_through_accessor():
    first = AT.type_definition("recipe")
    first["core_slots"] = ("hacked",)
    first["lead_block"] = "hacked"
    second = AT.type_definition("recipe")
    assert "dish" in second["core_slots"]
    assert second["lead_block"] == "ingredient_list"
