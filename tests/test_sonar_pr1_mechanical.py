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
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import login_client, post_json  # noqa: E402,F401
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.pipeline import StageContext  # noqa: E402
from yt2wiki.pipeline.stages import compose  # noqa: E402
from yt2wiki.reader import enhance  # noqa: E402
from yt2wiki.v9 import publish as v9pub  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402

ARTICLE_ID = "art9-yt-abc"
SOURCE_ID = "yt-abc"
JOB_ID = "job-1"
MISSING = "missing"
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


def _seed_article(settings, db, *, article_id=ARTICLE_ID, source_id=SOURCE_ID):
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


# ---------------------------------------------------------------------
# Error-path coverage for routes whose literal-to-constant swap touched
# previously untested branches (404/400 handlers).

def _rule_evidence():
    from yt2wiki.records import Evidence

    return [
        Evidence(evidence_id="ev-aaa111", kind="action",
                 text="Insert the USB drive into the Pi and boot it up",
                 t_start=214.0, t_end=219.0),
        Evidence(evidence_id="ev-bbb222", kind="config",
                 text="Set arm frequency to 3000 megahertz",
                 t_start=100.0, t_end=105.0),
    ]


def test_rule_unsupported_flags_broken_and_mismatched_citations():
    from yt2wiki.pipeline.stages.verify import _rule_unsupported

    ev = _rule_evidence()
    md_broken = ("## Procedure\n\n1. **Boot.**\n\n"
                 "   Insert the USB drive into the Pi and boot it up."
                 " <!-- cite: step=1 ev=ev-zzz999 t=214-219 frames=- -->\n")
    out = _rule_unsupported(None, md_broken, ev)
    broken = [f for f in out if f["code"] == "broken_citation"]
    assert broken and broken[0]["direction"] == "article->evidence"

    md_mismatch = ("## Procedure\n\n1. **Boot.**\n\n"
                   "   Run the installer from the USB drive and reboot."
                   " <!-- cite: step=1 ev=ev-bbb222 t=100-105 frames=- -->\n")
    out2 = _rule_unsupported(None, md_mismatch, ev)
    mismatched = [f for f in out2 if f["code"] == "mismatched_citation"]
    assert mismatched and mismatched[0]["detail"]["supported_by"] == "ev-aaa111"
    assert mismatched[0]["detail"]["cited"] == ["ev-bbb222"]


def test_classic_publish_stage_writes_standalone_bundle_and_zip(tmp_path):
    from types import SimpleNamespace

    from tests.test_generations import _seed_article, _seed_source_job
    from yt2wiki.pipeline.stages import publish as publish_stage

    settings = Settings.load(data_dir=tmp_path / "data", profile="compact",
                             device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    _seed_source_job(db)
    _seed_article(db, article_id=ARTICLE_ID, source_id=SOURCE_ID)
    db.execute("UPDATE articles SET status='draft' WHERE article_id=?", (ARTICLE_ID,))
    assets = settings.paths.article_assets(ARTICLE_ID)
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "f-1.jpg").write_bytes(b"\xff\xd8fakejpg")
    settings.paths.article_markdown(ARTICLE_ID).write_text("# T\n\nhello\n")
    ctx = SimpleNamespace(job_id="job-1", source_id=SOURCE_ID, settings=settings,
                          db=db, paths=settings.paths, fingerprint="fp",
                          artifacts={"article_id": ARTICLE_ID}, meta=lambda: {},
                          inference_snapshot=None)
    out = publish_stage.run(ctx)
    assert ctx.artifacts["published"] is True
    adir = settings.paths.article_dir(ARTICLE_ID)
    standalone = adir / "article.standalone.html"
    assert standalone.is_file()
    assert out["standalone_html_bytes"] == standalone.stat().st_size
    bundle_files = sorted(p.name for p in (adir / "export").rglob("*") if p.is_file())
    assert {"article.md", "article.html", "verification.json", "provenance.json"} <= set(bundle_files)
    with zipfile.ZipFile(adir / "article.zip") as zf:
        assert "article.md" in zf.namelist()
    assert db.query_one("SELECT status FROM articles WHERE article_id=?",
                        (ARTICLE_ID,))["status"] == "published"


def test_remote_backend_guards_raise_when_not_loaded():
    from yt2wiki.backends.remote_openai import RemoteOpenAIBackend, _NOT_LOADED_MSG
    from yt2wiki.config import ModelSpec

    spec = ModelSpec(key="k", role="vision", engine="remote-openai", repo="r")
    be = RemoteOpenAIBackend(spec, {}, "cpu", None, base_url="http://x:1/v1")
    assert be.loaded is False
    with pytest.raises(RuntimeError, match=_NOT_LOADED_MSG):
        be.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(RuntimeError, match=_NOT_LOADED_MSG):
        be.rank_images([], "pick one")


def test_web_error_paths_after_constant_swap(app, operator_password):
    settings, db, application = app
    _seed_article(settings, db)
    anon = TestClient(application)
    client, csrf = login_client(application)

    # anonymous 404s / 400s
    assert anon.get(f"/jobs/{MISSING}").status_code == 404
    assert anon.get(f"/api/jobs/{MISSING}").status_code == 404
    assert anon.get("/assets/art9-yt-abc/%2E%2E").status_code == 400
    assert anon.get("/frames/yt-abc/%2E%2E.jpg").status_code == 400
    assert anon.get("/media/yt-abc/%2E%2E").status_code == 400
    assert anon.get(f"/api/articles/{MISSING}/export.md").status_code == 404
    assert anon.get(f"/api/v9/articles/{MISSING}/sections").status_code == 404
    assert anon.get(f"/api/v9/articles/{MISSING}/alternatives").status_code == 404
    assert anon.post("/api/preview",
                     json={"article_id": MISSING, "markdown": "text"}
                     ).status_code == 404

    # operator-gated error paths
    assert client.get(f"/articles/{MISSING}/edit").status_code == 404
    assert post_json(client, csrf, f"/api/jobs/{MISSING}/reprocess", {}).status_code == 404
    assert post_json(client, csrf, f"/api/articles/{MISSING}", {"markdown": "x" * 20}
                     ).status_code == 404
    assert post_json(client, csrf, f"/api/articles/{MISSING}/images/move",
                     {"frame_id": "f-1", "section": "First part"}).status_code == 404
    assert post_json(client, csrf, f"/api/articles/{MISSING}/images/caption",
                     {"frame_id": "f-1", "caption": "c"}).status_code == 404
    assert post_json(client, csrf, f"/api/articles/{MISSING}/images/replace",
                     {"old_frame_id": "f-1", "new_frame_id": "f-2"}
                     ).status_code == 404

    # form flows render/redirect with the shared message strings
    r = client.post(f"/jobs/{MISSING}/retry", data={"csrf_token": csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=job%20not%20found" in r.headers["location"]
    r = client.post("/add/upload", data={"csrf_token": csrf})
    assert r.status_code == 400 and "Choose a video file" in r.text
