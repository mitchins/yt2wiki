"""PR C — cognitive-complexity refactor regression tests.

Pins the behaviour of the extracted helpers and the branches they newly
expose: caption priority, base-stage skip matrix, literal collection,
timestamp-candidate parsing, title validation/banking, header
normalisation and block-builder flows. No models, no network.
"""

from __future__ import annotations

import pytest

from yt2wiki.literals import _LiteralCollector, extract_literals
from yt2wiki.pipeline.stages.verify import _BlockBuilder, _citation_blocks
from yt2wiki.v9.publish import (
    _citation_candidates,
    _clock_values,
    _CLOCK_RE,
    _range_values,
    _RANGE_RE,
)
from yt2wiki.v9.run import _base_stage_reusable, _pick_caption, assign_captions
from yt2wiki.v9.writer import (
    _article_header,
    _SectionInvalid,
    _title_validate_response,
)


# --- caption priority -------------------------------------------------------
def test_pick_caption_prefers_preserved():
    cap, alt, source = _pick_caption(
        {"frame_id": "f1"}, {"caption": "Human cap", "alt_text": "Human alt"},
        True, "Heading")
    assert (cap, alt, source) == ("Human cap", "Human alt", "edited")


def test_pick_caption_preserved_without_alt():
    cap, alt, source = _pick_caption(
        {"frame_id": "f1"}, {"caption": "Human cap"}, True, "Heading")
    assert (cap, alt, source) == ("Human cap", "", "edited")


def test_pick_caption_keeps_genuine_vision():
    pick = {"caption": "A valve on a pipe. Extra.", "caption_source": "vision"}
    cap, alt, source = _pick_caption(pick, None, True, "Heading")
    assert cap == "A valve on a pipe. Extra."
    assert alt == "A valve on a pipe"
    assert source == "vision"


def test_pick_caption_keeps_existing_vision_alt():
    pick = {"caption": "Valve.", "alt_text": "Kept", "caption_source": "vision"}
    assert _pick_caption(pick, None, True, "H") == ("Valve.", "Kept", "vision")


def test_pick_caption_neutral_fallback():
    cap, alt, source = _pick_caption({"frame_id": "f"}, None, False, "Pump")
    assert source == "auto"
    assert "Pump" in cap or "Pump" in alt


def test_pick_caption_vision_gated_off():
    pick = {"caption": "A valve.", "caption_source": "vision"}
    assert _pick_caption(pick, None, False, "H")[2] == "auto"


def test_assign_captions_end_to_end():
    sections = [{"section_id": "s1", "heading": "Pump"}]
    picks = {"s1": [
        {"frame_id": "a", "caption": "V.a", "caption_source": "vision"},
        {"frame_id": "b", "caption": "stale", "caption_source": "rank"},
    ]}
    assign_captions(sections, picks, {"a": {"caption": "H", "alt_text": ""}},
                    enable_captions=True)
    by_id = {p["frame_id"]: p for p in picks["s1"]}
    assert by_id["a"]["caption_source"] == "edited"
    assert by_id["b"]["caption_source"] == "auto"


# --- base-stage skip matrix ---------------------------------------------------
@pytest.mark.parametrize("name,n_trans,n_frames,video_ok,want", [
    ("acquisition", 5, 0, True, True),
    ("acquisition", 5, 0, False, False),
    ("acquisition", 0, 0, True, False),
    ("inspect", 5, 0, False, True),
    ("transcribe", 5, 0, False, True),
    ("inspect", 0, 0, True, False),
    ("frame_candidates", 0, 3, False, True),
    ("frame_candidates", 5, 0, True, False),
    ("unknown_stage", 5, 5, True, False),
])
def test_base_stage_reusable_matrix(name, n_trans, n_frames, video_ok, want):
    assert _base_stage_reusable(name, n_trans, n_frames, video_ok) is want


# --- literal collector ----------------------------------------------------------
def test_collector_dedupes_and_claims():
    acc = _LiteralCollector()
    acc.add("ip", "192.168.1.1", "ctx", (0, 11))
    acc.add("ip", "192.168.1.1", "ctx", (0, 11))
    acc.add("version", "192.168.1.1", "ctx", (0, 11))
    assert len(acc.out) == 2  # same kind+value deduped, other kind kept
    assert not acc.free(3, 5)  # inside the claimed span
    assert acc.free(11, 20)  # boundary start is free


def test_collector_rejects_short_and_quoted():
    acc = _LiteralCollector()
    acc.add("flag", "x", "ctx")
    acc.add("flag", "'--ok'", "ctx")
    assert [lit.literal for lit in acc.out] == ["--ok"]


