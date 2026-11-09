"""Typed editorial unit tests: scout windows, decision, plan, selection.

Deterministic only: scripted stub chats, no models, no network.
"""

from __future__ import annotations

import pytest

from yt2wiki.v9 import decide as D
from yt2wiki.v9 import scout as S
from yt2wiki.v9 import select as v9select


def _segs(n=30, step=30.0):
    return [{"idx": i, "start": i * step, "end": (i + 1) * step,
             "text": f"Segment {i} discusses topic alpha and 100 units."}
            for i in range(n)]


def test_build_windows_covers_everything_exactly_once():
    segs = _segs(30)
    wins = S.build_windows(segs, 900.0, target_windows=8)
    assert len(wins) <= 8
    assert wins[0]["t_start"] == 0.0
    assert wins[-1]["t_end"] == 900.0
    for a, b in zip(wins, wins[1:]):
        assert a["t_end"] == b["t_start"]
    assert sum(w["chars"] for w in wins) >= sum(len(s["text"]) for s in segs) - 30


def test_build_windows_empty_transcript():
    assert S.build_windows([], 0.0) == []


def test_window_ids_stable_after_tail_merge():
    segs = _segs(9)
    wins = S.build_windows(segs, 900.0, target_windows=4)
    assert [w["window_id"] for w in wins] == [f"w-{k:03d}" for k in range(len(wins))]


def test_build_windows_enforces_max_chars():
    segs = _segs(30)
    wins = S.build_windows(segs, 900.0, target_windows=4, max_chars=300)
    assert all(w["chars"] <= 300 + 60 for w in wins)


def test_digest_marks_failures_undigested_not_dropped():
    wins = S.build_windows(_segs(6), 180.0, target_windows=3)

    def boom(messages, **kw):
        raise RuntimeError("no backend")

    out = S.digest_windows(boom, wins)
    assert set(out) == {w["window_id"] for w in wins}
    assert all(d["flags"].get("undigested") for d in out.values())


def test_digest_validates_shape_and_retries():
    wins = S.build_windows(_segs(4), 120.0, target_windows=2)
    calls = {"n": 0}

    def chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"digests": [{"window_id": "nope", "topics": []}]}
        return {"digests": [
            {"window_id": w["window_id"], "topics": ["alpha"],
             "claims": ["c"], "names": [], "measures": ["100 units"],
             "flags": {}} for w in wins]}

    out = S.digest_windows(chat, wins)
    assert calls["n"] == 2
    assert out[wins[0]["window_id"]]["topics"] == ["alpha"]


