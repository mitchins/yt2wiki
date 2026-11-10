"""Shared semantic lead/body blocks for typed articles (PR A).

Blocks are plain data produced by the editorial pass (PR B) and rendered
to two targets from a single definition:

- reader-safe Markdown (stored in article.md, converted by the existing
  ``article.markdown_to_html`` subset — notably *without* pipe tables);
- sanitised HTML for the type-specific lead component in the reader.

The Markdown target is plain text: angle brackets and Markdown syntax
in field values pass through as literal characters, exactly like all
other article prose, and ``article.markdown_to_html`` HTML-escapes them
at render time. The HTML target escapes every interpolated string, so
no caller-supplied markup reaches the reader DOM.
"""

from __future__ import annotations

import html
from typing import Any

#: Block types the typed renderer knows. Unknown blocks fail validation
#: instead of rendering as something else.
BLOCK_TYPES = (
    "paragraph",
    "list",
    "key_facts",
    "ingredient_list",
    "verdict",
    "position_list",
    "measured_comparison",
)


def _text(value: Any) -> str:
    """Verbatim text for a field: None becomes empty, zero stays "0".

    No character truncation: exact values (quantities, names, measures)
    must survive rendering verbatim. List growth is bounded by count
    caps in _items/_cells, not by cutting values short.
    """
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _items(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value
            if v is not None and str(v).strip()][:limit]


def _cells(value: Any, limit: int = 8) -> list[str]:
    """Table cells: empty strings are valid measurements (unknown value).

    Unlike _items, blanks are preserved so rows keep their column
    positions; only None entries are dropped.
    """
    if not isinstance(value, list):
        return []
    return ["" if v is None else str(v).strip() for v in value][:limit]


def validate_block(block: Any) -> list[str]:
    """Structural validation for one block; empty list means renderable."""
    if not isinstance(block, dict):
        return ["block must be an object"]
    kind = block.get("block")
    if kind not in BLOCK_TYPES:
        return [f"unknown block type {kind!r}"]
    if kind == "paragraph":
        return [] if _text(block.get("text")) else ["paragraph needs text"]
    if kind == "list":
        return [] if _items(block.get("items")) else ["list needs items"]
    if kind == "key_facts":
        facts = block.get("facts")
        if not isinstance(facts, list) or not facts:
            return ["key_facts needs facts"]
        for f in facts:
            if not isinstance(f, dict) or not _text(f.get("label")) \
                    or not _text(f.get("value")):
                return ["key_facts entries need label and value"]
        return []
    if kind == "ingredient_list":
        dishes = block.get("dishes")
        if not isinstance(dishes, list) or not dishes:
            return ["ingredient_list needs dishes"]
        for d in dishes:
            if not isinstance(d, dict) or not _text(d.get("dish")):
                return ["ingredient_list dishes need a dish name"]
            if not isinstance(d.get("items"), list) or not d.get("items"):
                return ["ingredient_list dishes need at least one item"]
            for it in d.get("items") or []:
                if not isinstance(it, dict) or not _text(it.get("item")):
                    return ["ingredient_list items need an item name"]
        return []
    if kind == "verdict":
        if not _text(block.get("statement")):
            return ["verdict needs a statement"]
        if not _text(block.get("attribution")):
            return ["verdict needs an attribution"]
        return []
    if kind == "position_list":
        positions = block.get("positions")
        if not isinstance(positions, list) or not positions:
            return ["position_list needs positions"]
        for p in positions:
            if not isinstance(p, dict) or not _text(p.get("summary")):
                return ["position_list entries need a summary"]
        return []
    if kind == "measured_comparison":
        headers = _items(block.get("headers"), limit=8)
        rows = block.get("rows")
        if not headers or not isinstance(rows, list) or not rows:
            return ["measured_comparison needs headers and rows"]
        for r in rows:
            cells = _cells(r, limit=8) if isinstance(r, list) else []
            if len(cells) != len(headers):
                return ["measured_comparison rows must match headers"]
        return []
    return [f"unhandled block type {kind!r}"]


