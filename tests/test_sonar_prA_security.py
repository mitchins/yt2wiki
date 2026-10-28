"""PR A — security review evidence and accessibility regression tests.

Covers the Sonar security-review findings with executable evidence rather
than prose alone: the file_hash trust boundary (pythonsecurity:S8707,
reviewed false positive), the judge's deterministic presentation shuffle
(python:S2245 hotspot), the three auto-escape bypasses (Web:S5247) tested
with injection payloads against the real render pipeline, and the
autofocus removal (Web:S9379). No models, no network.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.test_v9z1 import StubVLM, _cand, _rubric_json, _sec  # noqa: E402
from yt2wiki import reader  # noqa: E402
from yt2wiki.article import markdown_to_html  # noqa: E402
from yt2wiki.paths import file_hash  # noqa: E402
from yt2wiki.v9 import judge as J  # noqa: E402
from yt2wiki.web.views import _snippet_html  # noqa: E402


# ----------------------------------------------------------------------
# file_hash trust boundary (S8707 false positive)
def test_file_hash_returns_digest_not_content(tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"fake-video-bytes" * 100)
    digest = file_hash(target)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    assert b"fake-video" not in digest.encode()
    # Read-only: the file is untouched.
    assert target.read_bytes() == b"fake-video-bytes" * 100


def test_file_hash_is_general_purpose_not_root_confined(tmp_path):
    """Deliberately not confined to the data root: confinement lives with
    the upload-staging callers, not the hashing utility."""
    outside = tmp_path / "elsewhere" / "x.bin"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"data")
    assert file_hash(outside) == hashlib.sha256(b"data").hexdigest()[:16]


def test_file_hash_follows_symlink_without_disclosing(tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"secret-bytes")
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    assert file_hash(link) == file_hash(target)
    assert "secret" not in file_hash(link)


def test_file_hash_rejects_non_regular_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        file_hash(tmp_path)  # a directory
    with pytest.raises(FileNotFoundError):
        file_hash(tmp_path / "missing.bin")


def test_upload_path_confines_traversal_filename(tmp_path):
    from yt2wiki.paths import Paths

    paths = Paths(root=tmp_path)
    stored = paths.upload_path("up-1", "../../etc/passwd")
    assert stored.parent == paths.uploads_dir
    assert ".." not in stored.name
    stored2 = paths.upload_path("up-1", "/abs/path/evil.mp4")
    assert stored2.parent == paths.uploads_dir


# ----------------------------------------------------------------------
# judge presentation shuffle (S2245 hotspot)
def _adjudicate(tmp_path, seed):
    from PIL import Image

    frames = []
    for i in range(4):
        p = tmp_path / f"s{seed}-f{i}.png"
        Image.new("RGB", (16, 16), (i * 40,) * 3).save(p)
        frames.append(_cand(f"f{i}", str(p), float(i)))
    entries = [(i + 1, 5, 5, 5, 5) for i in range(4)]
    be = StubVLM(rubric=_rubric_json(entries))
    return J.adjudicate_section_z1(be, _sec(), frames[0], frames[1:], frames, seed=seed)


def test_presentation_shuffle_is_deterministic(tmp_path):
    """Same seed -> identical presentation order: reproducibility for the
    operator-configured settings.deterministic_seed (default 1234)."""
    first = _adjudicate(tmp_path, 3)["rounds"][0]["shown"]
    second = _adjudicate(tmp_path, 3)["rounds"][0]["shown"]
    assert first == second
    assert sorted(first) == [f"f{i}" for i in range(4)]


def test_presentation_shuffle_only_reorders(tmp_path):
    """The shuffle changes candidate order, never membership or scores."""
    rec = _adjudicate(tmp_path, 3)
    shown = rec["rounds"][0]["shown"]
    assert sorted(shown) == [f"f{i}" for i in range(4)]
    assert rec["decision"] in ("KEEP", "REPLACE", "USE_PAIR", "OMIT", "EXPAND")


# ----------------------------------------------------------------------
# auto-escape bypasses (S5247 hotspots): injection payloads
PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "![x](javascript:alert(1))",
    "[a](https://e.com\" onmouseover=\"alert(1))",
    "[v](data:text/html,<script>alert(1)</script>)",
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_pipeline_neutralises_injection(payload):
    body_md = f"# Title\n\nSome text {payload} here.\n"
    body_html = markdown_to_html(body_md)
    out, _toc = reader.enhance(body_html)
    # No executable markup: dangerous constructs may survive only as
    # inert escaped text (e.g. "&lt;img ... onerror=..." has no real tag).
    assert "<script" not in out
    assert "<img" not in out
    assert "javascript:" not in out
    assert "onerror=\"" not in out
    assert "onmouseover=\"" not in out
    assert "data:text/html" not in out


def test_pipeline_preserves_legitimate_formatting():
    md = "# T\n\nSome **bold** and `code` and [ok](https://example.com/x).\n"
    out, _toc = reader.enhance(markdown_to_html(md))
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out
    assert '<a href="https://example.com/x"' in out


def test_snippet_html_escapes_then_marks():
    snip = "watch [[out]] for <script>alert(1)</script> and ![i](javascript:x)"
    out = _snippet_html(snip)
    assert "<script" not in out
    assert "javascript:" not in out
    assert "<mark>out</mark>" in out


def test_snippet_plain_text_escapes():
    # FTS-snippet pre-cleaning strips markdown residue ("[#*>`]+") before
    # escaping; executable markup must still be inert.
    out = _snippet_html("plain <b>bold</b> text")
    assert "<b>" not in out
    assert "</b>" not in out
    assert "&lt;" in out


# ----------------------------------------------------------------------
# autofocus removal (S9379)
def test_add_template_has_no_autofocus():
    from pathlib import Path

    tpl = (Path(__file__).resolve().parent.parent
           / "src" / "yt2wiki" / "web" / "templates" / "add.html").read_text()
    assert "autofocus" not in tpl
    # The field keeps its accessible label and input semantics.
    assert "YouTube link" in tpl
    assert 'name="url"' in tpl
