"""Product web app: V9 job routing, recovery, library, reader, editor, settings.

No models, no network, no GPU: the V9 publisher is replaced by a stub where a
job would actually run.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import login_client  # noqa: E402,F401
from yt2wiki.config import Settings  # noqa: E402
from yt2wiki.db.schema import Database  # noqa: E402
from yt2wiki.web import app as webapp  # noqa: E402
from yt2wiki.web.app import create_app  # noqa: E402

MD = """# Install the thing

> Compiled from the video **Thing** by Someone
> Source video: https://youtu.be/abc
> This illustrated guide was compiled locally from the video's narration and original frames.

A short summary of what the video shows.

## First part

Run `make` now [0:12](https://youtu.be/abc?t=12).

![Source frame](assets/f-1.jpg)

*Source frame — source at [0:12](https://youtu.be/abc?t=12)*

## Second part

**Known limitations**

Text with a [bad link](javascript:alert(1)) and an ![x](assets/missing.jpg) inline.

![Source frame](assets/f-gone.jpg)

*Source frame*
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(settings.paths.db_path)
    db.migrate()
    calls = []

    def fake_run_source(settings_, db_, service_, source_id, **kw):
        calls.append((source_id, kw))
        return {"ok": True}

    monkeypatch.setattr("yt2wiki.v9.run.run_source", fake_run_source)
    return settings, db, calls


def _seed(db, settings, *, title="Install the thing", article_id="art9-yt-abc", markdown=MD,
          source_id="yt-abc", legacy=True):
    from PIL import Image

    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources(source_id, kind, origin_url, title, uploader, duration_s, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (source_id, "youtube", "https://youtu.be/abc", "Thing", "Someone", 300.0, now))
        conn.execute("INSERT OR IGNORE INTO jobs(job_id, source_id, profile, fingerprint, state, "
                     "config_json, created_at, updated_at) VALUES('job-w','yt-abc','compact','fp',"
                     "'done','{}',?,?)", (now, now))
        conn.execute("INSERT OR IGNORE INTO stages(job_id, stage, state, metrics_json) VALUES"
                     "('job-w','v9_write','done',?)", (json.dumps({"kind": "procedure", "sections": 2}),))
        outline = json.dumps([{"section_id": "sec-01", "heading": "First part", "t_start": 0,
                               "t_end": 60, "frames": ["f-1"]},
                              {"section_id": "sec-02", "heading": "Second part", "t_start": 60,
                               "t_end": 120, "frames": []}])
        conn.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown, html, version, "
            "status, outline_json, created_at, updated_at) VALUES(?,?,?,?,?,?,1,'published',?,?,?)",
            (article_id, source_id, "job-w", title, markdown, "<p>x</p>", outline, now, now))
        conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES(?,?,?)",
                     (article_id, title, markdown))
        if legacy:
            conn.execute(
                "INSERT INTO articles(article_id, source_id, title, markdown, version, status, "
                "created_at, updated_at) VALUES('art-yt-abc',?,'Legacy title','# Legacy\n\nold',"
                "1,'review',?,?)", (source_id, now - 10, now - 10))
            conn.execute("INSERT INTO articles_fts(article_id, title, markdown) VALUES"
                         "('art-yt-abc','Legacy title','# Legacy old')")
        conn.execute("INSERT INTO article_assets(asset_id, article_id, frame_id, path, caption, "
                     "alt_text, created_at) VALUES('asset-f-1',?, 'f-1', '', '', '', ?)",
                     (article_id, now))
    assets = settings.paths.article_assets(article_id)
    assets.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), "green").save(assets / "f-1.jpg")