def render_markdown(block: dict[str, Any]) -> str:
    """Portable Markdown for a validated block.

    Tables become nested lists on purpose: ``article.markdown_to_html``
    has no pipe-table support and must not gain silently untested
    syntax through this path.
    """
    kind = block.get("block")
    if kind == "paragraph":
        return _text(block.get("text"))
    if kind == "list":
        return "\n".join(f"* {_text(i)}" for i in _items(block.get("items")))
    if kind == "key_facts":
        lines = []
        for f in block.get("facts") or []:
            lines.append(f"* **{_text(f.get('label'))}:** {_text(f.get('value'))}")
        return "\n".join(lines)
    if kind == "ingredient_list":
        lines = []
        for d in block.get("dishes") or []:
            lines.append(f"**{_text(d.get('dish'))}**")
            for it in d.get("items") or []:
                amount = _text(it.get("amount"))
                item = _text(it.get("item"))
                note = _text(it.get("note"))
                cell = f"{amount} {item}".strip()
                if note:
                    cell += f" ({note})"
                lines.append(f"* {cell}")
            lines.append("")
        return "\n".join(lines).rstrip()
    if kind == "verdict":
        statement = _text(block.get("statement"))
        attribution = _text(block.get("attribution"))
        context = _text(block.get("context"))
        out = f"> {statement}\n>\n> — {attribution}"
        if context:
            out += f"\n>\n> {context}"
        return out
    if kind == "position_list":
        lines = []
        for p in block.get("positions") or []:
            stance = _text(p.get("stance"))
            summary = _text(p.get("summary"))
            speakers = ", ".join(_items(p.get("speakers"), limit=6))
            head = f"**{stance}**" if stance else "* Position"
            if speakers:
                head += f" ({speakers})"
            lines.append(f"{head}: {summary}")
            lines.append("")
        return "\n".join(lines).rstrip()
    if kind == "measured_comparison":
        headers = _items(block.get("headers"), limit=8)
        lines = [f"**{' / '.join(headers)}**"]
        for r in block.get("rows") or []:
            cells = _cells(r, limit=8)
            lines.append("* " + " · ".join(cells))
        return "\n".join(lines)
    return ""


def render_html(block: dict[str, Any]) -> str:
    """Sanitised lead-component HTML for a validated block.

    Every interpolated value is escaped; no caller-supplied markup is
    passed through. Used by the reader lead slot (PR A) with data that
    PR B will produce; unknown blocks render nothing.
    """
    e = html.escape
    kind = block.get("block")
    if kind == "paragraph":
        return f"<p>{e(_text(block.get('text')))}</p>"
    if kind == "list":
        lis = "".join(f"<li>{e(i)}</li>" for i in _items(block.get("items")))
        return f"<ul class=\"lead-list\">{lis}</ul>" if lis else ""
    if kind == "key_facts":
        terms = "".join(
            f"<dt>{e(_text(f.get('label')))}</dt><dd>{e(_text(f.get('value')))}</dd>"
            for f in block.get("facts") or []
            if isinstance(f, dict))
        return f"<dl class=\"kv lead-facts\">{terms}</dl>" if terms else ""
    if kind == "ingredient_list":
        groups = []
        for d in block.get("dishes") or []:
            if not isinstance(d, dict):
                continue
            lis = "".join(
                f"<li>{e((_text(it.get('amount')) + ' ' + _text(it.get('item'))).strip())}"
                + (f" <span class=\"muted\">({e(_text(it.get('note')))})</span>"
                   if _text(it.get("note")) else "") + "</li>"
                for it in d.get("items") or [] if isinstance(it, dict))
            if lis:
                groups.append(f"<h3>{e(_text(d.get('dish')))}</h3>"
                              f"<ul class=\"lead-ingredients\">{lis}</ul>")
        return "".join(groups)
    if kind == "verdict":
        statement = e(_text(block.get("statement")))
        attribution = e(_text(block.get("attribution")))
        context = e(_text(block.get("context")))
        extra = f"<p class=\"verdict-context\">{context}</p>" if context else ""
        return (f"<aside class=\"lead-verdict\"><p>{statement}</p>"
                f"<p class=\"verdict-by\">— {attribution}</p>{extra}</aside>"
                if statement and attribution else "")
    if kind == "position_list":
        cards = []
        for p in block.get("positions") or []:
            if not isinstance(p, dict):
                continue
            stance = e(_text(p.get("stance")))
            summary = e(_text(p.get("summary")))
            speakers = e(", ".join(_items(p.get("speakers"), limit=6)))
            head = f"<h3>{stance}</h3>" if stance else ""
            by = f"<p class=\"pos-by\">{speakers}</p>" if speakers else ""
            if summary:
                cards.append(f"<div class=\"pos\">{head}{by}<p>{summary}</p></div>")
        return f"<div class=\"lead-positions\">{''.join(cards)}</div>" if cards else ""
    if kind == "measured_comparison":
        headers = _items(block.get("headers"), limit=8)
        rows = block.get("rows") or []
        if not headers or not rows:
            return ""
        th = "".join(f"<th scope=\"col\">{e(h)}</th>" for h in headers)
        trs = []
        for r in rows:
            cells = _cells(r, limit=8) if isinstance(r, list) else []
            if len(cells) != len(headers):
                continue
            tds = "".join(f"<td>{e(c)}</td>" for c in cells)
            trs.append(f"<tr>{tds}</tr>")
        if not trs:
            return ""
        return (f"<table class=\"lead-table\"><thead><tr>{th}</tr></thead>"
                f"<tbody>{''.join(trs)}</tbody></table>")
    return ""
