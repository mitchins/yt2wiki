"""M6-Q §6 adversarial evaluator fixtures (no network, no GPU).

Each test names the defect it exposes.  Where the v1 scorer is blind to the
defect, the test asserts the v1 behavior explicitly (documenting the hole)
and then asserts v2 closes it.
"""

from __future__ import annotations

import pytest
pytest.importorskip("eval")

from eval.paired.compare import compare_articles, compare_articles_v2
from eval.paired.extract_reference import (
    EvidenceSegment,
    ReferenceFact,
    ReferenceFacts,
    VideoEvidenceCorpus,
    classify_facts_v2,
    extract_reference,
)


def _ref(facts, **kw):
    args = {"pair_id": "adv", "article_url": "https://example.com",
            "final_url": "https://example.com", "fetched_at": 1.0,
            "sha256": "x", "title": "T", "headings": ["Setup"],
            "blocks": [], "images": [], "facts": facts}
    args.update(kw)
    return ReferenceFacts(**args)


def _fact(kind, text, literal="", support="video_supported"):
    return ReferenceFact(fact_id=f"{kind}:{abs(hash(text)) % 9999}", kind=kind,
                         text=text, literal=literal, section="Setup",
                         locator="block-0", support=support, reason="")


def _corpus(*texts):
    return VideoEvidenceCorpus(
        segments=[EvidenceSegment(i * 10.0, i * 10.0 + 9.0, t)
                  for i, t in enumerate(texts)])


# 1. Empty denominators must yield N/A, not 1.00.
def test_empty_value_denominator_is_na_not_perfect():
    ref = _ref([_fact("action", "Create a database with a Due date property")])
    md = "## Procedure\n\n1. **Create a database with a Due date property.**\n"
    old = compare_articles(ref, markdown=md, findings=[])
    assert old.dimensions["exact_value_recall"] == 1.0  # v1 hole: vacuous perfect
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["exact_value_recall"]
    assert dim.score is None and not dim.applicable
    assert dim.numerator == 0 and dim.denominator == 0


# 2. Marketing copy must not become a scored action.
def test_testimonial_prose_is_article_only():
    facts = [ReferenceFact("a", "action",
                           "It saved me a ton of time trying to create something like this",
                           "", "Hype", "block-1", "uncertain", "")]
    out = classify_facts_v2(facts, _corpus("create something", "saved time"))
    assert out[0].support == "article_only"


# 3. Words scattered across unrelated sections must not count as a match.
def test_scattered_vocabulary_does_not_match_locally():
    ref = _ref([_fact("action", "Configure the state property as a formula block")])
    md = ("## Procedure\n\n1. **Configure the database view.**\n\n"
          "2. **Set the state property.**\n\n"
          "Some unrelated paragraph about formula blocks elsewhere.\n")
    old = compare_articles(ref, markdown=md, findings=[])
    assert old.dimensions["semantic_action_coverage"] == 1.0  # v1 hole: blob match
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["action_coverage_local"]
    assert dim.score == 0.0
    assert dim.numerator == 0 and dim.denominator == 1


# 4. Plausible instruction with no timestamped passage stays uncertain.
def test_unsupported_but_plausible_fact_keeps_evidence_trail():
    facts = [_fact("action", "Enable the nightly sync toggle in project settings")]
    out = classify_facts_v2(facts, _corpus("the weather today is pleasant",
                                           "project deadlines were extended"))
    assert out[0].support == "uncertain"
    assert "coverage" in out[0].reason
    assert out[0].evidence == ()


def test_supported_fact_carries_segment_evidence():
    facts = [_fact("action", "Create a Tasks database with a Due date property")]
    out = classify_facts_v2(
        facts, _corpus("welcome", "Create a Tasks database with a Due date property today"))
    assert out[0].support == "video_supported"
    assert out[0].evidence and out[0].evidence[0]["segs"] == [1]
    assert out[0].support_ts == (10.0, 19.0)


