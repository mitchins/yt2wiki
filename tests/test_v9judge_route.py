"""V9 publisher wiring tests: the production runner must route every section
through the V9-Z1 rubric judge on the selected vision backend (no models,
no network, stub backend)."""

from __future__ import annotations

import pytest

from yt2wiki.v9 import judge as J
from yt2wiki.v9 import run as V9


@pytest.fixture()
def tiny_pngs(tmp_path):
    from PIL import Image
    paths = []
    for i in range(6):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (16, 16), (i * 40, i * 40, i * 40)).save(p)
        paths.append(str(p))
    return paths


@pytest.fixture()
def no_ocr(monkeypatch):
    monkeypatch.setattr(
        J, "ocr_stats",
        lambda p: {"chars": 200, "text": "x" * 200, "max_h": 0.05,
                   "area": 0.05, "error": ""})


_RUBRIC_TIE_KEEP = (
    '{"scores": ['
    '{"idx": 1, "relevance": 2, "clarity": 2, "instructional": 2, "payoff": 2,'
    ' "veto": false, "veto_reason": "", "context_ok": false,'
    ' "weakness": "clear view of the widget device thing"},'
    '{"idx": 2, "relevance": 2, "clarity": 2, "instructional": 2, "payoff": 2,'
    ' "veto": false, "veto_reason": "", "context_ok": false,'
    ' "weakness": "clear view of the widget device thing"}],'
    '"decision": "KEEP", "winner": 1, "pair": [],'
    '"explanation": "both candidates show the widget clearly for readers",'
    '"refinement": {"direction": "none", "detail": ""}}')


class StubVLM:
    """Canned vision backend: rank JSON, rubric JSON, classify labels."""

    def __init__(self, rank=""):
        self.rank = rank
        self.calls = []

    def describe_batch(self, images, prompt, **kw):
        self.calls.append("describe_batch")
        return ["instructional" for _ in images]

    def rank_images(self, images, prompt, **kw):
        self.calls.append(("rank_images", len(images)))
        if "Classify this single image" in prompt:
            return "instructional"
        if "editorial image judge" in prompt:
            return _RUBRIC_TIE_KEEP
        return self.rank


def _frames(paths):
    return [{"frame_id": f"f{i}", "t": float(i * 10), "path": p, "kind": "scene"}
            for i, p in enumerate(paths)]


def _sec():
    return {"section_id": "sec-01", "heading": "Do the thing",
            "body": "Do the thing with the widget.", "t_start": 0.0, "t_end": 60.0}


def test_runner_routes_section_through_rubric_judge(tiny_pngs, no_ocr):
    frames = _frames(tiny_pngs[:4])
    vb = StubVLM(rank='{"best": [2], "reason": "shows widget", '
                      '"second": 0, "second_reason": ""}')
    used: set[str] = set()
    chosen, record = V9.adjudicate_section_with_judge(
        vb, _sec(), frames, used, seed=0)
    # The judge ran and decided; rank alone did not publish.
    assert record["judge_decision"].startswith("KEEP")
    assert record["z1_fallback"] is None
    assert record["rounds"] and record["rounds"][0]["judgement"]["scores"]
    assert record["vlm_calls"] and record["vlm_calls"] >= 2
    assert record["vision_model"] == "StubVLM"
    assert [c["frame_id"] for c in chosen] == ["f1"]
    assert "f1" in used


def test_rank_fallback_is_reported_not_silent(tiny_pngs, no_ocr):
    frames = _frames(tiny_pngs[:4])
    vb = StubVLM(rank="")  # backend returns nothing: rank falls back
    used: set[str] = set()
    chosen, record = V9.adjudicate_section_with_judge(
        vb, _sec(), frames, used, seed=0)
    assert "fallback" in (record["rank_reason"] or "")
    # ... but the fallback pick still passes through the judge.
    assert record["judge_decision"].startswith("KEEP")
    assert record["z1_fallback"] is None
    assert [c["frame_id"] for c in chosen] == [record["initial"]]


def test_already_installed_final_is_dropped_loudly(tiny_pngs, no_ocr,
                                                     monkeypatch):
    # A refinement round can resurface a frame installed by an earlier
    # section; the helper must drop it and say so.
    frames = _frames(tiny_pngs[:4])
    vb = StubVLM(rank='{"best": [1], "reason": "shows widget", '
                      '"second": 0, "second_reason": ""}')
    monkeypatch.setattr(J, "adjudicate_section_z1",
                        lambda *a, **k: {"final": ["f1"], "decision": "REPLACE",
                                         "reason": "sharper widget view",
                                         "rounds": [], "shown": ["f1"],
                                         "frames_inspected": 2, "vlm_calls": 2,
                                         "elapsed_s": 0.2})
    used: set[str] = {"f1"}  # installed by an earlier section
    chosen, record = V9.adjudicate_section_with_judge(
        vb, _sec(), frames, used, seed=0)
    assert chosen == []
    assert "dropped already-installed" in (record["judge_reason"] or "")


