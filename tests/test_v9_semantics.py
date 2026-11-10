"""Gate v2 semantic-verification regression tests (canary corrective mission).

Frozen first-pass evidence: canary #2 (mower blades, 'Repair') and canary
#5 (Byzantine report, '1 year' / 'Transition' / 'Dress'). The original
lexical findings are preserved in data/canary-01 (CANARY.md and
work/job-f8288c3359ff/v9_validate/findings.json); these fixtures pin the
corrected behavior on the real words.

No models, no network: the entailment probe is a stub here. Live-probe
behavior on the frozen candidates is qualified in the canary re-runs.
"""

from __future__ import annotations

from yt2wiki.v9 import validate as V


# ----------------------------------------------------------------------
# helpers
def _decision(tid="report"):
    return {"template_id": tid, "template_version": "1",
            "excluded_windows": []}


def _plan(sections):
    return {"sections": sections, "coverage": {}}


def _index():
    return {"windows": []}


def _segs(*texts, start=0.0):
    out = []
    t = start
    for text in texts:
        out.append({"start": t, "end": t + 15.0, "text": text})
        t += 15.0
    return out


def _article(atype, sections):
    return {"article_type": atype, "title": "T", "summary": "S",
            "sections": sections}


def _sec(sid, heading, body, t0=0.0, t1=30.0):
    return {"section_id": sid, "heading": heading, "body": body,
            "source_windows": [[t0, t1]], "t_start": t0, "t_end": t1}


def _stub(verdict, support=""):
    def probe(prompt):
        probe.seen.append(prompt)
        return {"verdict": verdict, "support": support}
    probe.seen = []
    return probe


# ----------------------------------------------------------------------
# fix 1: indefinite-article time normalisation ("after a year" = 1 year)
def test_a_year_normalises_to_one_year():
    assert V._numeralize("after a year") == "after 1 year"
    assert V._numeralize("an hour later") == "1 hour later"
    assert V._numeralize("rest a day") == "rest 1 day"
    assert V._numeralize("within a week") == "within 1 week"
    assert V._numeralize("after a month") == "after 1 month"


def test_minute_idiom_guard_preserved():
    # Sub-hour indefinite time stays literal: "wait a minute" is idiom.
    assert V._numeralize("wait a minute") == "wait a minute"
    assert V._numeralize("just a second") == "just a second"


def test_one_year_later_supported_by_after_a_year():
    # Frozen #5 words: transcript "after a year, the population
    # rebelled" supports article "a rebellion one year later". Pre-fix
    # this fired unsupported_exact_value critical (findings.json).
    segs = _segs("after a year, the population rebelled, and Alexios "
                 "himself was deposed and killed")
    art = _article("report", [_sec(
        "decline-and-fall", "H",
        "a rebellion one year later resulted in his deposition and death")])
    out = V.check_exact_values(art, segs, 300.0)
    assert not [f for f in out
                if f["code"] == "unsupported_exact_value"
                and "1 year" in f["detail"]], out


# ----------------------------------------------------------------------
# lexical triage still fires on the frozen words (first-pass preserved)
def _transition_article():
    return _article("report", [_sec(
        "__frontmatter__", "T",
        "The Transition from Rome to Byzantium persisted. "
        "The Transition shaped later centuries. Rome endured. Byzantium fell.",
        t0=0.0, t1=60.0)])


def test_transition_reproduces_first_pass_critical():
    segs = _segs("Constantine moved the Roman capital to Constantinople. "
                 "Rome endured in the east. Byzantium was its city.")
    out = [f for f in V.check_entities(_transition_article(), segs)
           if f["code"] == "unattested_entity" and f.get("name") == "Transition"]
    assert len(out) == 1 and out[0]["severity"] == "critical", out


