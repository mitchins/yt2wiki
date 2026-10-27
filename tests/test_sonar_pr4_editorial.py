"""PR4 (Sonar S3776 pipeline/editorial refactors): invariant pins.

Existing suites cover the refactored functions end-to-end (test_basics and
the reader tests for markdown_to_html; test_verification/test_citations for
the rule finders; test_pipeline for run_job; test_v9judge_route for
visual_interpret and _consolidate). These tests pin the riskiest editorial
invariants of the restructured code. No models, no network.
"""

from __future__ import annotations

from yt2wiki.article import markdown_to_html


def test_caption_inside_list_closes_list_and_becomes_paragraph():
    """A *caption* line inside an open list is paragraph text (base behaviour)."""
    md = "- a\n*Caption line*\n- b"
    html = markdown_to_html(md)
    assert html.count('<ul class="plain">') == 2      # the caption split the list
    assert "<p><em>Caption line</em></p>" in html
    assert "<figcaption>" not in html


def test_unclosed_fence_at_eof_renders_code_block():
    md = "intro\n```bash\nmake install"
    html = markdown_to_html(md)
    assert '<pre data-lang="bash">' in html
    assert "make install" in html


def test_unsafe_figure_target_is_dropped_entirely():
    html = markdown_to_html("![x](javascript:alert(1))")
    assert "<figure>" not in html
    assert "alert" not in html


def test_list_type_switch_closes_previous_list():
    md = "- a\n- b\n1. one\n2. two"
    html = markdown_to_html(md)
    assert html.index("</ul>") < html.index('<ol class="steps">')
    assert html.count('<ul class="plain">') == 1


def test_rule_ladder_codes_and_details():
    """broken_citation -> mismatched -> unsupported stays per-sentence, in order."""
    from yt2wiki.pipeline.stages.verify import _rule_unsupported
    from yt2wiki.records import Evidence

    ev = [
        Evidence(evidence_id="ev-aaa111", kind="action",
                 text="Insert the USB drive into the Pi and boot it up",
                 t_start=214.0, t_end=219.0),
        Evidence(evidence_id="ev-bbb222", kind="config",
                 text="Set arm frequency to 3000 megahertz",
                 t_start=100.0, t_end=105.0),
    ]
    md = ("## Procedure\n\n"
          "1. **Boot.**\n\n"
          "   Insert the USB drive into the Pi and boot it up."
          " <!-- cite: step=1 ev=ev-zzz999 t=214-219 frames=- -->\n\n"
          "2. **Clock.**\n\n"
          "   Run the installer from the USB drive and reboot."
          " <!-- cite: step=2 ev=ev-bbb222 t=100-105 frames=- -->\n\n"
          "3. **Unknown.**\n\n"
          "   Whisper into the modem and chant the firmware mantra."
          " <!-- cite: step=3 ev=ev-bbb222 t=100-105 frames=- -->\n")
    findings = _rule_unsupported(None, md, ev)
    codes = [f["code"] for f in findings]
    assert codes[0] == "broken_citation"   # emitted before sentence findings
    assert {"mismatched_citation", "unsupported_action"} <= set(codes)
    # detail fields the verifier relies on survive the refactor
    mismatched = next(f for f in findings if f["code"] == "mismatched_citation")
    assert mismatched["detail"]["supported_by"] == "ev-aaa111"
    assert mismatched["detail"]["cited"] == ["ev-bbb222"]
    unsupported = next(f for f in findings if f["code"] == "unsupported_action")
    assert unsupported["severity"] == "critical"


def test_missing_citation_info_for_supported_uncited_claim():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported
    from yt2wiki.records import Evidence

    ev = [Evidence(evidence_id="ev-aaa111", kind="action",
                   text="Insert the USB drive into the Pi and boot it up",
                   t_start=214.0, t_end=219.0)]
    md = ("## Procedure\n\n1. **Boot.**\n\n"
          "   Insert the USB drive into the Pi and boot it up.\n")
    findings = _rule_unsupported(None, md, ev)
    info = [f for f in findings if f["code"] == "missing_citation"]
    assert info
    assert info[0]["severity"] == "info"
