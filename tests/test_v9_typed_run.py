"""Typed path end-to-end (gated): scout -> decision -> plan -> write -> publish.

Deterministic only: scripted stub text backend dispatched on prompt
content (order-independent), real V9 select fallbacks (rank stubbed
empty), real publish into a tmp home. No models, no network, no GPU.
"""

from __future__ import annotations

from contextlib import contextmanager

from yt2wiki.config import Settings
from yt2wiki.db.schema import Database
from yt2wiki.services import Service
from yt2wiki.v9 import run as v9run


def _segs(n=12, step=10.0):
    return [(i, i * step, (i + 1) * step,
             f"Segment {i} reviews the Deck OLED display and battery life.")
            for i in range(n)]


def _section_reply(section_id="sec-x", heading="H"):
    return {"heading": heading,
            "body": "The Deck OLED display is bright. [0:01](https://example.com)",
            "key_points": ["bright display"], "warnings": []}


class _StubText:
    def __init__(self, plan_sections):
        import json as _json

        self.plan_sections = plan_sections
        self.calls = []
        self._json = _json

    def chat(self, messages, **kw):
        return self._json.dumps(self.chat_json(messages, **kw))

    def chat_json(self, messages, **kw):
        user = messages[-1]["content"] if messages else ""
        self.calls.append(user[:60])
        if "Summarise each transcript window" in user:
            import re
            ids = re.findall(r"\[(w-\d+)", user)
            return {"digests": [
                {"window_id": wid, "topics": ["display", "battery"],
                 "claims": ["the display is bright"], "names": ["Deck OLED"],
                 "measures": ["7.4 inches"], "flags": {}} for wid in ids]}
        if "Choose the article type" in user:
            return {"template_id": "review",
                    "rationale": "evaluation of a product across the full span",
                    "supporting_window_ids": ["w-000", "w-005", "w-010"],
                    "alternatives_rejected": [{"type": "procedure",
                                               "reason": "no executable task"}],
                    "excluded_window_ids": [], "status": "selected"}
        if "Plan the article" in user:
            return {"template_id": "review", "sections": self.plan_sections,
                    "coverage": {"included_window_ids": ["w-000"],
                                 "excluded_window_ids": [],
                                 "unassigned_substantive_window_ids": []}}
        if "titling a review article" in user:
            return {"title": "Deck OLED Review",
                    "summary": "What changed and how it performs."}
        if "lead component" in user:
            return {"block": "verdict", "statement": "The screen is the upgrade.",
                    "attribution": "the reviewer"}
        for sec in self.plan_sections:
            if sec["heading"] in user:
                return _section_reply(sec["section_id"], sec["heading"])
        return _section_reply()

    def rank_images(self, *a, **k):
        return ""

    def describe_batch(self, *a, **k):
        return []


class _StubManager:
    def __init__(self, text):
        self.text = text

    @contextmanager
    def acquire(self, role):
        yield self.text


def _plan_sections():
    return [
        {"section_id": "sec-subject", "slot": "subject",
         "heading": "What it is", "window_ids": ["w-000", "w-001"]},
        {"section_id": "sec-eval", "slot": "assessment",
         "heading": "How it performs", "window_ids": ["w-005", "w-010"]},
    ]


def _setup_db(tmp_path):
    settings = Settings.load(data_dir=tmp_path, profile="compact",
                             device_index=None)
    db = Database(settings.paths.db_path)
    db.execute("INSERT INTO sources(source_id, kind, title, uploader, origin_url,"
               " duration_s, created_at) VALUES('s','youtube','Deck OLED Review',"
               "'Unexpected', 'https://www.youtube.com/watch?v=test', 120.0, 1.0)")
    for i, start, end, text in _segs():
        db.execute("INSERT INTO transcript_segments(source_id, idx, start, end, text)"
                   " VALUES('s',?,?,?,?)", (i, start, end, text))
    from PIL import Image
    for n, t in enumerate([5.0, 25.0, 55.0, 85.0, 105.0, 115.0]):
        p = tmp_path / f"f-{n}.jpg"
        Image.new("RGB", (16, 16), (n * 30, 0, 0)).save(p)
        db.execute("INSERT INTO frames(frame_id, source_id, t, path, kind,"
                   " created_at) VALUES(?,?,?,?,?,1.0)",
                   (f"f-{n}", "s", t, str(p), "scene"))
    return settings, db


