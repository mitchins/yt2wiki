"""Shared semantic block renderers: markdown fallback + sanitised HTML.

Deterministic only. Every block type renders to portable Markdown (no
pipe tables — the article renderer has no table support) and to escaped
HTML for the reader lead slot. Model-shaped hostile input must never
produce raw markup, scripts or links.
"""

from __future__ import annotations

from yt2wiki.v9 import blocks as B


def test_all_block_types_validate_and_render():
    samples = [
        {"block": "paragraph", "text": "A short lede."},
        {"block": "list", "items": ["one", "two"]},
        {"block": "key_facts",
         "facts": [{"label": "Impact", "value": "2022-09-26"},
                   {"label": "Target", "value": "Dimorphos"}]},
        {"block": "ingredient_list",
         "dishes": [{"dish": "Loaf",
                     "items": [{"amount": "4 1/2 cups", "item": "flour"},
                               {"amount": "", "item": "water",
                                "note": "lukewarm"}]}]},
        {"block": "verdict",
         "statement": "The best upgrade is the screen.",
         "attribution": "LTT review",
         "context": "At the same price."},
        {"block": "position_list",
         "positions": [{"stance": "Close",
                        "summary": "RSI is near.",
                        "speakers": ["Researcher A"]}]},
        {"block": "measured_comparison",
         "headers": ["Title", "Deck", "OLED"],
         "rows": [["FPS", "60", "61"]]},
    ]
    for sample in samples:
        assert B.validate_block(sample) == [], sample
        md = B.render_markdown(sample)
        assert md and "<" not in md.replace("->", ""), sample
        html = B.render_html(sample)
        assert html and "<script" not in html, sample


def test_unknown_block_fails_closed():
    assert B.validate_block({"block": "star_rating", "stars": 5})
    assert B.render_html({"block": "star_rating"}) == ""


def test_hostile_input_is_escaped_not_rendered():
    evil = {"block": "verdict",
            "statement": '<script>alert(1)</script> [x](javascript:alert(1))',
            "attribution": '<img src=x onerror=alert(2)>'}
    assert B.validate_block(evil) == []
    out = B.render_html(evil)
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&lt;img" in out
    md = B.render_markdown(evil)
    # Markdown is plain text (same contract as all article prose): angle
    # brackets survive here and are escaped by markdown_to_html at render.
    from yt2wiki.article import markdown_to_html
    assert "<script" not in markdown_to_html(md)
    assert "<img" not in markdown_to_html(md)


def test_measured_comparison_has_no_pipe_tables():
    block = {"block": "measured_comparison",
             "headers": ["A", "B"], "rows": [["1", "2"]]}
    assert "|" not in B.render_markdown(block)
    html = B.render_html(block)
    assert "<table" in html and 'scope="col"' in html


def test_rows_must_match_headers():
    assert B.validate_block({"block": "measured_comparison",
                             "headers": ["A", "B"], "rows": [["only"]]})
    assert B.validate_block({"block": "ingredient_list", "dishes": []})


def test_verdict_needs_attribution():
    assert B.validate_block({"block": "verdict", "statement": "Great."})
    assert B.validate_block({"block": "verdict", "statement": "Great.",
                             "attribution": "Reviewer"}) == []


def test_empty_blocks_render_nothing():
    assert B.render_html({"block": "paragraph", "text": "  "}) == "<p></p>"
    assert B.render_html({"block": "list", "items": []}) == ""


def test_zero_values_survive_verbatim():
    block = {"block": "key_facts",
             "facts": [{"label": "Failures", "value": 0},
                       {"label": "Delta", "value": 0.0}]}
    assert B.validate_block(block) == []
    assert "0" in B.render_markdown(block)
    assert ">0<" in B.render_html(block)


def test_empty_cells_keep_column_positions():
    block = {"block": "measured_comparison",
             "headers": ["A", "B", "C"], "rows": [["1", "", "3"]]}
    assert B.validate_block(block) == []
    assert "1 ·  · 3" in B.render_markdown(block)
    assert B.render_html(block).count("<td>") == 3


def test_dish_without_items_fails():
    assert B.validate_block(
        {"block": "ingredient_list",
         "dishes": [{"dish": "Loaf", "items": []}]})