def test_empty_index_refuses_without_model():
    index = S.build_index([], 0.0)
    decision = D.select_type(lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call model")), "T", 0.0, index)
    assert decision["template_id"] == "no_article"
    assert decision["status"] == "refused"


def test_music_only_refuses():
    segs = _segs(4)
    index = S.build_index(segs, 120.0)
    index["digests"] = {w["window_id"]: {"flags": {"music_only": True}}
                        for w in index["windows"]}
    decision = D.select_type(lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call model")), "T", 120.0, index)
    assert decision["status"] == "refused"


def _index():
    segs = _segs(12)
    return S.build_index(segs, 360.0)


def _decision_reply(**over):
    d = {"template_id": "discussion",
         "rationale": "multi-party debate across the full span",
         "supporting_window_ids": ["w-000", "w-005", "w-010"],
         "alternatives_rejected": [{"type": "procedure",
                                    "reason": "no executable task"}],
         "excluded_window_ids": [],
         "status": "selected"}
    d.update(over)
    return d


def test_select_type_freezes_valid_decision():
    index = _index()

    def chat(messages, **kw):
        assert "discussion" in messages[1]["content"]
        return _decision_reply()

    decision = D.select_type(chat, "Debate video", 360.0, index)
    assert decision["template_id"] == "discussion"
    assert decision["index_hash"] == index["index_hash"]
    assert len(decision["supporting_windows"]) == 3


def test_select_type_rejects_bad_windows_then_retries():
    index = _index()
    calls = {"n": 0}

    def chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _decision_reply(supporting_window_ids=["w-999"])
        return _decision_reply()

    decision = D.select_type(chat, "T", 360.0, index)
    assert decision["template_id"] == "discussion"
    assert calls["n"] == 2


def test_select_type_raises_when_invalid():
    index = _index()
    with pytest.raises(D.EditorialError):
        D.select_type(lambda messages, **kw: {"template_id": "tutorial"},
                      "T", 360.0, index)


def test_plan_locks_windows_and_validates():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"]}

    def chat(messages, **kw):
        assert "review" in messages[1]["content"]
        return {"template_id": "review",
                "sections": [
                    {"section_id": "sec-subject", "slot": "subject",
                     "heading": "What it is",
                     "window_ids": ["w-000", "w-001"]},
                    {"section_id": "sec-eval", "slot": "assessment",
                     "heading": "How it performs",
                     "window_ids": ["w-005", "w-010"]}],
                "coverage": {"included_window_ids": ["w-000"],
                             "excluded_window_ids": [],
                             "unassigned_substantive_window_ids": ["w-003"]}}

    plan = D.plan_article(chat, decision, index)
    assert [s["section_id"] for s in plan["sections"]] == ["sec-subject", "sec-eval"]
    sec = plan["sections"][0]
    assert sec["t_start"] == 0.0 and len(sec["source_windows"]) == 2
    assert sec["t_end"] == max(p[1] for p in sec["source_windows"])


def test_plan_retries_then_accepts():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"]}
    calls = {"n": 0}

    def chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"template_id": "review", "sections": []}
        return {"template_id": "review",
                "sections": [{"section_id": "sec-s", "slot": "subject",
                              "heading": "H", "window_ids": ["w-000"]},
                             {"section_id": "sec-a", "slot": "assessment",
                              "heading": "A", "window_ids": ["w-001"]}],
                "coverage": {}}

    plan = D.plan_article(chat, decision, index)
    assert calls["n"] == 2
    assert len(plan["sections"]) == 2


def test_plan_rejects_silent_retype():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"]}
    with pytest.raises(D.EditorialError):
        D.plan_article(lambda messages, **kw: {"template_id": "report",
                                               "sections": [
                                                   {"section_id": "s",
                                                    "slot": "lede",
                                                    "heading": "H",
                                                    "window_ids": ["w-000"]}],
                                               "coverage": {}},
                       decision, index)


def test_plan_excludes_decision_excluded_windows():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"],
                "excluded_window_ids": [],
                "excluded_windows": [{"start": 0.0, "end": 60.0,
                                      "reason": "sponsor", "window_id": "w-000"},
                                     {"start": 60.0, "end": 120.0,
                                      "reason": "sponsor", "window_id": "w-001"}]}

    def chat(messages, **kw):
        return {"template_id": "review",
                "sections": [{"section_id": "sec-s", "slot": "subject",
                              "heading": "H", "window_ids": ["w-000"]},
                             {"section_id": "sec-a", "slot": "assessment",
                              "heading": "A", "window_ids": ["w-002"]}],
                "coverage": {}}

    with pytest.raises(D.EditorialError):
        D.plan_article(chat, decision, index)


def test_candidates_come_from_window_union_not_interval():
    frames = [{"frame_id": f"f-{i}", "t": float(i * 10), "kind": "scene"}
              for i in range(12)]
    # Disjoint topical windows with a sponsor gap between them.
    cands = v9select.candidates_for_windows(frames, [[0.0, 20.0], [100.0, 110.0]],
                                            pad=0.0, limit=10)
    ts = sorted(f["t"] for f in cands)
    assert ts == [0.0, 10.0, 20.0, 100.0, 110.0]
    assert 50.0 not in ts


def test_candidates_single_window_matches_legacy():
    frames = [{"frame_id": f"f-{i}", "t": float(i * 10), "kind": "periodic"}
              for i in range(12)]
    a = v9select.candidates_for_section(frames, 0.0, 50.0, pad=0.0, limit=10)
    b = v9select.candidates_for_windows(frames, [[0.0, 50.0]], pad=0.0,
                                        limit=10)
    assert [f["frame_id"] for f in a] == [f["frame_id"] for f in b]


def test_plan_decline_is_explicit_not_silent():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"]}
    with pytest.raises(D.EditorialError, match="planner declined"):
        D.plan_article(lambda messages, **kw: {"declined": True,
                                               "reason": "no method shown"},
                       decision, index)


