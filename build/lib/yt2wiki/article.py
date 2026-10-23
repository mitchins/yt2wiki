"""Article model, Markdown rendering and self-contained HTML export."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .literals import Literal

TIMESTAMP_LINK = "[{t}]({url})"


def format_ts(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(max(0, round(float(seconds))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def timestamp_url(base_url: str | None, seconds: float | None) -> str | None:
    if not base_url or seconds is None:
        return None
    if "youtu" in base_url:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}t={int(max(0, round(float(seconds))))}"
    return base_url


@dataclass
class FrameRef:
    frame_id: str
    path: Path
    caption: str = ""
    t: float | None = None
    annotated: bool = False
    crop: str | None = None

    def filename(self) -> str:
        return f"{self.frame_id}.jpg"


@dataclass
class Section:
    heading: str
    level: int = 2
    body: list[str] = field(default_factory=list)   # markdown blocks
    frames: list[FrameRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "level": self.level,
            "body": list(self.body),
            "frames": [
                {
                    "frame_id": f.frame_id,
                    "caption": f.caption,
                    "t": f.t,
                    "annotated": f.annotated,
                    "crop": f.crop,
                }
                for f in self.frames
            ],
        }


@dataclass
class Article:
    source_id: str
    title: str
    summary: str = ""
    scope: str = ""
    sections: list[Section] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    frames: list[FrameRef] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, Any] = field(default_factory=dict)
    content_type: str = "instructional"

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "scope": self.scope,
            "sections": [s.to_dict() for s in self.sections],
            "steps": self.steps,
            "provenance": self.provenance,
            "content_type": self.content_type,
        }


# ----------------------------------------------------------------------
# Markdown


def render_markdown(article: Article, *, asset_prefix: str = "assets") -> str:
    out: list[str] = []
    out.append(f"# {article.title}")
    out.append("")
    prov = article.provenance
    quote: list[str] = []
    if prov.get("attribution_line"):
        quote.append(prov["attribution_line"])
    if prov.get("url"):
        quote.append(f"Source video: {prov['url']}")
    quote.append(
        "This article was compiled offline from the video's own audio, on-screen text and "
        "frames. Each statement carries the timestamp of the source span that supports it."
    )
    out.append("> " + "\n> ".join(quote))
    out.append("")
    if article.summary:
        out.append(article.summary.strip())
        out.append("")

    for section in article.sections:
        out.append(f"{'#' * section.level} {section.heading}")
        out.append("")
        for block in section.body:
            out.append(block.rstrip())
            out.append("")
        for frame in section.frames:
            out.append(_frame_markdown(frame, asset_prefix=asset_prefix, provenance=prov))
    return "\n".join(out).rstrip() + "\n"


def _frame_markdown(frame: FrameRef, *, asset_prefix: str, provenance: dict[str, Any]) -> str:
    alt = html.escape(frame.caption or f"Frame from the source video at {format_ts(frame.t)}")
    src = f"{asset_prefix}/{frame.filename()}"
    caption = frame.caption or "Source frame"
    link = timestamp_url(provenance.get("url"), frame.t)
    if link:
        caption = f"{caption} — source at [{format_ts(frame.t)}]({link})"
    return f"![{alt}]({src})\n\n*{caption}*\n"


# ----------------------------------------------------------------------
# HTML


CSS = """
:root { color-scheme: light dark; --fg:#1b1f24; --muted:#5b6570; --bg:#fbfbfa;
        --card:#ffffff; --line:#e2e5e9; --accent:#0b6bcb; --warn:#9a6700; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6e6e6; --muted:#a0a8b0;
        --bg:#16181c; --card:#1d2026; --line:#2c3138; --accent:#5ea2ff; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 32px 20px 96px; }
header.article-head { border-bottom:2px solid var(--line); padding-bottom:18px; margin-bottom:26px; }
h1 { font-size:2rem; line-height:1.22; margin:0 0 10px; letter-spacing:-.01em; }
h2 { font-size:1.35rem; margin:38px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }
h3 { font-size:1.08rem; margin:24px 0 8px; }
p, li { margin: 8px 0; }
.lede { color:var(--muted); font-size:1.02rem; }
.meta { color:var(--muted); font-size:.85rem; margin-top:10px; }
blockquote { margin:16px 0; padding:10px 16px; border-left:3px solid var(--accent);
             background:var(--card); border-radius:0 6px 6px 0; color:var(--muted); }
blockquote p { margin:2px 0; font-size:.92rem; }
code, pre { font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace; }
code { background:var(--card); border:1px solid var(--line); border-radius:4px;
       padding:.1em .35em; font-size:.88em; }
pre { background:var(--card); border:1px solid var(--line); border-radius:8px;
      padding:12px 14px; overflow:auto; position:relative; }
pre code { background:none; border:0; padding:0; font-size:.86em; }
ol.steps { counter-reset: step; list-style:none; padding:0; margin:0; }
ol.steps > li { counter-increment: step; position:relative; padding:14px 16px 14px 56px;
                border:1px solid var(--line); border-radius:10px; margin:12px 0; background:var(--card); }
ol.steps > li::before { content: counter(step); position:absolute; left:14px; top:14px;
      width:28px; height:28px; border-radius:50%; background:var(--accent); color:#fff;
      display:flex; align-items:center; justify-content:center; font-weight:600; font-size:.9rem; }
.step-detail { color:var(--muted); font-size:.94rem; }
.expected { font-size:.9rem; color:var(--muted); margin-top:6px; }
.expected b { color:var(--fg); }
figure { margin:14px 0; }
figure img { max-width:100%; height:auto; border:1px solid var(--line); border-radius:8px;
             display:block; background:#000; }
figcaption { color:var(--muted); font-size:.82rem; margin-top:6px; }
figcaption a { color:var(--accent); text-decoration:none; }
.warning { border-left:3px solid var(--warn); background:rgba(154,103,0,.08);
           padding:8px 14px; border-radius:0 6px 6px 0; margin:10px 0; font-size:.92rem; }
.alternative { font-size:.9rem; color:var(--muted); margin-top:6px; }
.tag { display:inline-block; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
       padding:2px 7px; border-radius:20px; border:1px solid var(--line); color:var(--muted);
       margin-right:6px; }
footer { margin-top:56px; padding-top:16px; border-top:1px solid var(--line);
         color:var(--muted); font-size:.82rem; }
.kv { display:grid; grid-template-columns: minmax(140px, 220px) 1fr; gap:4px 14px; font-size:.9rem; }
.kv dt { color:var(--muted); }
.kv dd { margin:0; }
ul.plain { padding-left:20px; }
.badge-ok { color:#1a7f37; }
.badge-warn { color:var(--warn); }
"""


def markdown_to_html(md: str) -> str:
    """Small self-contained Markdown subset renderer (no external assets)."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    in_list: str | None = None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            code = html.escape("\n".join(buf))
            out.append(f'<pre data-lang="{html.escape(lang)}"><code>{code}</code></pre>')
            continue
        if stripped.startswith("#"):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            level = len(stripped) - len(stripped.lstrip("#"))
            level = max(1, min(6, level))
            out.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
            i += 1
            continue
        if stripped.startswith("> "):
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = " ".join(x for x in buf if x)
            out.append(f"<blockquote><p>{_inline(body)}</p></blockquote>")
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if m:
            if in_list != "ol":
                if in_list:
                    out.append(f"</{in_list}>")
                out.append('<ol class="steps">')
                in_list = "ol"
            out.append(f"<li>{_inline(m.group(2))}</li>")
            i += 1
            continue
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            if in_list != "ul":
                if in_list:
                    out.append(f"</{in_list}>")
                out.append('<ul class="plain">')
                in_list = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue
        m = re.match(r"^!\[(.*?)\]\((.*?)\)$", stripped)
        if m:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            if _safe_href(m.group(2)):
                out.append(f'<figure><img alt="{html.escape(m.group(1))}" src="{html.escape(m.group(2))}"></figure>')
            i += 1
            continue
        # A lone *emphasised* line is a figure caption; a lone **bold** line
        # is a paragraph (often a sub-heading the writer emphasised).
        m = re.match(r"^\*([^*]+)\*$", stripped)
        if m and in_list is None:
            out.append(f'<figcaption>{_inline(m.group(1))}</figcaption>')
            i += 1
            continue
        if not stripped:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            i += 1
            continue
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1
    if in_list:
        out.append(f"</{in_list}>")
    return "\n".join(out)


_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


_SCHEME = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.-]*):")


def _safe_href(href: str) -> bool:
    """Allow web, mail, anchor and relative links; refuse javascript:, data: etc.

    Article text is model output over untrusted captions, so a link target
    must never be able to execute anything in the reader.
    """
    m = _SCHEME.match(html.unescape(href))
    return m is None or m.group(1).lower() in ("http", "https", "mailto")


def _inline(text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = html.escape(text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _LINK.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>' if _safe_href(m.group(2))
        else m.group(1), text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)


_ARTICLE_CSS = Path(__file__).resolve().parent / "web" / "static" / "article.css"


def _article_css() -> str:
    """The reader's own stylesheet, so exports look like the web reader."""
    try:
        return _ARTICLE_CSS.read_text(encoding="utf-8")
    except OSError:
        return CSS


def render_html(article: Article, body_html: str, *, embed_images: bool = False,
                asset_dir: Path | None = None, prefix: str = "assets") -> str:
    """Self-contained article page: inline CSS, no scripts, local images only."""
    from .reader import enhance

    prov = article.provenance
    title = html.escape(article.title)
    if embed_images and asset_dir is not None:
        body_html = _embed_images(body_html, asset_dir, prefix)
    # The page header carries the title; drop the body's duplicate H1 and
    # present the leading attribution quote as a byline.
    body_html = re.sub(r"^\s*<h1>.*?</h1>\s*", "", body_html, count=1, flags=re.S)
    byline = ""
    m = re.match(r"^\s*<blockquote><p>(.*?)</p></blockquote>\s*", body_html, flags=re.S)
    if m:
        text = m.group(1)
        # V9 attribution quotes are three lines joined into one paragraph;
        # put the source link and the compilation note on their own lines.
        text = re.sub(r"\s+Source video: (https?://\S+)",
                      r'<br>Source video: <a href="\1">\1</a>', text)
        text = re.sub(r"\s+(This illustrated guide was compiled)", r"<br>\1", text)
        byline = f'<p class="attribution">{text}</p>'
        body_html = body_html[m.end():]
    body_html, toc = enhance(body_html, zoom_links=False)
    meta_bits = []
    if prov.get("uploader"):
        meta_bits.append(f"From <strong>{html.escape(str(prov['uploader']))}</strong>")
    if prov.get("duration_s"):
        meta_bits.append(f"source length {format_ts(prov['duration_s'])}")
    meta = " · ".join(meta_bits)
    contents = ""
    if len(toc) > 1:
        items = "".join(f'<li><a href="#{t.anchor}">{html.escape(t.text)}</a></li>' for t in toc)
        contents = f'<nav class="toc-inline"><span class="label">Contents</span><ol>{items}</ol></nav>'
    source = html.escape(str(prov.get("url") or "a local video file"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_article_css()}</style>
</head>
<body class="standalone">
<main class="sheet article">
<header class="a-head">
<h1 class="a-title">{title}</h1>
{f'<p class="a-meta">{meta}</p>' if meta else ''}
</header>
{byline}
{contents}
{body_html}
<footer class="colophon">
Compiled locally from {source}.
No network requests are required to read this document{'; all images are embedded' if embed_images else ''}.
</footer>
</main>
</body>
</html>
"""


def _embed_images(body_html: str, asset_dir: Path, prefix: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        src = m.group(2)
        if not src.startswith(prefix + "/"):
            return m.group(0)
        path = asset_dir / src[len(prefix) + 1:]
        if not path.is_file():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return m.group(0).replace(src, f"data:image/jpeg;base64,{data}")

    return re.sub(r'<img alt="([^"]*)" src="([^"]+)">', repl, body_html)