# 5. An irrelevant image beside correct text is not linked.
def test_irrelevant_image_is_not_linked():
    ref = _ref([], blocks=[{"kind": "image", "src": "x", "alt": "Raspberry Pi board revision",
                            "section": "Setup", "index": 0}])
    md = ("## Procedure\n\n1. **Install the operating system image.**\n\n"
          "![Company logo banner](assets/logo.png)\n")
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["image_relevance_linkage"]
    assert dim.score == 0.0
    assert dim.denominator == 1 and dim.numerator == 0


# 6. A promotional landing page yields no scorable recall dimensions.
def test_promo_page_has_no_scorable_recall():
    html = ("<html><head><title>Ultimate Thing</title></head><body><article>"
            "<h1>Ultimate Thing</h1>"
            "<p>Our world-class template will change your life. Join 50000 happy users!</p>"
            "<p>It saved me a ton of time. Highly recommended, five stars.</p>"
            "<p>Get this template for free today. Limited offer, act now!</p>"
            "</article></body></html>")
    ref = extract_reference("promo", "https://example.com", html)
    assert ref.facts, "expected boilerplate facts to be extracted"
    corpus = _corpus("ultimate template", "five stars")
    labelled = classify_facts_v2(ref.facts, corpus)
    assert all(f.support == "article_only" for f in labelled)
    ref2 = _ref(labelled)
    new = compare_articles_v2(ref2, markdown="## Procedure\n\n1. **Do the thing.**\n",
                              findings=[])
    na = {d.key for d in new.dimensions if not d.applicable}
    assert {"exact_value_recall", "command_config_preservation",
            "action_coverage_local"} <= na


# 7. A changed version between video and article is a contradiction.
def test_changed_version_is_a_contradiction():
    ref = _ref([_fact("value", "Set arm_freq=3000", "arm_freq=3000")])
    md = "## Procedure\n\n1. **Overclock the board.**\n\nSet `arm_freq=2600`.\n"
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["reference_contradictions"]
    assert dim.applicable and dim.score == 0.0
    assert dim.numerator == 0 and dim.denominator == 1
    assert any("arm_freq" in str(e) for e in dim.evidence)


# 8. Captionless video with a valid ASR transcript still supports facts.
def test_asr_transcript_supports_facts_without_captions():
    from eval.paired.extract_reference import corpus_from_transcript

    rows = [{"start": 12.0, "end": 18.0,
             "text": "Next, solder every single pin starting with the corners"},
            {"start": 18.0, "end": 24.0, "text": "Make sure the joints are shiny"}]
    corpus = corpus_from_transcript(rows)
    assert corpus.origin == "pipeline-asr"
    facts = [_fact("action", "Solder every single pin starting with the corners")]
    out = classify_facts_v2(facts, corpus)
    assert out[0].support == "video_supported"
    assert out[0].support_ts == (12.0, 18.0)


# 9. Destructive commands need nearby qualification.
def test_unqualified_destructive_command_is_flagged():
    ref = _ref([])
    md = ("## Procedure\n\n1. **Wipe the card.**\n\n"
          "```bash\nsudo dd if=image.img of=/dev/sda bs=4M\n```\n")
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["unqualified_destructive"]
    assert dim.score == 0.0
    assert any("dd" in str(e) for e in dim.evidence)
    fixed = md.replace("1. **Wipe the card.**",
                       "1. **Wipe the card.**\n\n> **Warning** — this destroys all data "
                       "on the target device. Double-check `of=` before running.")
    fixed_result = compare_articles_v2(ref, markdown=fixed, findings=[])
    dim2 = {d.key: d for d in fixed_result.dimensions}["unqualified_destructive"]
    assert dim2.score == 1.0