def test_plan_failure_names_the_cause():
    index = _index()
    decision = {"template_id": "review", "index_hash": index["index_hash"]}
    with pytest.raises(D.EditorialError, match="unknown window"):
        D.plan_article(lambda messages, **kw: {"template_id": "review",
                                               "sections": [
                                                   {"section_id": "s",
                                                    "slot": "subject",
                                                    "heading": "H",
                                                    "window_ids": ["w-404"]}],
                                               "coverage": {}},
                       decision, index)


def test_many_windows_survive_without_merging():
    segs = [{"idx": i, "start": i * 30.0, "end": (i + 1) * 30.0,
             "text": (f"Segment {i} discusses topic alpha. ") * 30}
            for i in range(300)]
    wins = S.build_windows(segs, 9000.0, target_windows=24)
    assert len(wins) > 64
    assert wins[0]["t_start"] == 0.0
    assert wins[-1]["t_end"] == 9000.0
    for a, b in zip(wins, wins[1:]):
        assert a["t_end"] == b["t_start"]


def test_window_features_carry_domestic_values():
    segs = [{"idx": 0, "start": 0.0, "end": 10.0,
             "text": "Add 4 1/2 cups of flour costing $300,000."}]
    wins = S.build_windows(segs, 10.0, target_windows=24)
    feats = S.window_features(wins[0])
    values = [lit["value"] for lit in feats["literals"]]
    assert "4 1/2 cups" in values
    assert "$300,000" in values


def test_values_extractor_cases():
    from yt2wiki.v9 import values as _V

    got = {lit.literal for lit in _V.extract_exact_values(
        "Bake 2 loaves at 350 degrees for 1 hour; costs $20, not $300.")}
    assert "350 degrees" in got and "$20" in got
    assert _V.extract_exact_values("") == []


def test_window_id_echo_is_canonicalised():
    from yt2wiki.v9.scout import canonical_window_id

    assert canonical_window_id("w-004 147-185s") == "w-004"
    assert canonical_window_id("window w-007") == "w-007"
    assert canonical_window_id("w-012") == "w-012"
    assert canonical_window_id("nope") is None
    assert canonical_window_id(None) is None


def test_plan_accepts_echoed_window_ids():
    index = _index()
    raw = {"template_id": "review",
           "sections": [{"section_id": "s", "slot": "subject",
                         "heading": "H",
                         "window_ids": ["w-000 0-30s", "window w-001"]},
                        {"section_id": "a", "slot": "assessment",
                         "heading": "A", "window_ids": ["w-002"]}],
           "coverage": {}}
    plan, reason = D._assemble_plan(raw, "review",
                                    {w["window_id"]: (w["t_start"], w["t_end"])
                                     for w in index["windows"]}, set())
    assert plan is not None, reason
    assert plan["sections"][0]["window_ids"] == ["w-000", "w-001"]


def test_digest_accepts_list_flags_and_echoed_ids():
    from yt2wiki.v9.scout import _normalize_flags, _valid_digest

    entry = {"window_id": "w-003 147-185s", "topics": ["t"],
             "flags": ["housekeeping"]}
    norm = _normalize_flags(entry)
    assert norm["flags"] == {"housekeeping": True}
    assert _valid_digest(norm, {"w-003"})
    assert _normalize_flags({"window_id": "w-1"})["flags"] == {}


def test_count_actions_counts_imperatives_and_commands():
    from yt2wiki.v9.values import count_actions

    assert count_actions("Set your allow list. Then scan the code.") == 2
    assert count_actions("The system ships with tools.") == 0
    assert count_actions("Run `git branch demo` now.") >= 1
    assert count_actions("") == 0


def test_year_and_decade_extraction():
    from yt2wiki.v9 import values as _V

    got = {lit.literal for lit in _V.extract_exact_values(
        "In 1976 dollars; in the 90s; not in 20s fashion.")}
    assert "1976" in got and "90s" in got
    # An explicit decade token also supports the abbreviated form; a
    # bare year emits no phantom decade alias.
    decade = {lit.literal for lit in _V.extract_exact_values(
        "Booming in the 1990s.")}
    assert "1990s" in decade and "90s" in decade
    bare = {lit.literal for lit in _V.extract_exact_values("In 1976.")}
    assert "1976" in bare and "76s" not in bare


