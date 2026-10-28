"""PR B — regex safety: linear-time replacements, equivalence and perf tests.

Each Sonar S8786 pattern was replaced by a backtracking-free parser.
Frozen copies of the original patterns serve as differential oracles on
small inputs; new implementations additionally face adversarial large
inputs with scaling bounds that a super-linear algorithm cannot meet.
No models, no network.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from yt2wiki import reader as R
from yt2wiki.article import (
    _break_byline_note,
    _bullet_item,
    _image_item,
    _link_byline_source,
    _ordered_item,
    markdown_to_html,
)
from yt2wiki.backends.llm import _strip_fence
from yt2wiki.literals import PORT_RE
from yt2wiki.pipeline.stages import verify as V
from yt2wiki.v9.publish import _strip_inline_timestamp_links
from yt2wiki.web.inference import _extract_ok_object

# Frozen oracles: exact copies of the pre-fix patterns ---------------------
_O_ORDERED = re.compile(r"^(\d+)\.\s+(.*)$")
_O_BULLET = re.compile(r"^[-*]\s+(.*)$")
_O_IMAGE = re.compile(r"^!\[(.*?)\]\((.*?)\)$")
_O_BYLINE_SRC = re.compile(r"\s+Source video: (https?://\S+)")
_O_BYLINE_NOTE = re.compile(r"\s+(This illustrated guide was compiled)")
_O_FENCE = re.compile(r"```(?:json|jsonc|JSON)?\s*(.*?)```", re.DOTALL)
_O_PORT = re.compile(r"(?<![\w.])port\s*[:=]?\s*(\d{2,5})(?!\d)(?!\.\d)", re.IGNORECASE)
_O_STEP = re.compile(r"^(\d+)\.\s+(.*)$", re.MULTILINE)
_O_CODEBLOCK = re.compile(r"```(?:bash|sh|ini|text)?\s*\n(.*?)```", re.DOTALL)
_O_PLACEHOLDER = re.compile(r"\s*[<\[][^<>\[\]]*[>\]]")
_O_LINKREM = re.compile(r"\[.*\]\(.*\)\*?")
_O_SOURCEAT = re.compile(r"^(.*?)\s+—\s+source at\s+(.+)$", re.S)
_O_TRAIL = re.compile(r" +\n")
_O_OKJSON = re.compile(r"\{.*ok.*\}", re.S)

rng = random.Random(1234)


def _oracle_steps(md):
    return [(int(m.group(1)), m.group(2).strip()) for m in _O_STEP.finditer(md)]


def _oracle_blocks(text):
    return [m.group(1) for m in _O_CODEBLOCK.finditer(text)]


def _timed(fn, arg):
    t0 = time.perf_counter()
    out = fn(arg)
    return out, time.perf_counter() - t0


def _assert_linear(fn, hostile, *, floor=10_000, ceiling=40_000, ratio=10.0):
    """A super-linear algorithm is 16x slower at 4x input; linear stays ~4x."""
    _, t_small = _timed(fn, hostile[:floor])
    _, t_big = _timed(fn, hostile[:ceiling])
    assert t_big < 2.0, f"{fn.__name__} too slow: {t_big:.2f}s"
    if t_small > 0.0005:
        assert t_big / t_small < ratio, (
            f"{fn.__name__} scales super-linearly: {t_small*1000:.1f}ms -> {t_big*1000:.1f}ms")


# --- 1. ordered / bullet / image line items -------------------------------
@pytest.mark.parametrize("line", [
    "1. Do the thing", "12.  spaced", "007. Bond", "1.\tx", "nope", "1.",
    "1. ", ". x", "12x. y", "١٢. unicode digits", "1. nbsp text", "9999999999. big",
    "- item", "* item", "-  deep", "-\ttab", "-x", "*", "-", " -- mixed",
    "![alt](http://e.com/x)", "![a](b)", "![a[b](c)", "![a](b(c))",
    "![a](b", "![a](", "not img", "![](x)", "![a](b) ",
])
def test_line_items_match_oracle(line):
    s = line.strip()
    m = _O_ORDERED.match(s)
    assert _ordered_item(s) == (m.group(2) if m else None)
    m = _O_BULLET.match(s)
    assert _bullet_item(s) == (m.group(1) if m else None)
    m = _O_IMAGE.match(s)
    assert _image_item(s) == ((m.group(1), m.group(2)) if m else None)


def test_line_items_fuzz():
    alpha = "abcXYZ019. -*[]()!\t \u00a0\u0661"
    for _ in range(2000):
        s = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 24))).strip()
        if "\n" in s:
            continue
        m = _O_ORDERED.match(s)
        assert _ordered_item(s) == (m.group(2) if m else None)
        m = _O_BULLET.match(s)
        assert _bullet_item(s) == (m.group(1) if m else None)
        m = _O_IMAGE.match(s)
        assert _image_item(s) == ((m.group(1), m.group(2)) if m else None)


# --- 2. byline substitutions ------------------------------------------------
def test_byline_source_matches_oracle():
    cases = [
        "Words Source video: https://youtu.be/abc end",
        "A  Source video: http://e.com/x  B Source video: https://y.org/z",
        "Source video: https://e.com/x",  # no leading ws: untouched by both
        "Words Source video: ftp://e.com/x",
        "Words Source video: ",
        "Words Source video: https://e.com/a,b?c=d",
        "Words\u00a0Source video: https://e.com/x",  # nbsp counts as \s
    ]
    for text in cases:
        want = _O_BYLINE_SRC.sub(r'<br>Source video: <a href="\1">\1</a>', text)
        assert _link_byline_source(text) == want, text


def test_byline_note_matches_oracle():
    cases = [
        "Intro This illustrated guide was compiled for you",
        "Intro  This illustrated guide was compiled",
        "This illustrated guide was compiled",
    ]
    for text in cases:
        assert _break_byline_note(text) == _O_BYLINE_NOTE.sub(r"<br>\1", text)


def test_byline_fuzz():
    bits = ["Source video: ", "https://e.com/x", "http://a.b/c", "word",
            "  ", "\n", "\t", "Source video: Source video: https://e.com/z"]
    for _ in range(1000):
        text = "".join(rng.choice(bits) for _ in range(rng.randint(1, 8)))
        want = _O_BYLINE_SRC.sub(r'<br>Source video: <a href="\1">\1</a>', text)
        assert _link_byline_source(text) == want, repr(text)


# --- 3. article steps -------------------------------------------------------
def test_article_steps_match_oracle_single_line():
    md = "1. First step\n\nSome prose\n\n12. Second step\n"
    assert V._article_steps(md) == _oracle_steps(md) == [(1, "First step"), (12, "Second step")]


def test_article_steps_cross_line_narrowing_is_intentional():
    # The old MULTILINE regex joined a bare "N." line with the next line;
    # article steps are never emitted that way (cf. verify._numbered_of).
    md = "7.\nnext line\n"
    assert _oracle_steps(md) == [(7, "next line")]
    assert V._article_steps(md) == []


def test_article_steps_fuzz():
    alpha = "abc019. \n"
    for _ in range(1000):
        md = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 60)))
        if re.search(r"(?m)^\d+\.\s*?$", md):
            continue  # documented newline-crossing narrowing; covered above
        assert V._article_steps(md) == _oracle_steps(md), repr(md)


# --- 4. code blocks ---------------------------------------------------------
def test_code_blocks_match_oracle():
    texts = [
        "```bash\necho hi\n```",
        "text ```sh\nx=1\n``` tail",
        "```\nplain\n```",
        "```python\nnope\n```",
        "```bash unclosed",
        "```bash\n```",
        "````\nweird\n```",
        "```bash  \nspaced tag\n```",
        "```ini\na=b\n``` and ```text\nt\n```",
    ]
    for text in texts:
        assert list(V._iter_code_blocks(text)) == _oracle_blocks(text), repr(text)


def test_code_blocks_fuzz():
    alpha = "`absh \n"
    for _ in range(1000):
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 80)))
        assert list(V._iter_code_blocks(text)) == _oracle_blocks(text), repr(text)


# --- 5. placeholders ---------------------------------------------------------
def test_placeholders_match_oracle():
    cases = ["run <file> now", "use [name] here", "a<b", "a<b<c>", "a <b> c",
             "<>", "[]", "x <a b> y [c d] z", "a<b>c[d]e"]
    for text in cases:
        want = " ".join(_O_PLACEHOLDER.sub(" ", text).split())
        assert V._strip_placeholders(text) == want, repr(text)


def test_placeholders_fuzz():
    alpha = "ab <>[]"
    for _ in range(2000):
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 30)))
        want = " ".join(_O_PLACEHOLDER.sub(" ", text).split())
        assert V._strip_placeholders(text) == want, repr(text)


def test_placeholders_no_longer_quadratic():
    hostile = " " * 40000 + "<abc"
    _, dt = _timed(V._strip_placeholders, hostile)
    assert dt < 2.0, f"still slow: {dt:.2f}s"
    _assert_linear(V._strip_placeholders, hostile)


# --- 6. link remnants ---------------------------------------------------------
def test_link_remnant_matches_oracle():
    cases = ["[a](b)", "[a](b)*", "[a](b)**", "[a[b](c)", "[]()", "[a]",
             "[a](b", "a](b)", "[a](#x)*", "plain", "[a](b) tail"]
    for s in cases:
        assert V._is_link_remnant(s) == bool(_O_LINKREM.fullmatch(s)), repr(s)


def test_link_remnant_fuzz():
    alpha = "ab[]()*"
    for _ in range(2000):
        s = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 16)))
        assert V._is_link_remnant(s) == bool(_O_LINKREM.fullmatch(s)), repr(s)


# --- 7. port literals ----------------------------------------------------------
def test_port_matches_oracle():
    cases = ["listen on port 8080", "port: 443 and port=22", "port 1",
             "port 123456", "airport 80", "port .80", "PORT 8080",
             "port   :   1234", "port", "port ", "support 80"]
    for text in cases:
        want = [m.group(1) for m in _O_PORT.finditer(text)]
        got = [m.group(1) for m in PORT_RE.finditer(text)]
        assert got == want, repr(text)


def test_port_fuzz_small():
    alpha = "port :=019 \nxy."
    for _ in range(1500):
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 40)))
        want = [(m.group(0), m.group(1)) for m in _O_PORT.finditer(text)]
        got = [(m.group(0), m.group(1)) for m in PORT_RE.finditer(text)]
        assert got == want, repr(text)


def test_port_no_longer_quadratic():
    hostile = "port" + " " * 40000
    _, dt = _timed(PORT_RE.search, hostile)
    assert dt < 2.0, f"still slow: {dt:.2f}s"
    _assert_linear(lambda s: PORT_RE.search(s), hostile)


# --- 8. code fences (llm) -------------------------------------------------------
def test_strip_fence_matches_oracle():
    cases = [
        '```json\n{"a": 1}\n```', "```jsonc\nx\n```", "```JSON\nx\n```",
        '```{"a": 1}```', "no fence", "```unclosed", "``````",
        'pre ```json\n{"a": 1}\n``` post', "```jsonx\ny\n```",
    ]
    for text in cases:
        m = _O_FENCE.search(text)
        want = m.group(1).strip() if m else text.strip()
        assert _strip_fence(text) == want, repr(text)


def test_strip_fence_fuzz():
    alpha = "`jsoncJSON \n{}\"ab"
    for _ in range(1000):
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 40)))
        m = _O_FENCE.search(text)
        want = m.group(1).strip() if m else text.strip()
        assert _strip_fence(text) == want, repr(text)


# --- 9. source-at captions -------------------------------------------------------
def test_source_at_matches_oracle():
    cases = [
        "Caption — source at 01:23", "Source frame — source at 1:02:03",
        "no dash here", "Caption - source at 01:23", "Caption — nosource 01:23",
        "Caption — source at ", "— source at 01:23", "A — B — source at 02:00",
        "Caption — source atlas 99",
    ]
    for text in cases:
        m = _O_SOURCEAT.match(text.strip())
        want = (m.group(1).strip(), m.group(2).strip()) if m else None
        assert R._split_source_at(text) == want, repr(text)


def test_source_at_fuzz():
    alpha = "ab —sourceat019: \n"
    for _ in range(1000):
        text = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 40)))
        m = _O_SOURCEAT.match(text.strip())
        want = (m.group(1).strip(), m.group(2).strip()) if m else None
        assert R._split_source_at(text) == want, repr(text)


def test_is_ws_matches_regex_s():
    for c in range(0x10000):
        assert R._is_ws(chr(c)) == bool(re.match(r"\s", chr(c)))


# --- 10. trailing spaces -----------------------------------------------------------
def test_trailing_spaces_match_oracle():
    bodies = ["a  \nb", "a   \n\nb  \n", "a\t\nb", "a  ", "  \n  \n"]
    for body in bodies:
        parts = body.split("\n")
        got = "\n".join([ln.rstrip(" ") for ln in parts[:-1]] + parts[-1:])
        assert got == _O_TRAIL.sub("\n", body)


def test_strip_inline_timestamp_links_keeps_behavior():
    body = "line with space  \nnext [01:23](assets/f.png) end  \n"
    out = _strip_inline_timestamp_links(body)
    assert "  \n" not in out
    assert out == out.strip()


# --- 11. ok-object extraction -------------------------------------------------------
def test_extract_ok_object_canonical():
    assert _extract_ok_object('{"ok": true}') == {"ok": True}
    assert _extract_ok_object('  {"ok": true, "text": "hi"}  ') == {"ok": True, "text": "hi"}
    assert _extract_ok_object('{"ok": false}') is None
    assert _extract_ok_object("no braces") is None
    assert _extract_ok_object('{"a": 1}') is None


def test_extract_ok_object_prose_wrapped():
    content = 'Thinking... {"ok": true} done.'
    assert _extract_ok_object(content) == {"ok": True}


def test_extract_ok_object_rejects_greedy_span():
    # The old greedy ``\\{.*ok.*\\}`` span was unparseable here; the scanner
    # finds the real object instead (intentional robustness, still correct).
    content = '{"note": 1} prose {"ok": true}'
    assert _extract_ok_object(content) == {"ok": True}


def test_extract_ok_object_no_longer_quadratic():
    hostile = "{" * 20000 + "}" * 20000
    _, dt = _timed(_extract_ok_object, hostile)
    assert dt < 2.0, f"still slow: {dt:.2f}s"
    _assert_linear(_extract_ok_object, hostile)


# --- 12. dead pattern removed --------------------------------------------------------
def test_warning_pattern_removed():
    assert not hasattr(V, "_WARNING_RE")


# --- 13. adversarial scaling across the remaining parsers ------------------------------
@pytest.mark.parametrize("name,make", [
    ("ordered", lambda n: "1" * n + " "),
    ("bullet", lambda n: "-" + " " * n),
    ("image", lambda n: "![" + "a" * n),
    ("byline-src", lambda n: "x" * (n // 2) + " Source video: " + "y" * (n // 2)),
    ("byline-note", lambda n: "Intro " + "z" * n + " This illustrated guide was compiled"),
    ("codeblock", lambda n: "```bash\n" + "x" * n),
    ("linkrem", lambda n: "[" + "a" * n),
    ("sourceat", lambda n: "a" * n),
    ("trail", lambda n: ("x" + " " * 50 + "\n") * (n // 52)),
])
def test_new_parsers_scale_linearly(name, make):
    fns = {
        "ordered": lambda s: _ordered_item(s.strip()),
        "bullet": lambda s: _bullet_item(s.strip()),
        "image": lambda s: _image_item(s.strip()),
        "byline-src": _link_byline_source,
        "byline-note": _break_byline_note,
        "codeblock": lambda s: list(V._iter_code_blocks(s)),
        "linkrem": V._is_link_remnant,
        "sourceat": R._split_source_at,
        "trail": lambda s: "\n".join(ln.rstrip(" ") for ln in s.split("\n")),
    }
    _assert_linear(fns[name], make(40000))


# --- 14. rendering pipeline still exercises the new parsers ------------------------------
def test_rendering_pipeline_uses_new_parsers():
    md = ("# T\n\n1. First step\n2. Second step\n\n- bullet\n\n"
          "![alt](http://e.com/x.png)\n\n*caption line*\n")
    out = markdown_to_html(md)
    assert "<ol" in out
    assert "<li>First step</li>" in out
    assert "<ul" in out
    assert "<li>bullet</li>" in out
    assert '<img alt="alt" src="http://e.com/x.png">' in out