def test_typed_run_publishes_review_with_lead_and_manifest(monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)
    text = _StubText(_plan_sections())
    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(text))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert res["ok"], res.get("error")
    assert res["article_id"] == "art9-s"
    assert res["typed"]["decision"]["template_id"] == "review"
    row = db.query_one("SELECT article_type, markdown FROM articles "
                       "WHERE article_id='art9-s'")
    assert row["article_type"] == "review"
    assert "The screen is the upgrade" in row["markdown"]
    import json
    with open(tmp_path / "articles" / "art9-s"
              / res["publish"]["manifest"]) as fh:
        man = json.load(fh)
    assert man["article_type"] == "review"
    assert man["template_version"] == 1
    assert man["editorial"]["selection"]["template_id"] == "review"
    assert man["editorial"]["lead_blocks"][0]["block"]["block"] == "verdict"


def test_typed_sections_carry_window_unions(monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)
    text = _StubText(_plan_sections())
    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(text))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert res["ok"], res.get("error")
    import json
    with open(tmp_path / "articles" / "art9-s" / "v9_report.json") as fh:
        report = json.load(fh)
    assert report["typed"]["article_type"] == "review"
    assert res["typed"]["index"]["n_windows"] >= 2
    assert len(res["selection"]) == 2


def test_music_only_source_refuses_without_publishing(monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)

    class _MusicText(_StubText):
        def chat_json(self, messages, **kw):
            user = messages[-1]["content"] if messages else ""
            if "Summarise each transcript window" in user:
                import re
                ids = re.findall(r"\[(w-\d+)", user)
                return {"digests": [{"window_id": wid, "topics": [],
                                     "claims": [], "names": [], "measures": [],
                                     "flags": {"music_only": True}}
                                    for wid in ids]}
            raise AssertionError("decision must refuse without model calls")

    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(_MusicText([])))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert not res["ok"]
    assert res["error"].startswith("refused:")
    assert db.query_one("SELECT COUNT(*) c FROM articles")["c"] == 0


def test_refused_status_with_type_publishes_nothing(monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)

    class _RefusingText(_StubText):
        def chat_json(self, messages, **kw):
            user = messages[-1]["content"] if messages else ""
            if "Summarise each transcript window" in user:
                return super().chat_json(messages, **kw)
            if "Choose the article type" in user:
                return {"template_id": "discussion",
                        "rationale": "uncertain essentials",
                        "supporting_window_ids": ["w-000"],
                        "alternatives_rejected": [],
                        "excluded_window_ids": [],
                        "status": "refused"}
            raise AssertionError("must stop at refusal")

    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(_RefusingText([])))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert not res["ok"] and res["error"].startswith("refused:")
    assert db.query_one("SELECT COUNT(*) c FROM articles")["c"] == 0


def test_typed_run_records_ready_validation(monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)
    text = _StubText(_plan_sections())
    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(text))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert res["ok"], res.get("error")
    assert res["validation"]["status"] == "ready"
    assert all(f["severity"] == "major"
               for f in res["validation"]["findings"])
    row = db.query_one("SELECT job_id, stage, state, metrics_json FROM stages "
                       "WHERE job_id=? AND stage='v9_validate'",
                       (res["v9_job_id"],))
    assert row["state"] == "done"
    import json as _json2
    assert _json2.loads(row["metrics_json"])["validation"] == "ready"


def test_needs_review_keeps_private_candidate_and_publishes_nothing(
        monkeypatch, tmp_path):
    settings, db = _setup_db(tmp_path)

    class _InventingText(_StubText):
        def chat_json(self, messages, **kw):
            if any("Plan the article" in (m.get("content") or "") for m in messages):
                return super().chat_json(messages, **kw)
            user = messages[-1]["content"] if messages else ""
            for sec in self.plan_sections:
                if sec["heading"] in user:
                    body = ("It costs $300 and uses 2 1/2 cups of sugar. "
                            "[0:01](https://example.com)")
                    return _section_reply(sec["section_id"], sec["heading"]) | {
                        "body": body}
            return super().chat_json(messages, **kw)

    monkeypatch.setattr("yt2wiki.backends.loader.ModelManager",
                        lambda *a, **k: _StubManager(_InventingText(_plan_sections())))
    service = Service(settings, db)
    res = v9run.run_source(settings, db, service, "s", enable_typed=True)
    assert not res["ok"]
    assert res["error"].startswith("needs_review:")
    assert db.query_one("SELECT COUNT(*) c FROM articles")["c"] == 0
    cand = tmp_path / "work" / res["v9_job_id"] / "v9_validate" / "candidate.md"
    assert cand.is_file() and "$300" in cand.read_text()
    import json as _json
    findings = _json.loads((cand.parent / "findings.json").read_text())
    assert findings["status"] == "needs_review"
    assert any(f["code"] == "unsupported_exact_value"
               for f in findings["findings"])