def test_mega_span_sections_rejected_at_plan_time():
    by_id = {f"w-{i:03d}": (float(i * 300), float(i * 300 + 200))
             for i in range(12)}
    raw = {"template_id": "review",
           "sections": [{"section_id": "s", "slot": "subject",
                         "heading": "H",
                         "window_ids": [f"w-{i:03d}" for i in range(12)]},
                        {"section_id": "a", "slot": "assessment",
                         "heading": "A", "window_ids": ["w-001"]}],
           "coverage": {}}
    plan, reason = D._assemble_plan(raw, "review", by_id, set())
    assert plan is None
    assert "15 min" in reason


def test_fifteen_minute_boundary_and_retry_after_cap():
    by_id = {"w-000": (0.0, 450.0), "w-001": (450.0, 900.0),
             "w-002": (950.0, 1400.0)}
    raw = {"template_id": "review",
           "sections": [{"section_id": "s", "slot": "subject",
                         "heading": "H", "window_ids": ["w-000", "w-001"]},
                        {"section_id": "a", "slot": "assessment",
                         "heading": "A", "window_ids": ["w-002"]}],
           "coverage": {}}
    plan, reason = D._assemble_plan(raw, "review", by_id, set())
    assert plan is not None, reason
    calls = {"n": 0}

    def chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"template_id": "review",
                    "sections": [{"section_id": "s", "slot": "subject",
                                  "heading": "H",
                                  "window_ids": ["w-000", "w-001", "w-002"]},
                                 {"section_id": "a", "slot": "assessment",
                                  "heading": "A", "window_ids": ["w-002"]}],
                    "coverage": {}}
        return {"template_id": "review",
                "sections": [{"section_id": "s", "slot": "subject",
                              "heading": "H", "window_ids": ["w-000"]},
                             {"section_id": "a", "slot": "assessment",
                              "heading": "A", "window_ids": ["w-002"]}],
                "coverage": {}}

    index = {"windows": [
        {"window_id": "w-000", "t_start": 0.0, "t_end": 450.0},
        {"window_id": "w-001", "t_start": 450.0, "t_end": 900.0},
        {"window_id": "w-002", "t_start": 950.0, "t_end": 1400.0}],
        "digests": {}, "index_hash": "idx-1"}
    decision = {"template_id": "review", "index_hash": "idx-1"}
    plan = D.plan_article(chat, decision, index)
    assert calls["n"] == 2
    assert len(plan["sections"]) == 2


def test_decade_alias_only_for_explicit_decades():
    from yt2wiki.v9 import values as _V

    got = {lit.literal for lit in _V.extract_exact_values("in the 1990s.")}
    assert "1990s" in got and "90s" in got
    got2 = {lit.literal for lit in _V.extract_exact_values("in 1976.")}
    assert "1976" in got2 and "76s" not in got2


def test_negative_and_comma_values():
    from yt2wiki.v9 import values as _V

    got = {lit.literal for lit in _V.extract_exact_values("-4 degrees, 1,500 units.")}
    assert "-4 degrees" in got
    got2 = {lit.literal for lit in _V.extract_exact_values("1500 units.")}
    assert "1500" in got2


def test_half_a_cup_and_time_idioms():
    from yt2wiki.article import markdown_to_html  # noqa: F401 (import order)
    from yt2wiki.v9.validate import _numeralize

    assert "1/2 cup" in _numeralize("half a cup of flour")
    assert "1/4 " in _numeralize("a quarter cup of sugar")
    assert _numeralize("wait a minute") == "wait a minute"


def test_flags_string_false_is_not_set():
    from yt2wiki.v9.scout import _normalize_flags

    assert _normalize_flags({"window_id": "w-1",
                             "flags": {"ad": "false"}})["flags"] == {"ad": False}
    assert _normalize_flags({"window_id": "w-1",
                             "flags": {"ad": "yes"}})["flags"] == {"ad": True}


def test_canonical_id_rejects_four_digits():
    from yt2wiki.v9.scout import canonical_window_id

    assert canonical_window_id("w-1000") is None
