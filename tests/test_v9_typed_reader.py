"""Typed reader contract: migration, manifest identity, badge display.

Deterministic only. Old articles keep their labels; typed identity is
additive; the reader never fabricates a type.
"""

from __future__ import annotations

import sqlite3

from yt2wiki.article import markdown_to_html
from yt2wiki.db.schema import Database
from yt2wiki.generations import prepare_generation
from yt2wiki.paths import Paths
from yt2wiki.v9 import blocks as B
from yt2wiki.web import views


def test_article_type_column_added_not_backfilled(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    cols = {r["name"] for r in db.query("PRAGMA table_info(articles)")}
    assert "article_type" in cols
    db.execute(
        "INSERT INTO sources(source_id, kind, title, created_at)"
        " VALUES('s','upload','S',1.0)")
    db.execute(
        "INSERT INTO articles(article_id, source_id, title, markdown, version,"
        " status, created_at, updated_at) VALUES('a','s','T','# T',1,'published',1.0,2.0)")
    assert db.query_one("SELECT article_type FROM articles")["article_type"] is None
    db.migrate()  # idempotent
    assert db.query_one("SELECT article_type FROM articles")["article_type"] is None


def test_manifest_carries_typed_identity_only_when_given(tmp_path):
    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    adir = paths.article_dir("art9-x")
    (adir / "assets").mkdir(parents=True, exist_ok=True)
    (adir / "article.md").write_text("# X\n", encoding="utf-8")
    plain = prepare_generation(
        db, paths, article_id="art9-x", source_id="s", job_id=None,
        recipe="v9", recipe_version=2, source_dir=adir)
    assert "article_type" not in plain["manifest"]
    assert "editorial" not in plain["manifest"]
    typed = prepare_generation(
        db, paths, article_id="art9-x", source_id="s", job_id=None,
        recipe="v9", recipe_version=2, source_dir=adir,
        article_type="discussion", template_version=1,
        editorial={"selection": {"template_id": "discussion"}})
    assert typed["manifest"]["article_type"] == "discussion"
    assert typed["manifest"]["template_version"] == 1
    assert typed["manifest"]["editorial"]["selection"]["template_id"] == "discussion"


def test_badge_prefers_typed_id_then_legacy_then_neutral():
    assert views.article_type_display("discussion", "explainer") == ("discussion", "Discussion")
    assert views.article_type_display(None, "procedure") == ("procedure", "How-to")
    assert views.article_type_display(None, "experimental") == ("experiment", "Experiment")
    # comparison/teardown keep their historical labels, not a mapped type.
    assert views.article_type_display(None, "comparison") == (None, "Comparison")
    assert views.article_type_display(None, "teardown") == (None, "Teardown")
    assert views.article_type_display(None, "") == (None, "Article")
    assert views.article_type_display("invented", "") == (None, "Article")
    assert views.article_type_display(None, None) == (None, "Article")


def test_lead_blocks_pass_through_reader_view(tmp_path):
    from yt2wiki.web.views import render_reader

    paths = Paths(root=tmp_path / "data")
    block = {"block": "key_facts",
             "facts": [{"label": "Impact", "value": "2022-09-26"}]}
    assert B.validate_block(block) == []
    view = render_reader(paths, "art9-x", "# T\n\nBody.\n",
                         lead_blocks=[{"name": "key_facts",
                                       "html": B.render_html(block)}])
    assert view["lead_blocks"][0]["name"] == "key_facts"
    assert "2022-09-26" in view["lead_blocks"][0]["html"]
    plain = render_reader(paths, "art9-x", "# T\n\nBody.\n")
    assert plain["lead_blocks"] == []


def test_v4_stamp_without_type_column_is_healed(tmp_path):
    p = tmp_path / "heal.sqlite3"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE articles(article_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,"
                 " title TEXT NOT NULL, markdown TEXT NOT NULL, html TEXT,"
                 " version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'draft',"
                 " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '4')")
    conn.commit()
    conn.close()
    db = Database(p)
    cols = {r["name"] for r in db.query("PRAGMA table_info(articles)")}
    assert "article_type" in cols


def test_both_byline_wordings_break(tmp_path):
    from yt2wiki.article import _break_byline_note

    old = "Lead text This illustrated guide was compiled locally."
    new = "Lead text This article was compiled locally."
    assert "<br>This illustrated guide was compiled" in _break_byline_note(old)
    assert "<br>This article was compiled" in _break_byline_note(new)
    assert _break_byline_note("No marker here.") == "No marker here."


def test_lead_blocks_from_manifest_only_renders_valid():
    editorial = {
        "lead_blocks": [
            {"block": {"block": "key_facts",
                       "facts": [{"label": "Impact", "value": "2022-09-26"}]}},
            {"block": {"block": "star_rating", "stars": 5}},
            {"block": "not-a-block"},
            "junk",
        ]
    }
    out = views.lead_blocks_from_manifest(editorial)
    assert len(out) == 1
    assert out[0]["name"] == "key_facts"
    assert "2022-09-26" in out[0]["html"]
    assert views.lead_blocks_from_manifest({}) == []
    assert views.lead_blocks_from_manifest(None) == []


def test_new_attribution_quote_renders_broken_byline(tmp_path):
    from yt2wiki.article import Article, render_html

    md = ("> Compiled from the video **T** by U\n"
          ">\n"
          "> This article was compiled locally from the video's narration "
          "and original frames. Timestamps link each section to its source span.\n"
          "\nBody.\n")
    article = Article(source_id="s", title="T", sections=[],
                      provenance={"uploader": "U"}, content_type="v9_illustrated")
    html = render_html(article, markdown_to_html(md))
    assert "<br>This article was compiled" in html


def test_article_context_resolves_typed_badge_and_numbering(tmp_path):
    from yt2wiki.web.views import article_context

    paths = Paths(root=tmp_path / "data")
    db = Database(paths.db_path)
    db.execute("INSERT INTO sources(source_id, kind, title, created_at)"
               " VALUES('s','youtube', 'S', 1.0)")
    for aid, atype in (("art9-a", "procedure"), ("art9-b", None)):
        db.execute(
            "INSERT INTO articles(article_id, source_id, job_id, title, markdown,"
            " version, status, article_type, created_at, updated_at)"
            " VALUES(?,?,?,?,?,1,'published',?,1.0,2.0)",
            (aid, "s", f"job-{aid}", "T", "# T\n\nBody.\n", atype))
    ctx = article_context(db, paths, "art9-a")
    assert ctx["article_type"] == "procedure"
    assert ctx["kind"] == "How-to"
    assert ctx["view"]["toc"] is not None
    legacy = article_context(db, paths, "art9-b")
    assert legacy["article_type"] is None
    assert legacy["kind"] == "Article"
