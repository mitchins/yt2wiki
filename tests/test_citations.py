"""M7-A §5 adversarial citation tests (no models, no network).

Each case pins a required behavior of the citation system.  Cases marked
FIX-ME fail against the current implementation and pass after the §4/§6
treatment; the rest lock in behavior that must not regress.
"""

from __future__ import annotations

from yt2wiki.literals import Literal
from yt2wiki.records import Evidence


def _ev(eid, kind, text, t_start=None, literals=()):
    return Evidence(
        evidence_id=eid, kind=kind, text=text,
        literals=[Literal(kind="command", literal=v) for v in literals],
        t_start=t_start, t_end=(t_start + 5.0) if t_start is not None else None,
    )


EV = [
    _ev("ev-aaa111", "action", "Insert the USB drive into the Pi and boot it up",
        214.0),
    _ev("ev-bbb222", "warning",
        "Using USB attached disks for a permanent part of a data pool is unreliable",
        30.0),
    _ev("ev-ccc333", "command", "Run the installer from the USB drive",
        220.0, literals=["sudo dd if=image.img of=/dev/sda bs=4M"]),
    _ev("ev-ddd444", "config", "Set arm frequency to 3000 megahertz",
        100.0, literals=["arm_freq=3000"]),
]


def _finding(fid, message, code="unsupported_action", detail=None):
    return {"finding_id": fid, "direction": "article->evidence",
            "severity": "warning", "code": code, "message": message,
            "detail_json": None, "detail": detail or {}}


# 1. Correct claim, missing citation -> triage A.
def test_correct_claim_missing_citation_is_A():
    from eval.paired.triage import triage_finding

    md = ("## Procedure\n\n1. **Boot the Pi.**\n\n"
          "   Insert the USB drive into the Pi and boot it up.\n")
    out = triage_finding(
        _finding("f1", "The article states that you should insert the USB drive "
                       "into the Pi and boot it up, but does not provide a "
                       "direct link to the source."), md,
        [dict(evidence_id=e.evidence_id, kind=e.kind, text=e.text,
              literals=[l.literal for l in e.literals], t_start=e.t_start,
              frame_ids=[]) for e in EV])
    assert out["verdict"] == "A"
    assert out["best_coverage"] >= 0.7


# 2. Correct claim, broken citation ID -> broken_citation finding (FIX-ME).
def test_broken_citation_id_is_flagged_not_dropped():
    from yt2wiki.pipeline.stages.verify import _confirm_llm_findings

    raw = [{"direction": "article->evidence", "severity": "warning",
            "code": "unsupported_action",
            "message": "Step 1 is backed by ev-deadbeef which does not exist.",
            "quote": "Insert the USB drive into the Pi and boot it up.",
            "evidence_ref": "ev-deadbeef"}]
    out = _confirm_llm_findings(raw, EV)
    assert any(f["code"] == "broken_citation" for f in out), out


# 3. Correct claim linked to an unrelated passage -> still flagged (FIX-ME).
def test_claim_linked_to_unrelated_passage_is_flagged():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported

    md = ("## Procedure\n\n1. **Boot the Pi.**\n\n"
          "   Insert the USB drive into the Pi and boot it up.\n"
          "   *Source: [0:30](https://example.com/watch?v=x&t=30)*\n")
    out = _rule_unsupported(_ctx(), md, EV)
    flagged = [f for f in out if "Step 1" in f.get("message", "")]
    assert flagged, "a citation to an unrelated passage must not excuse the claim"


class _Ctx:
    pass


def _ctx():
    return _Ctx()


# 4. Incorrect numeric value linked to a relevant passage -> value_mismatch.
def test_wrong_value_with_relevant_passage_is_flagged():
    from yt2wiki.pipeline.stages.verify import _rule_value_mismatch

    md = "## Procedure\n\n1. **Overclock.**\n\n   Set `arm_freq=2600`.\n"
    out = _rule_value_mismatch(_ctx(), md, EV, {"arm_freq=3000"})
    assert any(f["code"] == "value_mismatch" for f in out)


# 6. Compound step where only one claim is supported -> per-claim finding (FIX-ME).
def test_compound_step_flags_only_unsupported_half():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported

    md = ("## Procedure\n\n1. **Boot and configure.**\n\n"
          "   Insert the USB drive into the Pi and boot it up. "
          "Then reformat the disk with a hammer.\n"
          "   *Source: [3:34](https://example.com/watch?v=x&t=214)*\n")
    out = _rule_unsupported(_ctx(), md, EV)
    assert any("hammer" in f.get("message", "") or "hammer" in str(f.get("detail", ""))
               for f in out), out
    assert not any("Insert the USB drive" in f.get("message", "") for f in out)


# 7. Paraphrased but genuinely supported instruction -> no finding.
def test_supported_paraphrase_is_not_flagged():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported

    md = ("## Procedure\n\n1. **Boot the Pi from USB.**\n\n"
          "   Plug the USB stick into the Raspberry Pi and power it on.\n"
          "   *Source: [3:34](https://example.com/watch?v=x&t=214)*\n")
    out = _rule_unsupported(_ctx(), md, EV)
    assert out == [], out


# 8. Instruction whose evidence was removed -> flagged (must not regress).
def test_evidence_removed_claim_is_flagged():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported

    md = ("## Procedure\n\n1. **Boot the Pi.**\n\n"
          "   Insert the USB drive into the Pi and boot it up.\n")
    out = _rule_unsupported(_ctx(), md, [e for e in EV if e.evidence_id != "ev-aaa111"])
    assert any("Step 1" in f.get("message", "") for f in out)


# 9. Anecdote with a timestamp link is still non-material.
def test_linked_anecdote_stays_non_material():
    from eval.paired.disposition import NON_INSTRUCTIONAL, material_summary

    comparison = {"fact_outcomes": [
        {"fact_id": "joke", "status": "hit"},
        {"fact_id": "real", "status": "hit"},
    ]}
    dispositions = [
        {"fact_id": "joke", "disposition": NON_INSTRUCTIONAL},
        {"fact_id": "real", "disposition": "REQUIRED_INSTRUCTION"},
    ]
    m = material_summary(comparison, dispositions)
    assert (m["material_hits"], m["material_total"]) == (1, 1)
    assert m["material_completeness"] == 1.0


# 5. Same key asserted with a different value -> contradiction (FIX-ME).
def test_contradictory_value_is_flagged_as_contradiction():
    from yt2wiki.pipeline.stages import verify as V

    md = "## Procedure\n\n1. **Overclock.**\n\n   Set `arm_freq=2600`.\n"
    out = V._rule_contradictions(md, EV)
    assert any(f.get("code") == "contradiction" for f in out), out
    assert any("ev-ddd444" in str(f.get("detail", "")) for f in out)