# 10. An image dump scores poorly even when reference images are covered.
def test_image_dump_penalised_by_precision():
    ref = _ref([], blocks=[
        {"kind": "image", "src": "a", "alt": "Tasks database setup view",
         "section": "Setup", "index": 0},
        {"kind": "image", "src": "b", "alt": "Due date property configuration",
         "section": "Setup", "index": 1}])
    steps = "".join(
        f"{i}. **Do thing number {i} with the tasks database setup view.**\n\n"
        f"![Tasks database setup view {i}](assets/{i}.png)\n\n" for i in range(1, 27))
    md = "## Procedure\n\n" + steps
    new = compare_articles_v2(ref, markdown=md, findings=[])
    dim = {d.key: d for d in new.dimensions}["image_relevance_linkage"]
    assert dim.applicable and dim.denominator == 2
    assert dim.score < 0.5, dim.to_dict()


# 11. A sentence echoing the document title is preserved but non-material.
def test_title_echo_is_preserved_but_non_material():
    from eval.paired.disposition import NON_INSTRUCTIONAL, REQUIRED_INSTRUCTION
    from eval.paired.disposition import assign_dispositions
    html = ("<html><head><title>How to install TrueNAS on a Raspberry Pi</title></head>"
            "<body><article><h1>How to install TrueNAS on a Raspberry Pi</h1>"
            "<p>How to install TrueNAS on a Raspberry Pi</p>"
            "<p>Install the TrueNAS installer image and configure the USB drive.</p>"
            "</article></body></html>")
    ref = extract_reference("t", "https://example.com", html)
    by_text = {f.text: f for f in ref.facts}
    assert "How to install TrueNAS on a Raspberry Pi" in by_text  # preserved
    disp = {d.fact_id: d.disposition for d in assign_dispositions(
        [vars(f) for f in ref.facts], title=ref.title)}
    echo = by_text["How to install TrueNAS on a Raspberry Pi"]
    assert disp[echo.fact_id] == NON_INSTRUCTIONAL
    real = by_text["Install the TrueNAS installer image and configure the USB drive."]
    assert disp[real.fact_id] == REQUIRED_INSTRUCTION


# 12. First-person anecdotes are preserved as context, not warnings.
def test_anecdote_is_context_not_warning():
    from eval.paired.disposition import (
        CONTEXTUAL_EXPLANATION,
        SAFETY_OR_COMPATIBILITY_WARNING,
    )
    from eval.paired.disposition import assign_dispositions
    html = ("<html><head><title>T</title></head><body><article>"
            "<p>I don't learn much when a project goes off without a hitch, "
            "so set arm_freq=3000 carefully.</p>"
            "<p>Be careful: deleting the database cannot be undone.</p>"
            "</article></body></html>")
    ref = extract_reference("t", "https://example.com", html)
    assert any(f.kind == "warning" for f in ref.facts)  # preserved verbatim
    disp = {d.fact_id: d.disposition for d in assign_dispositions(
        [vars(f) for f in ref.facts], title=ref.title)}
    for f in ref.facts:
        if f.kind == "warning" and f.text.startswith("I don't learn"):
            assert disp[f.fact_id] == CONTEXTUAL_EXPLANATION
        if f.kind == "warning" and "deleting the database" in f.text:
            assert disp[f.fact_id] == SAFETY_OR_COMPATIBILITY_WARNING
    assert any(f.kind == "value" and f.literal == "arm_freq=3000" for f in ref.facts)


# 13. An on-screen-only value observed in evidence records is supportable.
def test_evidence_literal_supports_onscreen_value():
    from eval.paired.extract_reference import classify_facts_v2
    facts = [_fact("value", "Set arm_freq=3000", "arm_freq=3000")]
    out = classify_facts_v2(
        facts, _corpus("welcome to the video", "today we overclock"),
        [{"literal": "arm_freq=3000", "evidence_id": "ev1", "t_start": 120.5}])
    assert out[0].support == "video_supported"
    assert out[0].evidence[0]["evidence_id"] == "ev1"
    assert out[0].support_ts == (120.5, 120.5)