def test_judge_omit_installs_nothing(tiny_pngs, no_ocr, monkeypatch):
    frames = _frames(tiny_pngs[:4])
    vb = StubVLM(rank='{"best": [2], "reason": "shows widget", '
                      '"second": 0, "second_reason": ""}')
    monkeypatch.setattr(J, "adjudicate_section_z1",
                        lambda *a, **k: {"final": None, "decision": "OMIT",
                                         "reason": "nothing useful",
                                         "rounds": [], "shown": [],
                                         "frames_inspected": 1, "vlm_calls": 1,
                                         "elapsed_s": 0.1})
    used: set[str] = set()
    chosen, record = V9.adjudicate_section_with_judge(
        vb, _sec(), frames, used, seed=0)
    assert chosen == []
    assert record["judge_decision"] == "OMIT"


def test_publish_drops_stale_assets(tmp_path):
    from PIL import Image
    from yt2wiki.v9 import publish as P
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki import paths as paths_mod

    data_dir = tmp_path / "data"
    (data_dir / "db").mkdir(parents=True)
    db = Database(data_dir / "db" / "t.sqlite3")
    db.migrate()
    import time as _t
    db.execute("INSERT INTO sources(source_id, kind, created_at) VALUES(?,?,?)",
               ("sx", "upload", _t.time()))
    db.execute("""INSERT INTO jobs(job_id, source_id, profile, fingerprint,
                      state, stage, created_at, updated_at)
                  VALUES(?,?,?,?,?,?,?,?)""",
               ("j", "sx", "compact", "fp", "running", "", _t.time(), _t.time()))
    settings = Settings.load(data_dir=str(data_dir), profile="compact")
    assert settings.paths.root == data_dir.resolve()

    src = tmp_path / "frame.jpg"
    Image.new("RGB", (32, 32), (9, 9, 9)).save(src)
    old_picks = {"sec-a": [{"frame_id": "f-old", "t": 1.0,
                             "path": str(src), "caption": ""}]}
    P.persist(settings, db, article_id="art9-x", source_id="sx", job_id="j",
              title="T", markdown="# T\n", sections=[], picks=old_picks,
              provenance={}, report={})
    assert (settings.paths.article_assets("art9-x") / "f-old.jpg").is_file()
    new_picks = {"sec-a": [{"frame_id": "f-new", "t": 2.0,
                             "path": str(src), "caption": ""}]}
    P.persist(settings, db, article_id="art9-x", source_id="sx", job_id="j",
              title="T", markdown="# T\n", sections=[], picks=new_picks,
              provenance={}, report={})
    assets = settings.paths.article_assets("art9-x")
    assert (assets / "f-new.jpg").is_file()
    assert not (assets / "f-old.jpg").exists()
    import zipfile
    names = zipfile.ZipFile(assets.parent / "article.zip").namelist()
    assert "assets/f-old.jpg" not in names
    assert "assets/f-new.jpg" in names


def test_vision_projectors_are_model_specific():
    """Qwen3.5 and Gemma projectors share a generic upstream filename but
    are different bytes; the registry must resolve distinct files."""
    import yaml
    from pathlib import Path
    reg_path = Path(__file__).resolve().parents[1] / "config" / "models.yaml"
    reg = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    q = reg["models"]["vl-qwen35-4b"]["variants"]["compact"]
    g = reg["models"]["vl-gemma-12b"]["variants"]["standard"]
    assert q["mmproj_file"] != g["mmproj_file"]
    from yt2wiki.backends.llama_server import LlamaServerVLM
    assert LlamaServerVLM is not None  # backend importable; resolution is by filename


def test_drop_far_links_keeps_window_and_assets():
    from yt2wiki.v9.publish import drop_far_links, assemble_markdown
    url = "https://youtube.com/watch?v=XvaXemGDSpk"
    body = ("Verdict here [4:11](https://youtube.com/watch?v=XvaXemGDSpk&t=251) "
            "and earlier [6:51](https://youtube.com/watch?v=XvaXemGDSpk&t=411).")
    out = drop_far_links(body, url, 410.0, 438.0)
    assert "[4:11](https://youtube.com/watch?v=XvaXemGDSpk&t=251)" not in out
    assert "4:11" not in out  # false provenance dropped entirely
    assert "[6:51](https://youtube.com/watch?v=XvaXemGDSpk&t=411)" in out
    img = "![alt](assets/f-abc.jpg)"
    assert drop_far_links(img, url, 0.0, 10.0) == img
    # New consolidated presentation: inline per-sentence citations become one
    # subdued section reference (6:50–7:18), not two inline links
    md, _ = assemble_markdown(
        "T", "S",
        [{"section_id": "s", "heading": "H", "body": body,
          "t_start": 410.0, "t_end": 438.0}], {},
        {"url": url, "attribution_line": "A"})
    assert "Verdict here" in md and "4:11" not in md.split("## H")[1].split("*Source")[0]
    assert "Source 6:50" in md and "&t=410" in md
    assert md.count("&t=") == 1  # only the consolidated reference