def test_repair_reproduces_first_pass_major():
    art = _article("procedure", [_sec(
        "s1", "H",
        "### Assessment and Repair\n"
        "* If the blade has damage from rocks, grind a new edge.")])
    segs = _segs("if the blade has damage from rocks grind a new edge")
    out = [f for f in V.check_entities(art, segs)
           if f["code"] == "unattested_entity" and f.get("name") == "Repair"]
    assert len(out) == 1 and out[0]["severity"] == "major", out


# ----------------------------------------------------------------------
# semantic adjudication mapping (stub probe)
def test_entailed_verifiable_quote_discharges():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)
    probe = _stub("entailed", "moved the Roman capital to Constantinople")
    kept, discharged = V.adjudicate_entities(triage, art, segs, probe)
    names = [f.get("name") for f in discharged]
    assert "Transition" in names, (kept, discharged)
    assert not [f for f in kept if f.get("name") == "Transition"]
    d = next(f for f in discharged if f.get("name") == "Transition")
    assert d["adjudication"]["verdict"] == "entailed"
    assert d["adjudication"]["support"]
    # The probe saw one formatted prompt carrying the word, the real
    # claim passage and bounded evidence.
    prompt = probe.seen[0]
    assert "Flagged word: Transition" in prompt
    assert "Constantine moved the Roman capital" in prompt


def test_claim_carries_neighbour_context_not_bare_label():
    # A bare label asserts no proposition; the probe must see the topic
    # in context (one neighbouring line each side, same section).
    art = _article("procedure", [_sec(
        "s1", "Addressing Damage",
        "### Assessment and Repair\n"
        "* If the blade has damage from rocks, grind a new edge.")])
    segs = _segs("damage from rocks grind a new edge")
    triage = V.check_entities(art, segs)
    probe = _stub("entailed", "damage from rocks grind a new edge")
    V.adjudicate_entities(triage, art, segs, probe)
    prompt = probe.seen[0]
    assert "Flagged word: Repair" in prompt
    assert "Assessment and Repair" in prompt
    assert "damage from rocks" in prompt


def test_elided_duplicate_sentences_still_discharge():
    # Captions repeat whole sentences; the probe may elide the
    # duplicate. Every quoted sentence is still verbatim: discharge.
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople. "
                 "Constantine moved the Roman capital to Constantinople. "
                 "Rome endured in the east. Byzantium was its city.")
    triage = V.check_entities(art, segs)
    support = ("Constantine moved the Roman capital to Constantinople. "
               "Byzantium was its city.")
    kept, discharged = V.adjudicate_entities(
        triage, art, segs, _stub("entailed", support))
    assert [f.get("name") for f in discharged] == ["Transition"]
    assert not kept


def test_reworded_quote_is_uncertain():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)
    probe = _stub("entailed", "Constantine relocated the capital city.")
    kept, discharged = V.adjudicate_entities(triage, art, segs, probe)
    assert discharged == []
    assert kept[0].get("adjudication", {}).get("verdict") == "uncertain"


def test_trivial_fragment_cannot_discharge():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)
    probe = _stub("entailed", "the capital")
    kept, discharged = V.adjudicate_entities(triage, art, segs, probe)
    assert discharged == []
    assert kept[0].get("adjudication", {}).get("verdict") == "uncertain"


def test_entailed_without_verifiable_quote_is_uncertain():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)
    probe = _stub("entailed", "the narrator definitely implied it")
    kept, discharged = V.adjudicate_entities(triage, art, segs, probe)
    assert not [f for f in discharged if f.get("name") == "Transition"]
    k = next(f for f in kept if f.get("name") == "Transition")
    assert k["adjudication"]["verdict"] == "uncertain"


def test_uncertain_keeps_severity_with_evidence():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)
    kept, _ = V.adjudicate_entities(triage, art, segs, _stub("uncertain"))
    k = next(f for f in kept if f.get("name") == "Transition")
    assert k["severity"] == "critical"  # hold preserved, not silent
    assert k["adjudication"]["evidence_excerpt"]


