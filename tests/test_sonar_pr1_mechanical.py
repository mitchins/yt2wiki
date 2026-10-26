"""PR1 (Sonar HIGH mechanical cleanup) regression tests.

Covers the behaviour-bearing parts of the mechanical cleanup: the
deduplicated asset-reference regex, the timezone-aware provenance timestamp,
the reader asset-prefix mapping, the v9 markdown-link regex swap, the
StageContext dataclass annotation, and the accessible-name/visible-label
template fixes. No models, no network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import login_client  # noqa: E402,F401
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.pipeline import StageContext  # noqa: E402
from yt2wiki.pipeline.stages import compose  # noqa: E402
from yt2wiki.reader import enhance  # noqa: E402
from yt2wiki.v9 import publish as v9pub  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402

MD = """# Install the thing

> Compiled from the video **Thing** by Someone

## First part

Run `make` now.

![Source frame](assets/f-1.jpg)

*Source frame*
"""


@pytest.fixture()
def app(tmp_path):
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    return settings, db, create_app(settings)


def _seed_article(settings, db, *, article_id="art9-yt-abc", source_id="yt-abc"):
    from PIL import Image

    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources(source_id, kind, origin_url, title, uploader, "
            "duration_s, created_at) VALUES(?,?,?,?,?,?,?)",
            (source_id, "youtube", "https://youtu.be/abc", "Thing", "Someone", 300.0, now))
        conn.execute("INSERT OR IGNORE INTO jobs(job_id, source_id, profile, fingerprint, state,"
                     " config_json, created_at, updated_at) VALUES('job-w',?, 'compact','fp',"
                     "'done','{}',?,?)", (source_id, now, now))
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version,"
            " status, outline_json, created_at, updated_at) VALUES(?,?,?,?,?,?,1,'published',?,?,?)",
            (article_id, source_id, "job-w", "Install the thing", MD, "<p>x</p>",
             json.dumps([{"section_id": "sec-01", "heading": "First part", "t_start": 0,
                          "t_end": 60, "frames": ["f-1"]}]), now, now))
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
                     (article_id, "Install the thing", MD))
        conn.execute("INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption,"
                     " alt_text, created_at) VALUES('asset-f-1',?, 'f-1', '', '', '', ?)",
                     (article_id, now))
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), "green").save(assets / "f-1.jpg")


# ---------------------------------------------------------------------
# S1192: deduplicated literals stay behaviour-identical

def test_compose_asset_ref_regex_extracts_frame_names():
    md = "![a](assets/f-1.jpg)\n[ext](https://x/y.png)\n![b](assets/g-2.webp)"
    assert compose.article_frames(md) == ["f-1.jpg", "g-2.webp"]
    assert compose.article_frames("no refs") == []


def test_reader_assets_prefix_maps_and_drops_missing():
    from yt2wiki.article import markdown_to_html

    html = markdown_to_html("![a](assets/f-1.jpg)")

    served, _ = enhance(html, asset_url=lambda n: f"/assets/art/{n}")
    assert '/assets/art/f-1.jpg' in served

    dropped, _ = enhance(html, asset_url=lambda n: None)
    assert "<img" not in dropped

    untouched, _ = enhance('<a href="https://x/y">y</a>')
    assert 'href="https://x/y"' in untouched


def test_v9_markdown_link_regex_still_drops_far_links():
    body = "step [0:30](https://youtu.be/abc?t=30) and [14:00](https://youtu.be/abc?t=840)"
    out = v9pub.drop_far_links(body, "https://youtu.be/abc", t_start=0, t_end=60, tol=2)
    assert "0:30" in out          # inside the section window: kept
    assert "14:00" not in out     # far outside: dropped entirely
    plain = "[label](https://example.com/x)"
    assert v9pub.drop_far_links(plain, None, 0, 60) == plain


# ---------------------------------------------------------------------
# S6903: timezone-aware UTC construction, identical serialization

def test_provenance_section_renders_utc_zulu_timestamp():
    from yt2wiki.article import Article

    ts = 1761364345.939061
    article = Article(source_id="yt-abc", title="Thing", summary="",
                      provenance={"retrieved_at": ts})
    section = compose._provenance_section(None, article)
    line = next(b for b in section.body if b.startswith("- **Source retrieved:**"))
    assert line.endswith("2025-10-25T03:52:25.939061Z")
    assert "+00:00" not in line
    assert "utcfromtimestamp" not in Path(compose.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# S8514: dataclass attribute annotation

def test_stage_context_current_stage_is_annotated_field():
    field = StageContext.__dataclass_fields__["current_stage"]
    assert field.default == ""
    assert "str" in str(field.type)


# ---------------------------------------------------------------------
# Web:S7927: accessible names reflected in visible labels

def _no_stale_aria_labels(html: str, stale: list[str]) -> None:
    for label in stale:
        assert f'aria-label="{label}"' not in html


def test_base_nav_settings_label_is_visible(app):
    _settings, _db, application = app
    html = TestClient(application).get("/").text
    assert ">Settings</span>" in html
    assert 'href="/settings"' in html
    _no_stale_aria_labels(html, ["Settings"])


def test_article_lightbox_controls_have_visible_labels(app):
    settings, db, application = app
    _seed_article(settings, db)
    html = TestClient(application).get("/articles/art9-yt-abc").text
    for visible in ("Close", "Previous", "Next"):
        assert f">{visible}</span>" in html
    _no_stale_aria_labels(html, ["Close", "Previous image", "Next image"])


def test_editor_controls_have_visible_labels(app, operator_password):
    settings, db, application = app
    _seed_article(settings, db)
    client, _csrf = login_client(application)
    html = client.get("/articles/art9-yt-abc/edit").text
    assert ">Back</span>" in html
    assert ">Remove</span>" in html
    _no_stale_aria_labels(html, ["Back to the guide", "Remove this image"])
