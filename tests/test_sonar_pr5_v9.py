"""PR5 (Sonar S3776 v9 editorial/publishing refactors): invariant pins.

The v9 suites (test_v9, test_v9z1, test_v9judge_route, test_v9_editorial,
test_v9norm) exercise the refactored paths end-to-end with stub backends,
including selection decisions and editorial rejection; these tests pin the
structural contracts most at risk from the restructuring. No models, no
network.
"""

from __future__ import annotations

import pytest

from yt2wiki.v9.judge import _parse_rubric
from yt2wiki.v9.publish import assemble_markdown

PROV = {"url": "https://youtu.be/abc", "attribution_line": "Compiled from the video **T** by S"}
SECTIONS = [
    {"section_id": "sec-01-install", "heading": "Install", "body": "Run it [0:10](https://youtu.be/abc?t=10).",
     "t_start": 0.0, "t_end": 60.0},
    {"section_id": "sec-02-configure", "heading": "Configure", "body": "Set the value.", "t_start": 60.0,
     "t_end": 120.0},
]
PICKS = {"sec-01-install": [{"frame_id": "f-1", "t": 30.0, "path": "/x/f-1.jpg",
                             "caption": "", "alt_text": ""}],
         "sec-02-configure": []}


def test_assemble_places_figure_after_body_and_source_ref_between():
    md, rows = assemble_markdown("T", "sum", SECTIONS, PICKS, PROV)
    assert md.index("## Install") < md.index("![")
    assert md.index("Run it") < md.index("![")          # figure after prose
    assert md.index("## Configure") > md.index("![")    # figure stays inside its section
    assert len(rows) == 1 and rows[0]["frame_id"] == "f-1"
    assert rows[0]["alt_text"].startswith("Illustration for Install")


def test_assemble_consolidates_inline_citations_into_section_refs():
    md, _ = assemble_markdown("T", "sum", SECTIONS, PICKS, PROV)
    # per-sentence citations are consolidated to one subdued section ref
    assert "[0:10]" not in md
    assert "*Source 0:00–1:00 — [0:00](https://youtu.be/abc?t=0)*" in md
    assert "javascript:" not in md


def test_assemble_no_url_keeps_timestamps_plain():
    prov = {"attribution_line": "Compiled locally"}
    md, _ = assemble_markdown("T", "sum", SECTIONS, PICKS, prov)
    assert "](http" not in md


def test_rubric_rejects_made_up_or_partial_verdicts():
    good = ('{"scores": ['
            '{"idx": 1, "relevance": 2, "clarity": 2, "specificity": 2, "context": 2, "aesthetics": 1},'
            '{"idx": 2, "relevance": 1, "clarity": 1, "specificity": 1, "context": 1, "aesthetics": 1}],'
            '"decision": "REPLACE", "winner": 1, "explanation": "e", "images_received": 2}')
    assert _parse_rubric(good, 2) is not None
    # wrong images_received count: rejected outright
    assert _parse_rubric(good.replace('"images_received": 2', '"images_received": 3'), 2) is None
    # partial score set (only one of two candidates): rejected
    partial = ('{"scores": ['
               '{"idx": 1, "relevance": 2, "clarity": 2, "specificity": 2,'
               ' "context": 2, "aesthetics": 1}],'
               '"decision": "REPLACE", "winner": 1, "explanation": "e",'
               ' "images_received": 2}')
    assert _parse_rubric(partial, 2) is None
    # boolean count approximation: rejected (never coerced)
    assert _parse_rubric(good.replace('"images_received": 2', '"images_received": true'), 2) is None
    # the model reporting NO_IMAGE is never installed
    assert _parse_rubric(good.replace('"REPLACE"', '"NO_IMAGE"'), 2) is None


@pytest.mark.parametrize("decision", ["KEEP", "REPLACE", "EXPAND", "USE_PAIR", "OMIT"])
def test_rubric_known_decisions_accepted(decision):
    raw = ('{"scores": [{"idx": 1, "relevance": 2, "clarity": 2, "specificity": 2,'
           ' "context": 2, "aesthetics": 1}],'
           f'"decision": "{decision}", "winner": 1, "explanation": "e",'
           ' "images_received": 1}')
    parsed = _parse_rubric(raw, 1)
    assert parsed is not None and parsed["decision"] == decision


def test_z1_refinement_round_accounts_for_vlm_calls(monkeypatch):
    """An EXPAND refinement round increments vlm_calls/frames_inspected once."""
    from yt2wiki.v9 import judge as J

    calls = {"classify": 0}

    def fake_classify_many(backend, paths, *, cache=None, expects_text=False):
        calls["classify"] += 1
        return [{"path": p, "label": "instructional", "veto": False,
                 "reason": "", "ocr": {}} for p in paths]

    monkeypatch.setattr(J, "classify_many", fake_classify_many)
    section = {"section_id": "sec-1", "heading": "Install it",
               "body": "Install the widget by following these steps precisely.",
               "t_start": 0.0, "t_end": 60.0}
    initial = {"frame_id": "f-1", "path": "/tmp/f-1.jpg", "t": 10.0}
    alts = [{"frame_id": f"f-{i}", "path": f"/tmp/f-{i}.jpg", "t": 10.0 + i}
            for i in range(2, 6)]
    # unseen later-video frames the EXPAND refinement can retrieve
    later = [{"frame_id": "f-10", "path": "/tmp/f-10.jpg", "t": 40.0},
             {"frame_id": "f-11", "path": "/tmp/f-11.jpg", "t": 50.0}]

    def fake_scores(ordered):
        return {"scores": [{"idx": i + 1, "relevance": 0, "clarity": 0,
                            "specificity": 0, "context": 0, "aesthetics": 0,
                            "total": 0, "veto": False, "veto_reason": "",
                            "context_ok": False, "weakness": "",
                            "frame_id": c["frame_id"]}
                           for i, c in enumerate(ordered)],
                "decision": "EXPAND", "winner": 0, "pair": [],
                "explanation": "e",
                "refinement": {"direction": "later", "detail": "d"}}

    def fake_score_candidates(backend, section, ordered, current_pos, *, cache=None):
        return fake_scores(ordered), 1

    class Stub:
        supports_structured_schema = False

    monkeypatch.setattr(J, "score_candidates", fake_score_candidates)
    monkeypatch.setattr(J, "_vet_candidate",
                        lambda backend, cand, expects: {"flagged": False,
                                                        "reason": "", "label": "",
                                                        "ocr": {}})
    monkeypatch.setattr(J, "subject_of", lambda backend, path, cache=None: "widget")
    monkeypatch.setattr(J, "_subject_supported", lambda desc, section: True)
    rec = J.adjudicate_section_z1(
        Stub(), section, initial, alts, [initial] + alts + later,
        max_rounds=1, seed=0)
    # EXPAND with all-zero totals and max_rounds=1 ends in OMIT after rescore
    assert rec["decision"] == "OMIT"
    assert rec["rounds"] and rec["rounds"][-1]["refinement"]
    # one classify_many in pool pre-filter + one per refinement round
    refinements = sum(1 for r in rec["rounds"] if "refinement" in r)
    assert calls["classify"] == 1 + refinements
    assert rec["vlm_calls"] >= 2