# 14. Evidence literals never rescue fuzzy action matching.
def test_evidence_literals_do_not_support_actions():
    from eval.paired.extract_reference import classify_facts_v2
    facts = [_fact("action", "Enable the nightly sync toggle in project settings")]
    out = classify_facts_v2(
        facts, _corpus("the weather today is pleasant"),
        [{"literal": "sync toggle", "evidence_id": "ev9", "t_start": 5.0}])
    assert out[0].support == "uncertain"


# 15. M7-A §2 regression: exact TrueNAS narrative material gets non-material
# dispositions while the genuine outcome stays material.
def test_truenas_narrative_dispositions():
    from eval.paired.disposition import (
        CONTEXTUAL_EXPLANATION,
        NON_INSTRUCTIONAL,
        TECHNICAL_OUTCOME,
        assign_dispositions,
    )

    title = "How to install TrueNAS on a Raspberry Pi - Jeff Geerling"
    facts = [
        {"fact_id": "title", "kind": "action",
         "text": "How to install TrueNAS on a Raspberry Pi"},
        {"fact_id": "amp", "kind": "warning",
         "text": "I currently run an Ampere Arm server in my rack with Linux and ZFS."},
        {"fact_id": "hitch", "kind": "warning",
         "text": "I don't learn much when a project goes off without a hitch."},
        {"fact_id": "joke", "kind": "action",
         "text": "Or you could be boring and just install TrueNAS on x86, "
                  "where it's fully supported ;)"},
        {"fact_id": "edk2", "kind": "outcome",
         "text": "You should see a Raspberry Pi logo and the EDK2 bootloader screen appear."},
        {"fact_id": "uefi", "kind": "action",
         "text": "Now that the Pi is booting into UEFI mode, you can install TrueNAS."},
        {"fact_id": "nano", "kind": "command",
         "text": "sudo nano /etc/default/rpi-eeprom-update",
         "literal": "sudo nano /etc/default/rpi-eeprom-update"},
    ]
    disp = {d.fact_id: d.disposition for d in assign_dispositions(facts, title=title)}
    assert disp["title"] == NON_INSTRUCTIONAL
    assert disp["amp"] == CONTEXTUAL_EXPLANATION
    assert disp["hitch"] == CONTEXTUAL_EXPLANATION
    assert disp["joke"] == NON_INSTRUCTIONAL
    assert disp["edk2"] == TECHNICAL_OUTCOME
    from eval.paired.disposition import REQUIRED_INSTRUCTION
    assert disp["uefi"] == REQUIRED_INSTRUCTION
    assert disp["nano"] == REQUIRED_INSTRUCTION


# 16. Material completeness counts only material supported facts.
def test_material_summary_separates_context():
    from eval.paired.disposition import material_summary
    comparison = {"fact_outcomes": [
        {"fact_id": "a", "status": "hit"},
        {"fact_id": "b", "status": "miss"},
        {"fact_id": "c", "status": "hit"},
        {"fact_id": "d", "status": "uncertain_miss"},
        {"fact_id": "e", "status": "excluded"},
    ]}
    dispositions = [
        {"fact_id": "a", "disposition": "REQUIRED_INSTRUCTION"},
        {"fact_id": "b", "disposition": "REQUIRED_INSTRUCTION"},
        {"fact_id": "c", "disposition": "CONTEXTUAL_EXPLANATION"},
        {"fact_id": "d", "disposition": "REQUIRED_INSTRUCTION"},
        {"fact_id": "e", "disposition": "REQUIRED_INSTRUCTION"},
    ]
    m = material_summary(comparison, dispositions)
    assert (m["material_hits"], m["material_total"]) == (1, 2)
    assert m["material_completeness"] == 0.5
    assert (m["contextual_hits"], m["contextual_total"]) == (1, 1)
    assert m["material_misses"] == ["b"]