# ----------------------------------------------------------------------
# submission and the job lifecycle
def test_url_submission_creates_v9_job_and_dedupes(env, monkeypatch, operator_password):
    from tests.conftest import configure_ready
    settings, db, calls = env
    configure_ready(settings)
    monkeypatch.setattr(webapp.Service, "preview_url", lambda self, url: {"ok": True})
    application = create_app(settings)
    held = []
    monkeypatch.setattr(application.state.runner, "start", lambda: held.append(1))
    client, csrf = login_client(application)
    r = client.post("/add/url", data={"url": "https://youtu.be/abc12345678", "csrf_token": csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/job-")
    job_id = r.headers["location"].rsplit("/", 1)[1]
    cfg = json.loads(db.query_one("SELECT config_json FROM jobs WHERE job_id=?", (job_id,))[0])
    assert cfg["pipeline"] == "v9" and cfg["origin"] == "web"
    # the same source while queued does not create duplicate work
    again = client.post("/api/sources/url", json={"url": "https://youtu.be/abc12345678"},
                        headers={"X-CSRF-Token": csrf}).json()
    assert again["job_id"] == job_id
    assert db.query_one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200 and "Queued" in page.text


def test_url_form_reports_errors_in_page(env, monkeypatch, operator_password):
    settings, _, _ = env
    monkeypatch.setattr(webapp.Service, "preview_url",
                        lambda self, url: {"ok": False, "error": "Video unavailable"})
    client, csrf = login_client(create_app(settings))
    r = client.post("/add/url", data={"url": "https://youtu.be/nope", "csrf_token": csrf})
    assert r.status_code == 400 and "Video unavailable" in r.text
    r = client.post("/add/url", data={"url": "not a link", "csrf_token": csrf})
    assert r.status_code == 400 and "https://" in r.text


def test_worker_runs_web_jobs_through_v9_with_the_job_id(env):
    settings, db, calls = env
    _seed(db, settings)
    application = create_app(settings)
    runner = application.state.runner
    job = application.state.service.create_job(
        "yt-abc", profile="compact", config={"pipeline": "v9", "origin": "web"})
    runner.submit(job)
    for _ in range(100):
        if calls:
            break
        time.sleep(0.05)
    runner.stop()
    assert calls and calls[0][0] == "yt-abc" and calls[0][1]["job_id"] == job


def test_recover_marks_interrupted_and_requeues_web_jobs(env, monkeypatch, operator_password):
    from tests.conftest import configure_ready
    settings, db, _ = env
    configure_ready(settings)
    _seed(db, settings)
    application = create_app(settings)
    service, runner = application.state.service, application.state.runner
    monkeypatch.setattr(runner, "start", lambda: None)  # keep jobs queued for inspection
    web = {"pipeline": "v9", "origin": "web"}
    running = service.create_job("yt-abc", profile="compact", config=web)
    queued = service.create_job("yt-abc", profile="compact", config=web)
    cli = service.create_job("yt-abc", profile="compact")
    db.execute("UPDATE jobs SET state='running' WHERE job_id IN (?,?)", (running, cli))
    db.execute("INSERT INTO stages(job_id, stage, state) VALUES(?, 'v9_write', 'running')", (running,))
    out = runner.recover()
    assert out == {"interrupted": 1, "requeued": 1}
    assert db.query_one("SELECT state FROM jobs WHERE job_id=?", (running,))["state"] == "interrupted"
    assert db.query_one("SELECT state FROM stages WHERE job_id=?", (running,))["state"] == "failed"
    assert db.query_one("SELECT state FROM jobs WHERE job_id=?", (cli,))["state"] == "running"
    assert runner.live() == [queued]
    client = TestClient(application)
    page = client.get(f"/jobs/{running}")
    assert "Interrupted" in page.text and "Retry" in page.text
    # retry re-enqueues the same source; the queued job already covers it
    _cli, _csrf = login_client(application)
    r = _cli.post(f"/jobs/{running}/retry", data={"csrf_token": _csrf}, follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{queued}"
    assert runner.cancel_queued(queued)
    assert db.query_one("SELECT state FROM jobs WHERE job_id=?", (queued,))["state"] == "cancelled"


def test_job_view_reports_v9_stage_progress(env):
    from yt2wiki.web import views

    job = {"job_id": "j", "state": "running", "config_json": '{"pipeline":"v9"}', "stage": "v9_select",
           "started_at": time.time() - 30, "stages": [
               {"stage": "acquisition", "state": "skipped", "metrics_json": '{"reused": true}'},
               {"stage": "transcribe", "state": "skipped", "metrics_json": '{"reused": true}'},
               {"stage": "frame_candidates", "state": "skipped", "metrics_json": '{"reused": true}'},
               {"stage": "v9_write", "state": "done", "duration_s": 12.0,
                "metrics_json": '{"sections": 7, "kind": "experimental"}'},
               {"stage": "v9_select", "state": "running", "metrics_json": "{}"}]}
    v = views.job_view(job, live={"j"})
    keys = [s["key"] for s in v["steps"]]
    assert keys == ["acquisition", "transcribe", "frame_candidates", "v9_write", "v9_discover",
                    "v9_select", "v9_publish"]
    assert v["current_label"] == "Choose the images"
    assert v["steps"][3]["detail"] == "7 sections · Experiment"
    assert v["steps"][0]["detail"] == "Reused from an earlier run"
    assert 0 < v["progress"] < 100
    stale = views.job_view({**job, "state": "queued"}, live=set())
    assert stale["state"] == "stale" and stale["can_retry"]


# ----------------------------------------------------------------------
# library and reader
def test_library_lists_v9_article_once_with_thumbnail(env):
    settings, db, _ = env
    _seed(db, settings)
    client = TestClient(create_app(settings))
    page = client.get("/").text
    assert "Install the thing" in page
    assert "Legacy title" not in page  # superseded by the V9 article for the same source
    assert "/assets/art9-yt-abc/f-1.jpg" in page
    assert "How-to" in page and "A short summary of what the video shows." in page
    assert client.get("/library").status_code == 200


def test_library_empty_state(env):
    settings, _, _ = env
    page = TestClient(create_app(settings)).get("/").text
    assert "Your library is empty" in page


def test_library_search_uses_article_text(env):
    settings, db, _ = env
    _seed(db, settings)
    client = TestClient(create_app(settings))
    page = client.get("/", params={"q": "make"}).text
    assert "Install the thing" in page and "<mark>" in page
    page = client.get("/", params={"q": "zzzz"}).text
    assert "Nothing matches" in page


def test_reader_structure_and_safety(env):
    settings, db, _ = env
    _seed(db, settings, title='Evil <script>alert(1)</script>')
    client = TestClient(create_app(settings))
    page = client.get("/articles/art9-yt-abc").text
    assert "<script>alert(1)</script>" not in page
    assert "Evil &lt;script&gt;" in page
    assert 'href="javascript:' not in page
    assert 'id="first-part"' in page and 'href="#second-part"' in page
    assert 'class="sec-num"' in page  # numbered because the writer said "procedure"
    assert 'class="ts"' in page
    assert "/assets/art9-yt-abc/f-1.jpg" in page and 'width="32" height="18"' in page
    assert "f-gone.jpg" not in page and "missing.jpg" not in page  # never a broken image
    assert "<strong>Known limitations</strong>" in page  # bold line is not a caption
    assert "Other versions from this video" in page and "art-yt-abc" in page


def test_markdown_renderer_refuses_script_links():
    from yt2wiki.article import markdown_to_html

    out = markdown_to_html("[a](javascript:alert(1)) [b](JaVaScRiPt:x) [c](https://ok.example)"
                           "\n\n![i](data:text/html,x)")
    assert "javascript" not in out.lower().replace("[a]", "")
    assert '<a href="https://ok.example">c</a>' in out
    assert "data:text/html" not in out


def test_standalone_export_shares_reader_styles(tmp_path):
    from yt2wiki.article import Article, markdown_to_html, render_html

    html = render_html(Article(source_id="s", title="T"), markdown_to_html(MD))
    assert "<script" not in html.lower()
    assert '<body class="standalone">' in html and ".sec-num" in html
    assert html.count("<h1") == 1
    assert 'class="attribution"' in html and 'class="toc-inline"' in html


# ----------------------------------------------------------------------
# editing
def test_edit_revert_and_title_sync(env, operator_password):
    from tests.conftest import post_json
    settings, db, _ = env
    _seed(db, settings)
    client, csrf = login_client(create_app(settings))
    assert client.get("/articles/art9-yt-abc/edit").status_code == 200
    new_md = MD.replace("# Install the thing", "# Install the thing properly")
    r = post_json(client, csrf, "/api/articles/art9-yt-abc", {"markdown": new_md, "note": "retitled"})
    assert r.json()["version"] == 2
    row = db.query_one("SELECT title, edit_note FROM articles WHERE article_id='art9-yt-abc'")
    assert row["title"] == "Install the thing properly" and row["edit_note"] == "retitled"
    rev = client.get("/api/articles/art9-yt-abc/revisions").json()[0]
    assert client.get(f"/api/articles/art9-yt-abc/revisions/{rev['revision_id']}").json()["markdown"] == MD
    r = post_json(client, csrf, "/api/articles/art9-yt-abc/revert", {"revision_id": rev["revision_id"]})
    assert r.json()["version"] == 3
    row = db.query_one("SELECT title, markdown FROM articles WHERE article_id='art9-yt-abc'")
    assert row["markdown"] == MD and row["title"] == "Install the thing"
    hits = db.query("SELECT article_id FROM articles_fts WHERE articles_fts MATCH 'properly'")
    assert not hits  # search follows the restored text
    assert post_json(client, csrf, "/api/articles/other/revert",
                     {"revision_id": rev["revision_id"]}).status_code == 404


def test_move_image_between_sections(env, operator_password):
    from tests.conftest import post_json
    settings, db, _ = env
    _seed(db, settings)
    client, csrf = login_client(create_app(settings))
    r = post_json(client, csrf, "/api/articles/art9-yt-abc/images/move",
                  {"frame_id": "f-1", "section": "Second part"})
    assert r.status_code == 200, r.text
    md = db.query_one("SELECT markdown FROM articles WHERE article_id='art9-yt-abc'")["markdown"]
    second = md.index("## Second part")
    assert md.index("assets/f-1.jpg") > second
    assert md.index("*Source frame — source at") > second
    assert md.count("assets/f-1.jpg") == 1
    bad = post_json(client, csrf, "/api/articles/art9-yt-abc/images/move",
                    {"frame_id": "f-1", "section": "Nope"})
    assert bad.status_code == 400


def test_preview_renders_unsaved_markdown(env):
    settings, db, _ = env
    _seed(db, settings)
    client = TestClient(create_app(settings))
    d = client.post("/api/preview", json={"article_id": "art9-yt-abc",
                                          "markdown": "# New\n\n## A\n\n1. step one"}).json()
    assert d["title"] == "New" and 'class="steps"' in d["body_html"]


def test_republish_keeps_previous_text_as_revision(env):
    from yt2wiki.v9 import publish as v9publish

    settings, db, _ = env
    _seed(db, settings)
    db.execute("UPDATE articles SET markdown='# Edited by user\n\nmine' WHERE article_id='art9-yt-abc'")
    sections = [{"section_id": "sec-01", "heading": "First part", "body": "b", "t_start": 0, "t_end": 9}]
    v9publish.persist(settings, db, article_id="art9-yt-abc", source_id="yt-abc", job_id="job-2",
                      title="T", markdown="# T\n\n## First part\n\nb\n", sections=sections,
                      picks={}, provenance={}, report={})
    revs = db.query("SELECT markdown, origin FROM article_revisions WHERE article_id='art9-yt-abc'")
    assert any("Edited by user" in r["markdown"] and r["origin"] == "generated" for r in revs)
    assert db.query_one("SELECT job_id FROM articles WHERE article_id='art9-yt-abc'")["job_id"] == "job-2"


# ----------------------------------------------------------------------
# settings
def test_inference_settings_validate_and_never_store_keys(env, operator_password):
    from tests.conftest import post_json
    settings, _, _ = env
    client, csrf = login_client(create_app(settings))
    assert client.get("/settings").status_code == 200
    bad = post_json(client, csrf, "/api/settings/inference", {
        "providers": [{"name": "lan", "url": "http://10.0.0.2:8000/v1", "api_key_env": "sk-secret-123"}]})
    assert bad.status_code == 400 and "YT2WIKI_" in bad.json()["detail"]
    bad2 = post_json(client, csrf, "/api/settings/inference", {
        "providers": [{"name": "lan", "url": "http://10.0.0.2:8000/v1/", "model": "m",
                       "api_key_env": "LAN_KEY"}],
        "roles": {"writer": "lan", "vision": "lan"}})
    assert bad2.status_code == 400 and "YT2WIKI_" in bad2.json()["detail"]
    ok = post_json(client, csrf, "/api/settings/inference", {
        "providers": [{"name": "lan", "url": "http://10.0.0.2:8000/v1/", "model": "m",
                       "api_key_env": "YT2WIKI_LAN_KEY"}],
        "roles": {"writer": "lan", "vision": "lan"}})
    assert ok.status_code == 200
    saved = json.loads((settings.paths.root / "config" / "inference.json").read_text())
    assert saved["providers"]["lan"]["url"] == "http://10.0.0.2:8000/v1"
    assert saved["roles"] == {"writer": "lan", "vision": "lan", "asr": ""}
    unknown = post_json(client, csrf, "/api/settings/inference", {"providers": [], "roles": {"writer": "x"}})
    assert unknown.status_code == 400
    page = client.get("/settings").text
    assert "http://10.0.0.2:8000/v1" in page and "YT2WIKI_LAN_KEY" in page


def test_inference_test_reports_unreachable(env, monkeypatch, operator_password):
    from tests.conftest import post_json
    settings, _, _ = env
    monkeypatch.delenv("YT2WIKI_NO_SUCH_KEY_VAR", raising=False)
    monkeypatch.delenv("NO_SUCH_KEY_VAR", raising=False)
    client, csrf = login_client(create_app(settings))
    r = post_json(client, csrf, "/api/settings/inference/test",
                  {"url": "http://127.0.0.1:9/v1"}).json()
    assert r["ok"] is False and r["error"]
    # Non-prefixed env var is rejected before probing (400)
    r2 = post_json(client, csrf, "/api/settings/inference/test",
                   {"url": "http://127.0.0.1:9/v1", "api_key_env": "NO_SUCH_KEY_VAR"})
    assert r2.status_code == 400 and "YT2WIKI_" in r2.json()["detail"]
    r = post_json(client, csrf, "/api/settings/inference/test",
                  {"url": "http://127.0.0.1:9/v1", "api_key_env": "YT2WIKI_NO_SUCH_KEY_VAR"}).json()
    assert r["ok"] is False and "YT2WIKI_NO_SUCH_KEY_VAR" in r["error"]


def test_schema_newer_than_build_is_refused(tmp_path):
    """A database stamped by a newer unsupported build must not be written."""
    import pytest

    from yt2wiki.db import schema

    db = Database(tmp_path / "x.sqlite3")
    db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(schema.SCHEMA_VERSION + 1),))
    with pytest.raises(RuntimeError, match="newer than this build supports"):
        Database(tmp_path / "x.sqlite3")
    # The marker is untouched: the old build neither lowered nor rewrote it.
    v = db.query_one("SELECT value FROM meta WHERE key='schema_version'")["value"]
    assert int(v) == schema.SCHEMA_VERSION + 1


def test_frames_served_from_recorded_path_only(env, tmp_path):
    from PIL import Image

    settings, db, _ = env
    other = tmp_path / "elsewhere" / "frames" / "yt-abc"
    other.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(other / "f-9.jpg")
    Image.new("RGB", (8, 8)).save(other / "f-10.jpg")
    _seed(db, settings)
    db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) "
               "VALUES('f-9','yt-abc',3.0,?,'scene',0)", (str(other / "f-9.jpg"),))
    client = TestClient(create_app(settings))
    assert client.get("/frames/yt-abc/f-9.jpg").status_code == 200
    assert client.get("/frames/yt-abc/f-10.jpg").status_code == 404  # no row: never guessed
    assert client.get("/frames/other/f-9.jpg").status_code == 404    # row is for another source


def test_recover_leaves_jobs_of_another_live_instance(env, monkeypatch):
    import os
    import socket

    settings, db, _ = env
    _seed(db, settings)
    application = create_app(settings)
    service, runner = application.state.service, application.state.runner
    monkeypatch.setattr(runner, "start", lambda: None)
    other = f"{socket.gethostname()}:{os.getppid()}"  # a process that is certainly alive
    gone = f"{socket.gethostname()}:999999999"
    live_job = service.create_job("yt-abc", profile="compact",
                                  config={"pipeline": "v9", "origin": "web", "worker": other})
    dead_job = service.create_job("yt-abc", profile="compact",
                                  config={"pipeline": "v9", "origin": "web", "worker": gone})
    db.execute("UPDATE jobs SET state='running' WHERE job_id IN (?,?)", (live_job, dead_job))
    assert runner.recover() == {"interrupted": 1, "requeued": 0}
    assert db.query_one("SELECT state FROM jobs WHERE job_id=?", (live_job,))["state"] == "running"
    assert db.query_one("SELECT state FROM jobs WHERE job_id=?", (dead_job,))["state"] == "interrupted"