def test_uncertain_major_forces_needs_review():
    art = _article("procedure", [_sec(
        "s1", "H",
        "### Assessment and Repair\n* Grind a new edge on the blade.")])
    segs = _segs("grind a new edge on the blade")
    res = V.validate_typed(_decision("procedure"), _plan([]), _index(),
                           art, segs, 60.0,
                           probe=_stub("uncertain"))
    assert res["status"] == "needs_review", res
    assert any((f.get("adjudication") or {}).get("verdict") == "uncertain"
               for f in res["findings"])


def test_contradicted_major_escalates_to_critical():
    art = _article("procedure", [_sec(
        "s1", "H",
        "### Assessment and Repair\n* Grind a new edge on the blade.")])
    segs = _segs("grind a new edge on the blade")
    res = V.validate_typed(_decision("procedure"), _plan([]), _index(),
                           art, segs, 60.0,
                           probe=_stub("contradicted", "no repair exists"))
    assert res["status"] == "needs_review", res
    k = next(f for f in res["findings"] if f.get("name") == "Repair")
    assert k["severity"] == "critical"
    assert k["adjudication"]["verdict"] == "contradicted"


def test_probe_error_retains_lexical_finding_unchanged():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    triage = V.check_entities(art, segs)

    def boom(prompt):
        raise RuntimeError("backend down")

    kept, discharged = V.adjudicate_entities(triage, art, segs, boom)
    assert discharged == []
    lex = [f for f in triage if f.get("name") == "Transition"][0]
    assert next(f for f in kept if f.get("name") == "Transition") == lex

    bad = _stub("maybe", "")
    kept2, discharged2 = V.adjudicate_entities(triage, art, segs, bad)
    assert discharged2 == []
    assert next(f for f in kept2 if f.get("name") == "Transition") == lex


def test_probe_none_preserves_v1_triage():
    art = _transition_article()
    segs = _segs("Constantine moved the Roman capital to Constantinople.")
    res = V.validate_typed(_decision(), _plan([]), _index(), art, segs, 60.0)
    assert res["status"] == "needs_review", res
    codes = {(f["code"], f["severity"]) for f in res["findings"]}
    assert ("unattested_entity", "critical") in codes
    assert res["discharged"] == []
    assert res["validator_version"] == V.VALIDATOR_VERSION


# ----------------------------------------------------------------------
# adversarial controls: strict protections never reach the probe
def test_invented_quantity_still_critical_without_probe():
    art = _article("recipe", [_sec(
        "s1", "H", "Add 540 grams of flour to the bowl.")])
    segs = _segs("add some flour to the bowl")
    out = [f for f in V.check_exact_values(art, segs, 60.0)
           if f["code"] == "unsupported_exact_value"]
    assert any(f["severity"] == "critical" and "'540 grams'" in f["detail"]
               for f in out), out


def test_invented_quantity_still_critical_with_probe():
    art = _article("recipe", [_sec(
        "s1", "H", "Add 540 grams of flour to the bowl.")])
    segs = _segs("add some flour to the bowl")
    res = V.validate_typed(_decision("recipe"), _plan([]), _index(),
                           art, segs, 60.0,
                           probe=_stub("entailed", "add some flour"))
    assert res["status"] == "needs_review", res
    assert any(f["code"] == "unsupported_exact_value"
               and f["severity"] == "critical" for f in res["findings"])


def test_genuinely_unsupported_name_contradicted_blocks():
    art = _article("report", [_sec(
        "s1", "H",
        "The fleet that commander Shea led at dawn sailed on.")])
    segs = _segs("commander Brien led the fleet at dawn and gave the order")
    res = V.validate_typed(_decision(), _plan([]), _index(), art, segs, 60.0,
                           probe=_stub("contradicted", "O'Brien commanded"))
    assert res["status"] == "needs_review", res
    k = next(f for f in res["findings"] if f.get("name") == "Shea")
    assert k["severity"] == "critical"
