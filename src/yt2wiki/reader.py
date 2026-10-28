"""Reader presentation: split a published article and enhance its HTML.

Shared by the web reader and the standalone export so both present the
same structure (sections with anchors, figures with captions, timestamp
links, callouts). Presentation only: nothing here adds, removes or rewords
article content, except that images whose asset file is missing are
dropped rather than shown broken.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Callable

_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
_ASSETS_PREFIX = "assets/"
_TS_LABEL = re.compile(rf"^{_TS}(?:\s*[-–]\s*{_TS})?$")

_FIGURE = re.compile(
    r'<figure><img alt="([^"]*)" src="([^"]+)"></figure>'
    r'(?:\s*<figcaption>(.*?)</figcaption>)?',
    re.S,
)
_LINK = re.compile(r'(!?)<a href="([^"]*)">(.*?)</a>', re.S)
_BARE_TS = re.compile(rf"(?<![\w>\]])\[({_TS}(?:\s*[-–]\s*{_TS})?)\](?!\()")
_SOURCE_AT = re.compile(r"^(.*?)\s+—\s+source at\s+(.+)$", re.S)
_CALLOUT = re.compile(
    r"<blockquote><p>\s*<strong>(Warning|Caution|Important|Tip|Note)</strong>",
    re.I,
)


@dataclass
class ArticleParts:
    title: str = ""
    attribution: list[str] = field(default_factory=list)
    lead_md: str = ""
    body_md: str = ""


@dataclass
class TocEntry:
    anchor: str
    text: str


def split_markdown(md: str) -> ArticleParts:
    """Split into title, leading attribution quote, lead text and body.

    The lead is everything between the attribution and the first `##`
    heading; when there is no `##` heading the whole remainder is body.
    """
    lines = md.replace("\r\n", "\n").split("\n")
    parts = ArticleParts()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.match(r"^#\s+", lines[i]):
        parts.title = lines[i].lstrip("#").strip()
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and lines[i].strip().startswith(">"):
        text = lines[i].strip().lstrip(">").strip()
        if text:
            parts.attribution.append(text)
        i += 1
    rest = lines[i:]
    first_h2 = next((k for k, ln in enumerate(rest) if ln.startswith("## ")), None)
    if first_h2 is None:
        parts.body_md = "\n".join(rest).strip("\n")
    else:
        parts.lead_md = "\n".join(rest[:first_h2]).strip("\n")
        parts.body_md = "\n".join(rest[first_h2:]).strip("\n")
    return parts


def lead_summary(md: str, limit: int = 240) -> str:
    """First lead paragraph as plain text, for library cards."""
    parts = split_markdown(md)
    source = parts.lead_md or parts.body_md
    for block in re.split(r"\n\s*\n", source):
        block = block.strip()
        if not block or block.startswith(("#", "!", ">", "```", "*Source", "|")):
            continue
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", block)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[*_`]", "", text)
        text = re.sub(rf"\s*\[{_TS}(?:\s*[-–]\s*{_TS})?\]", "", text)
        text = " ".join(text.split())
        if len(text) > limit:
            cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
            text = cut + "…"
        return text
    return ""


def slugify(text: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "section"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def _strip_tags(fragment: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _caption_html(caption: str) -> str:
    """Restrained caption: 'Source frame' label plus a timestamp link."""
    m = _SOURCE_AT.match(caption.strip())
    if m:
        label, link = m.group(1).strip(), m.group(2).strip()
        quiet = " quiet" if label.lower() == "source frame" else ""
        return (f'<span class="cap-label{quiet}">{label}</span>'
                f'<span class="cap-ts">{link}</span>')
    quiet = " quiet" if caption.strip().lower() == "source frame" else ""
    return f'<span class="cap-label{quiet}">{caption}</span>'


def _figure_html(m: "re.Match[str]",
                 asset_url: Callable[[str], str | None] | None,
                 asset_size: Callable[[str], tuple[int, int] | None] | None,
                 zoom_links: bool) -> str:
    """Render one `![caption](assets/<file>)` figure; '' drops a missing asset."""
    alt, src, caption = m.group(1), m.group(2), m.group(3)
    url = src
    size = None
    if src.startswith(_ASSETS_PREFIX):
        name = src[len(_ASSETS_PREFIX):]
        if asset_url is not None:
            url = asset_url(name)
            if url is None:
                return ""
        if asset_size is not None:
            size = asset_size(name)
    dims = f' width="{size[0]}" height="{size[1]}"' if size else ""
    img = f'<img src="{url}" alt="{alt}"{dims} loading="lazy" decoding="async">'
    if zoom_links:
        img = f'<a class="frame-zoom" href="{url}" data-zoom>{img}</a>'
    cap = f"<figcaption>{_caption_html(caption)}</figcaption>" if caption else ""
    return f'<figure class="frame">{img}{cap}</figure>'


def _link_html(m: "re.Match[str]",
               asset_url: Callable[[str], str | None] | None) -> str:
    """Render one inline link, resolving asset links to their served URLs."""
    bang, href, label = m.group(1), m.group(2), m.group(3)
    if href.startswith(_ASSETS_PREFIX):
        # An inline image the block renderer left as a link: point it at
        # the served asset, or keep just the text when the file is gone.
        url = asset_url(href[len(_ASSETS_PREFIX):]) if asset_url is not None else href
        return f'<a href="{url}">{label}</a>' if url else label
    external = href.startswith(("http://", "https://"))
    attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
    if _TS_LABEL.match(_strip_tags(label)):
        return f'{bang}<a class="ts" href="{href}"{attrs}>{label.replace("-", "–")}</a>'
    return f'{bang}<a href="{href}"{attrs}>{label}</a>'


def _section_html(out: str, used: set[str],
                  numbered_sections: bool) -> tuple[str, list[TocEntry]]:
    """Wrap h2-delimited chunks in <section> elements and build the TOC."""
    toc: list[TocEntry] = []
    chunks = re.split(r"(?=<h2>)", out)
    rendered = []
    for chunk in chunks:
        m = re.match(r"<h2>(.*?)</h2>", chunk, re.S)
        if not m:
            if chunk.strip():
                rendered.append(chunk)
            continue
        inner = m.group(1)
        text = _strip_tags(inner)
        anchor = slugify(text, used)
        toc.append(TocEntry(anchor=anchor, text=text))
        num = (f'<span class="sec-num" aria-hidden="true">{len(toc)}</span>'
               if numbered_sections else "")
        head = (f'<h2 id="{anchor}">{num}<span class="sec-title">{inner}</span>'
                f'<a class="anchor" href="#{anchor}" aria-label="Link to this section">#</a></h2>')
        body = chunk[m.end():]
        body = re.sub(
            r"<h3>(.*?)</h3>",
            lambda h: f'<h3 id="{slugify(anchor + "-" + _strip_tags(h.group(1)), used)}">'
                      f"{h.group(1)}</h3>",
            body)
        rendered.append(f'<section class="sec" aria-labelledby="{anchor}">{head}{body}</section>')
    return "\n".join(rendered), toc


def enhance(body_html: str, *,
            asset_url: Callable[[str], str | None] | None = None,
            asset_size: Callable[[str], tuple[int, int] | None] | None = None,
            zoom_links: bool = True,
            numbered_sections: bool = False) -> tuple[str, list[TocEntry]]:
    """Post-process `markdown_to_html` output for reading.

    `asset_url(filename)` maps an `assets/<file>` reference to a served URL,
    returning None when the file is missing (the figure is then dropped).
    """
    out = _FIGURE.sub(lambda m: _figure_html(m, asset_url, asset_size, zoom_links),
                      body_html)
    out = _LINK.sub(lambda m: _link_html(m, asset_url), out)
    out = _BARE_TS.sub(lambda m: f'<span class="ts">{m.group(1).replace("-", "–")}</span>', out)

    def callout(m: "re.Match[str]") -> str:
        kind = "warn" if m.group(1).lower() in ("warning", "caution", "important") else "tip"
        return f'<blockquote class="callout {kind}"><p><strong>{m.group(1)}</strong>'

    out = _CALLOUT.sub(callout, out)
    return _section_html(out, set(), numbered_sections)