def test_extract_literals_still_extracts():
    lits = extract_literals("Connect to 192.168.1.1 on port 8080, 5V rail.")
    by_kind = {lit.kind for lit in lits}
    assert {"ip_address", "port", "measurement"} <= by_kind


# --- timestamp candidates ----------------------------------------------------------
def test_plain_numbers():
    assert _citation_candidates("159") == [159.0]
    assert _citation_candidates("159.5") == [159.5]


def test_clock_values_branches():
    assert _clock_values(_CLOCK_RE.fullmatch("1:02:03"), loose_seconds=False) == [3723]
    assert _clock_values(_CLOCK_RE.fullmatch("7:83"), loose_seconds=False) == [783]
    assert _clock_values(_CLOCK_RE.fullmatch("2:30"), loose_seconds=False) == [150.0]
    assert _clock_values(_CLOCK_RE.fullmatch("159:0"), loose_seconds=False) == [9540.0, 159.0]
    plain = _clock_values(_CLOCK_RE.fullmatch("13:00"), loose_seconds=False)
    assert plain == [780.0]
    loosened = _clock_values(_CLOCK_RE.fullmatch("13:00"), loose_seconds=True)
    assert loosened == [780.0, 13.0]


def test_range_values_branches():
    got = _range_values(_RANGE_RE.fullmatch("13:00-20:00"))
    assert got == [780.0, 1200.0, 13.0, 20.0]
    assert _range_values(_RANGE_RE.fullmatch("1:02-3:04")) == [62.0, 184.0, 1.2, 3.4]
    assert _range_values(_RANGE_RE.fullmatch("7:83-1:02")) == [503.0, 62.0, 783.0, 1.2]


def test_citation_candidates_invalid():
    assert _citation_candidates("not a time") == []
    assert _citation_candidates("") == []


def test_citation_candidates_prefers_clock_over_range():
    # "1:02" is a clock label, not a degenerate range.
    assert _citation_candidates("1:02") == [62.0]


# --- title validation ---------------------------------------------------------------
def test_title_validate_rejects_construction_promise():
    saved = {"title": "", "kind": "explainer"}
    with pytest.raises(_SectionInvalid):
        _title_validate_response(
            {"title": "Build a deck today", "summary": "Steps.", "kind": "procedure"},
            0, saved)


def test_title_validate_banks_valid_title_on_editorial():
    saved = {"title": "", "kind": "explainer"}
    with pytest.raises(_SectionInvalid) as exc:
        _title_validate_response(
            {"title": "Quiet overview",
             "summary": "In this video we will show you",
             "kind": "procedure"},
            0, saved)
    assert exc.value.category == "editorial"
    assert saved["title"] == "Quiet overview"


def test_title_validate_passes_clean():
    saved = {"title": "", "kind": "explainer"}
    out = _title_validate_response(
        {"title": "Quiet overview", "summary": "The valve sits left."},
        0, saved)
    assert out["title"] == "Quiet overview"
    assert saved["title"] == ""


def test_article_header_normalises_kind():
    title, summary, kind = _article_header(
        {"title": "T", "summary": "S", "kind": "HOWTO"}, "Video")
    assert (title, summary, kind) == ("T", "S", "explainer")
    assert _article_header({"title": "", "summary": ""}, "Video")[0] == "Video"


# --- block builder ---------------------------------------------------------------
def test_block_builder_flows():
    md = ("## Section\n\n1. First step\n> a warning\n\nplain prose\n\n"
          "## Other\n\nstandalone quote\n> quoted\n")
    blocks = _citation_blocks(md)
    kinds = [(b["kind"], b.get("step"), b["section"]) for b in blocks]
    assert ("step", 1, "Section") in kinds
    assert any(k == "prose" for k, _, _ in kinds)
    assert any(k == "callout" for k, _, _ in kinds)


def test_block_builder_callout_stays_in_step():
    b = _BlockBuilder()
    b.feed("1. Do the thing")
    b.feed("> careful now")
    assert len(b.result()) == 1
    assert b.result()[0]["kind"] == "step"


def test_block_builder_standalone_callout():
    b = _BlockBuilder()
    b.feed("> careful now")
    res = b.result()
    assert len(res) == 1
    assert res[0]["kind"] == "callout"


def test_block_builder_blank_lines_never_split():
    b = _BlockBuilder()
    b.feed("1. Step one")
    b.feed("")
    b.feed("more of step one")
    res = b.result()
    assert len(res) == 1
    assert len(res[0]["lines"]) == 3
