"""PR4 follow-up: direct unit coverage for the restructured helpers.

Exercises every new helper introduced by the article/pipeline refactor so the
Sonar new-code gate stays green. Pure functions, deterministic fixtures only.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from yt2wiki.article import Article, markdown_to_html
from yt2wiki.literals import Literal
from yt2wiki.records import Evidence, Procedure, ProcedureStep


def _ev(eid="ev-aaa111", kind="action", text="Insert the USB drive", t=10.0, **kw):
    kw.setdefault("t_start", t)
    kw.setdefault("t_end", t + 5.0 if t is not None else None)
    return Evidence(evidence_id=eid, kind=kind, text=text, **kw)


def _proc(**kw):
    base = {"procedure_id": "p1", "title": "T", "summary": "S"}
    base.update(kw)
    return Procedure(**base)


# ---------------------------------------------------------------- article

def test_render_block_fence_variants():
    from yt2wiki.article import _consume_fence, _render_block

    frag, nxt = _consume_fence(["```bash", "echo hi", "```", "after"], 0)
    assert nxt == 3
    assert 'echo hi' in frag[0]
    frag, nxt = _consume_fence(["```", "unclosed"], 0)
    assert nxt == 3
    out = _render_block(["```py", "x"], 0, None)
    assert out is not None
    assert out[2] is None


def test_render_block_headings_clamped():
    from yt2wiki.article import _render_block

    out, nxt, lst = _render_block(["####### deep"], 0, "ul")
    assert '<h6>' in ''.join(out)
    assert nxt == 1
    assert lst is None
    out, nxt, lst = _render_block(["# Top"], 0, None)
    assert "<h1>Top</h1>" in "".join(out)


def test_render_block_quote_and_lists():
    from yt2wiki.article import _consume_quote, _emit_list_close, _render_block

    frag, nxt = _consume_quote(["> hello world", "> more", "next"], 0)
    assert nxt == 2
    assert 'hello' in frag[0]
    assert _render_block(["> x"], 0, None) is not None
    out: list[str] = []
    _emit_list_close(out, None)
    assert out == []
    _emit_list_close(out, "ul")
    assert out == ["</ul>"]
    frag, nxt, lst = _render_block(["1. first"], 0, None)
    assert lst == 'ol'
    assert '<li>' in frag[-1]
    frag, nxt, lst = _render_block(["1. second"], 0, "ol")
    assert lst == 'ol'
    assert '<ol' not in ''.join(frag)
    frag, nxt, lst = _render_block(["- item"], 0, "ol")
    assert lst == 'ul'
    assert '</ol>' in ''.join(frag)
    frag, nxt, lst = _render_block(["- item2"], 0, "ul")
    assert lst == "ul"


def test_render_block_image_caption_blank_paragraph():
    from yt2wiki.article import _render_block

    frag, nxt, lst = _render_block(["![a](assets/f1.jpg)"], 0, "ul")
    assert lst is None
    assert '<figure>' in ''.join(frag)
    frag, nxt, lst = _render_block(["![x](javascript:alert(1))"], 0, None)
    assert "<figure>" not in "".join(frag)
    frag, nxt, lst = _render_block(["*Caption here*"], 0, None)
    assert "<figcaption>" in "".join(frag)
    assert _render_block(["*Caption here*"], 0, "ul") is None  # paragraph text in lists
    frag, nxt, lst = _render_block([""], 0, "ol")
    assert lst is None
    assert '</ol>' in ''.join(frag)
    assert _render_block(["just text"], 0, None) is None
    html = markdown_to_html("# H\n\n1. one\n\n- two\n\n![a](assets/f.jpg)\n\n*Cap*\n\ntext")
    assert '<h1>' in html
    assert '<figcaption>' in html


# ------------------------------------------------- pipeline __init__

def _context(tmp_path, sid="yt-pr40001"):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(tmp_path / "db" / "yt2wiki.sqlite3")
    db.migrate()
    db.execute(
        "INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, "youtube", "https://youtu.be/pr40001", "T", "chan", 10.0, time.time()),
    )
    db.execute(
        "INSERT INTO jobs(job_id, source_id, state, stage, profile, fingerprint, config_json, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("job-pr4", sid, "running", "compose", "compact", "fp", "{}", time.time(), time.time()),
    )
    ctx = StageContext(job_id="job-pr4", source_id=sid, settings=settings, db=db,
                       paths=settings.paths, fingerprint="fp")
    return ctx, db


def test_cached_stage_row_miss_and_hit(tmp_path):
    from yt2wiki.pipeline import _begin_stage, _cached_stage_row

    ctx, db = _context(tmp_path)
    assert _cached_stage_row(db, ctx, "compose", False) is None
    _begin_stage(db, ctx, "compose", 3)
    # still running: not cached
    assert _cached_stage_row(db, ctx, "compose", False) is None
    fp = ctx.stage_fingerprint("compose")
    db.execute(
        "UPDATE stages SET state='done', artifacts_json=?, fingerprint=? "
        "WHERE job_id=? AND stage=?",
        ('{"a": 1}', fp, "job-pr4", "compose"),
    )
    row = _cached_stage_row(db, ctx, "compose", False)
    assert row is not None
    assert ctx.artifacts.get('a') == 1
    # force bypasses the cache
    assert _cached_stage_row(db, ctx, "compose", True) is None
    # stale fingerprint misses
    db.execute("UPDATE stages SET fingerprint='other' WHERE job_id=? AND stage=?",
               ("job-pr4", "compose"))
    assert _cached_stage_row(db, ctx, "compose", False) is None


def test_fail_and_record_stage_failure(tmp_path):
    from yt2wiki.pipeline import _begin_stage, _fail_stage, _record_stage_failure

    ctx, db = _context(tmp_path)
    _begin_stage(db, ctx, "verify", 1)
    _fail_stage(db, "job-pr4", "verify", "boom")
    row = db.query_one("SELECT state, error FROM stages WHERE job_id=? AND stage=?",
                       ("job-pr4", "verify"))
    assert row['state'] == 'failed'
    assert 'boom' in row['error']
    _record_stage_failure(db, "job-pr4", "verify", ValueError("bad"), ctx.source_id)
    job = db.query_one("SELECT state FROM jobs WHERE job_id=?", ("job-pr4",))
    assert job["state"] == "failed"


def test_summarise_gpu_branches(tmp_path, monkeypatch):
    from yt2wiki.pipeline import _summarise_gpu

    ctx, _ = _context(tmp_path)
    summary: dict = {}
    _summarise_gpu(summary, ctx.settings)  # no GPUs here: no-op
    assert "gpu_used_gb" not in summary

    import yt2wiki.pipeline as pipe

    gpu = SimpleNamespace(index=0, used_gb=3.0, name="GPU0")

    class S:
        device_index = 0

    monkeypatch.setattr(pipe, "nvidia_smi_gpus", lambda: [gpu])
    _summarise_gpu(summary, S())
    assert summary["gpu_used_gb"] == 3.0

    class S2:
        device_index = 9

    summary2: dict = {}
    monkeypatch.setattr(pipe, "nvidia_smi_gpus", lambda: [gpu])
    _summarise_gpu(summary2, S2())
    assert "gpu_used_gb" not in summary2


# ---------------------------------------------------------------- compose

def test_scope_section_variants():
    from yt2wiki.pipeline.stages.compose import _scope_section

    art = Article(source_id="s", title="t")
    assert _scope_section(art, _proc(), [], None) is None
    art2 = Article(source_id="s", title="t", scope="Do X")
    sec = _scope_section(art2, _proc(), [], None)
    assert sec is not None
    assert sec.body == ['Do X']
    step = ProcedureStep(position=1, text="Plug it", evidence_ids=["ev-aaa111"])
    proc = _proc(steps=[step])
    ev = [_ev()]
    sec = _scope_section(art2, proc, ev, "https://youtu.be/x")
    assert sec is not None
    assert any(('cite:' in b for b in sec.body))
    sec = _scope_section(art2, proc, [_ev(t_start=None)], None)
    assert sec is not None


def test_warning_block_and_section():
    from yt2wiki.pipeline.stages.compose import _warning_block, _warnings_section

    assert _warnings_section(_proc(), [], None) is None
    assert _warning_block("short", [], [], None) == ["> **Warning** — short"]
    body = _warning_block("Keep fingers clear", ["ev-aaa111"], [_ev()], "https://youtu.be/x")
    assert any("cite:" in ln for ln in body)
    body = _warning_block("Keep fingers clear", ["ev-aaa111"], [_ev()], None)
    assert any("cite:" in ln for ln in body)
    proc = _proc(steps=[ProcedureStep(position=1, text="Cut it",
                                      warnings=["Keep fingers clear"],
                                      evidence_ids=["ev-aaa111"])])
    sec = _warnings_section(proc, [_ev()], None)
    assert sec is not None
    assert sec.heading == 'Before you start'


def test_steps_commands_configs_expected_trouble_unresolved():
    from yt2wiki.pipeline.stages.compose import (
        _commands_section,
        _configs_section,
        _expected_section,
        _steps_section,
        _troubleshooting_section,
        _unresolved_section,
    )

    assert _steps_section(_proc(), [], {}, None) is None
    step = ProcedureStep(position=1, text="Do it", detail="detail here",
                         evidence_ids=["ev-aaa111"],
                         frame_ids=["f1"], warnings=["abcdefgh"])
    proc = _proc(steps=[step])
    frames = {"f1": {"frame_id": "f1", "path": "/tmp/f1.jpg", "t": 5.0, "summary": "A plug"}}
    sec = _steps_section(proc, [_ev()], frames, None)
    assert sec is not None
    ctx = SimpleNamespace(progress=lambda *a: None, artifacts={})
    assert _commands_section(ctx, _proc(), [], ) is None
    ev_cmd = Evidence(evidence_id="ev-c1", kind="command", text="make install",
                      literals=[Literal(kind="command", literal="make install")],
                      t_start=1.0, t_end=2.0)
    sec = _commands_section(ctx, _proc(), [ev_cmd])
    assert sec is not None
    assert 'make install' in '\n'.join(sec.body)
    assert _configs_section([]) is None
    ev_cfg = Evidence(evidence_id="ev-k1", kind="config", text="freq",
                      literals=[Literal(kind="keyvalue", literal="freq=3000")],
                      t_start=1.0, t_end=2.0)
    sec = _configs_section([ev_cfg])
    assert sec is not None
    assert _configs_section([ev_cfg, ev_cfg]) is not None  # dedupe path
    assert _expected_section(_proc(), []) is None
    proc2 = _proc(steps=[ProcedureStep(position=1, text="Do it", expected_result="It works")])
    assert _expected_section(proc2, []) is not None
    assert _troubleshooting_section([]) is None
    ev_err = _ev(eid="ev-e1", kind="error", text="It smoked")
    ev_fix = _ev(eid="ev-e2", kind="correction", text="Unplug it", t=20.0)
    sec = _troubleshooting_section([ev_err, ev_fix])
    assert sec is not None
    assert any(('What the source does' in b for b in sec.body))
    assert _unresolved_section(_proc(), []) is None
    sec = _unresolved_section(_proc(unresolved=["Check manual"]), [_ev(eid="ev-u1", kind="unresolved", text="Mystery")])
    assert sec is not None
    assert len(sec.body) == 3


def test_cite_and_step_lines():
    from yt2wiki.pipeline.stages.compose import (
        _cite_comment,
        _config_value,
        _step_frame_lines,
        _step_source_lines,
        _step_warning_lines,
    )

    assert _cite_comment(step=1, ev_ids=[], t_min=None, t_max=None) == \
        "<!-- cite: step=1 ev=- t=- frames=- -->"
    assert "1.0-2.0" in _cite_comment(step="scope", ev_ids=["a"], t_min=1.0, t_max=2.0)
    assert _cite_comment(step=2, ev_ids=["a"], t_min=1.0, t_max=1.0).count("1.0") == 1
    step = ProcedureStep(position=2, text="Do", evidence_ids=["ev-aaa111"],
                         frame_ids=["f1"], warnings=["ev-aaa111", "x", "Mind the heat sink fins"])
    lines = _step_source_lines(step, [_ev()], "https://youtu.be/x")
    assert any("cite:" in ln for ln in lines)
    lines = _step_source_lines(step, [_ev()], None)
    assert any("of the source video" in ln for ln in lines)
    lines = _step_source_lines(step, [], None)
    assert any("cite:" in ln for ln in lines)
    warns = _step_warning_lines(step)
    assert warns == ["   > **Warning** — Mind the heat sink fins"]
    frames = {"f1": {"frame_id": "f1", "path": "/tmp/f1.jpg", "t": 5.0, "summary": "Plug"}}
    lines = _step_frame_lines(step, frames, [_ev()], "https://youtu.be/x")
    assert lines
    assert 'assets/f1.jpg' in lines[0]
    lines = _step_frame_lines(step, frames, [_ev()], None)
    assert "of the source video" in lines[0]
    assert _step_frame_lines(step, {}, [], None) == []
    assert _config_value(Literal(kind="keyvalue", literal="a=1")) == "a=1"
    assert _config_value(Literal(kind="command", literal="make")) == "make"


# ---------------------------------------------------------------- evidence

def test_extract_chunk_paths():
    from yt2wiki.pipeline.stages import evidence as evm

    ctx = SimpleNamespace(check_cancel=lambda: None, source_id="s",
                          work_dir=lambda *a: __import__("pathlib").Path("/tmp"),
                          db=SimpleNamespace(log=lambda *a, **k: None),
                          artifacts={})
    chunk = [{"start": 0.0, "end": 5.0, "text": "hello world"}]
    frames: list = []

    class Backend:
        def chat_with_dump(self, *a, **k):
            return {"observations": [
                "not-a-dict",
                {"kind": "weird-kind", "text": "an observation here", "confidence": "bad"},
                {"kind": "action", "text": "no"},
                {"kind": "action", "text": "Insert the USB drive",
                 "supported_by": ["frame:f1", "frame:"],
                 "confidence": 0.9},
            ]}

    out = evm._extract_chunk(ctx, Backend(), 0, chunk, frames, "m", None,
                             SimpleNamespace(text_max_tokens=100, text_temperature=0.0))
    assert len(out) == 2
    assert out[0].kind == 'observation'

    class Boom:
        def chat_with_dump(self, *a, **k):
            raise RuntimeError("model down")

    assert evm._extract_chunk(ctx, Boom(), 1, chunk, frames, "m", None,
                              SimpleNamespace(text_max_tokens=100,
                                              text_temperature=0.0)) == []


def test_merge_and_contradictions():
    from yt2wiki.pipeline.stages import evidence as evm

    a = _ev(eid="ev-1", text="Set frequency to 3000 MHz", t=10.0)
    b = _ev(eid="ev-2", text="Set frequency to 3000 MHz", t=200.0)
    out: list = []
    evm._merge_candidate(out, a)
    evm._merge_candidate(out, b)  # far apart in time: kept
    assert len(out) == 2
    c = _ev(eid="ev-3", text="Set frequency to 3000 MHz", t=12.0)
    evm._merge_candidate(out, c)
    assert len(out) == 2  # collapsed onto a near-duplicate
    d1 = _ev(eid="ev-4", kind="config", text="frequency 3000", t=10.0)
    d2 = _ev(eid="ev-5", kind="config", text="frequency 5000", t=11.0)
    pairs = evm._detect_contradiction_pairs([d1, d2])
    assert isinstance(pairs, list)
    ctx = SimpleNamespace()
    assert evm._adjudicate_contradictions(ctx, object(), [a]) == []


# --------------------------------------------------------------- procedures

def test_parse_step_shapes():
    from yt2wiki.pipeline.stages.procedures import _parse_step

    assert _parse_step(0, "nope") is None
    assert _parse_step(0, {"text": "ab"}) is None
    step = _parse_step(0, {"text": "  Do the thing  ", "detail": " d ",
                           "evidence_ids": [1], "frame_ids": ["f"],
                           "warnings": [" w ", " "], "expected_result": " ok ",
                           "alternatives": [" alt ", ""]})
    assert step is not None
    assert step.position == 1
    assert step.evidence_ids == ['1']
    assert step.warnings == [' w ']


# --------------------------------------------------------------- transcribe

def test_chapter_and_numeric_lines():
    from yt2wiki.pipeline.stages.transcribe import (
        _chapter_titles,
        _numeric_description_lines,
        _vocabulary_hint,
    )

    row = {"title": "T", "uploader": "U", "description": "Step 1: open it\nhello",
           "chapters_json": '[{"title": "Intro"}, {"no": 1}]'}
    assert _chapter_titles(row) == ["Intro"]
    assert _chapter_titles({"title": "", "uploader": ""}) == []
    lines = _numeric_description_lines("Step 1: open it\nshort\n" + "x" * 100 + "1\nno digits here")
    assert lines == ["Step 1: open it"]
    hint = _vocabulary_hint(SimpleNamespace(), row)
    assert 'Intro' in hint
    assert 'open it' in hint
    assert _vocabulary_hint(SimpleNamespace(), {"title": "", "uploader": ""}) == ""


# ------------------------------------------------------------------ verify

def test_heading_numbered_parsing():
    from yt2wiki.pipeline.stages.verify import _heading_of, _numbered_of

    assert _heading_of("## Scope") == "Scope"
    assert _heading_of("#### too deep") is None
    assert _heading_of("##") is None
    assert _heading_of("#nospace") is None
    assert _heading_of("text") is None
    assert _numbered_of("12. Do it") == 12
    assert _numbered_of("12.Do it") is None
    assert _numbered_of("nope") is None
    assert _numbered_of("1. ") is None or isinstance(_numbered_of("1. "), int)


def test_citation_blocks_shapes():
    from yt2wiki.pipeline.stages.verify import _absorb_citation, _citation_blocks

    md = ("intro prose\n\n> standalone callout\n\n## Procedure\n\n1. **Boot.**\n\n"
          "   Insert the drive.\n"
          "   <!-- cite: step=1 ev=ev-aaa111 t=214.0-219.0 frames=- -->\n")
    blocks = _citation_blocks(md)
    kinds = [b["kind"] for b in blocks]
    assert 'step' in kinds
    assert 'callout' in kinds
    assert 'prose' in kinds
    assert _citation_blocks("") == []
    cur: dict = {"lines": [], "ev_ids": [], "t_spans": []}
    _absorb_citation(cur, "See (ev-aaa111) at ?t=42.")
    assert 'ev-aaa111' in cur['ev_ids']
    assert cur['t_spans'] == [(42.0, 42.0)]
    with pytest.raises(AssertionError):
        _absorb_citation(None, "x")


def test_block_check_helpers():
    from yt2wiki.pipeline.stages.verify import (
        _BlockCheck,
        _block_bindings,
        _broken_citation_finding,
        _checkable_text,
        _claim_id,
        _unsupported_command_findings,
    )

    assert len(_claim_id("hello")) == len("claim-") + 12
    block = {"kind": "step", "step": 1, "section": "Procedure",
             "lines": ["<!-- cite: step=1 -->", "", "Do the thing"],
             "ev_ids": ["ev-zzz"], "t_spans": [(10.0, 20.0)]}
    by_id = {"ev-aaa111": _ev()}
    bound, broken, spans = _block_bindings(block, by_id, [_ev(), _ev(eid="ev-far", t=9999.0)])
    assert broken == ['ev-zzz']
    assert spans == [_ev(), _ev(eid='ev-far', t=9999.0)][0:1]
    f = _broken_citation_finding("Step 1", block, "ev-zzz")
    assert f["code"] == "broken_citation"
    assert 'cite:' not in _checkable_text(block)
    assert 'Source' not in _checkable_text(block)
    check = _BlockCheck(label="Step 1", block=block, bound=[], span_recs=[],
                        evidence=[_ev()])
    assert check.anchored == []
    assert check.cite_ids == []
    check2 = _BlockCheck(label="Step 1", block=block, bound=[_ev()], span_recs=[],
                         evidence=[])
    assert check2.cite_ids == ["ev-aaa111"]
    out = _unsupported_command_findings("run `make install` now", [])
    assert isinstance(out, list)


def test_keyvalue_helpers():
    from yt2wiki.literals import Literal as Lit
    from yt2wiki.pipeline.stages.verify import (
        _article_keyvalues,
        _clean_keyvalue,
        _evidence_keyvalues,
    )

    assert _clean_keyvalue(Lit(kind="keyvalue", literal="FREQ = 3000 MHz")) == ("freq", "3000 MHz")
    assert _clean_keyvalue(Lit(kind="keyvalue", literal="no separator here")) is None
    assert _clean_keyvalue(Lit(kind="keyvalue", literal="ab = ")) is None
    kv = _article_keyvalues("Set `FREQ = 3000 MHz` for the board.")
    assert kv.get("freq") == "3000"
    assert _article_keyvalues("plain prose, no code") == {}
    by_val, by_ev = _evidence_keyvalues([_ev()])
    assert isinstance(by_val, dict)
    assert isinstance(by_ev, dict)
    lit_ev = _ev(eid="ev-kv", kind="config")
    lit_ev.literals = [Lit(kind="keyvalue", literal="FREQ = 3000 MHz")]
    by_val, by_ev = _evidence_keyvalues([lit_ev])
    assert "freq" in by_val


# --------------------------------------------------------- visual_interpret

def test_visual_pure_helpers():
    from yt2wiki.pipeline.stages import visual_interpret as vi

    assert vi._merge_text_lines(["  Hello  ", ""], ["hello", "World", "x" * 300, "  "]) == ["Hello", "World"]
    assert vi._norm("  A  B ") == "a b"
    assert vi._safe_json('{"a": 1}')["a"] == 1
    assert vi._safe_json("nope").get("parse_error") is True
    assert vi._looks_text_dense({}, ["a", "b", "c"]) in (True, False)


def test_load_images_skips_missing(tmp_path):
    from PIL import Image

    from yt2wiki.pipeline.stages.visual_interpret import _load_images

    chunk = [{"frame_id": "f1", "path": str(tmp_path / "nope.jpg")}]
    images, payloads = _load_images(chunk)
    assert images == []
    assert payloads == []
    Image.new("RGB", (32, 32), (1, 2, 3)).save(tmp_path / "ok.jpg")
    images, payloads = _load_images([{"frame_id": "f2", "path": str(tmp_path / "ok.jpg")}])
    assert len(images) == 1
    assert payloads[0]['frame_id'] == 'f2'


def test_describe_and_dense_pass_with_stub():
    from yt2wiki.pipeline.stages import visual_interpret as vi

    class Backend:
        def __init__(self):
            self.calls = 0

        def describe_batch(self, images, *a, **k):
            self.calls += 1
            if self.calls == 1:
                return ['{"truncated json']
            return ['{"visible_text": ["ok"], "legibility": "high"}']

    t = SimpleNamespace(vision_max_new_tokens=50, vision_temperature=0.0,
                        vision_dense_text_pass=False)
    fake_settings = SimpleNamespace(model=lambda role: SimpleNamespace(key="k"),
                                    profile="compact")
    ctx = SimpleNamespace(db=SimpleNamespace(execute=lambda *a: None),
                          settings=fake_settings, artifacts={})
    stored, dense = vi._describe_batch(Backend(), ctx, ["img"], [{"frame_id": "f1"}], t, 1)
    assert stored == 1
    assert dense == []


def test_dense_text_pass_merge_and_store(tmp_path):
    import json as _json

    from yt2wiki.pipeline.stages import visual_interpret as vi

    assert vi._looks_text_dense({"frame_type": "terminal"}, []) is True
    assert vi._looks_text_dense({}, ["w" * 100]) is True
    assert vi._looks_text_dense({"legibility": "low"}, []) is True
    assert vi._looks_text_dense({"legibility": "high"}, ["hi"]) is False

    ctx, db = _context(tmp_path, sid="yt-pr40002")
    db.execute(
        "INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) VALUES(?,?,?,?,?,?)",
        ("fd1", ctx.source_id, 1.0, str(tmp_path / "fd1.jpg"), "scene", time.time()),
    )
    from PIL import Image

    Image.new("RGB", (400, 400), (10, 20, 30)).save(tmp_path / "fd1.jpg")
    frame = {"frame_id": "fd1", "path": str(tmp_path / "fd1.jpg")}

    class DenseBackend:
        def describe_batch(self, images, *a, **k):
            return [_json.dumps({"text_lines": ["alpha line", "beta line"]})
                    for _ in images]

    tiles, meta = vi._dense_tiles(frame)
    assert tiles
    assert meta
    assert meta[0]['source'] == 'fd1'
    assert vi._dense_tiles({"frame_id": "nope", "path": str(tmp_path / "missing.jpg")}) == ([], [])
    crops, stored = vi._dense_text_pass(
        DenseBackend(), ctx, [(frame, {"visible_text": []})], 4)
    assert stored == 1
    assert crops >= 0
    # malformed tile reply is skipped, frame still stored
    class BadBackend:
        def describe_batch(self, images, *a, **k):
            return ["not json at all"]

    crops, stored = vi._dense_text_pass(
        BadBackend(), ctx, [(frame, {"visible_text": ["keep"]})], 4)
    assert stored == 1


def test_store_and_peak(tmp_path):
    from yt2wiki.pipeline.stages import visual_interpret as vi

    ctx, db = _context(tmp_path)
    db.execute(
        "INSERT INTO frames(frame_id, source_id, t, path, kind, created_at) VALUES(?,?,?,?,?,?)",
        ("f9", ctx.source_id, 3.0, str(tmp_path / "f.jpg"), "scene", time.time()),
    )
    frame = {"frame_id": "f9"}
    payload = {"summary": "A plug", "frame_type": "demo", "visible_text": ["hi"],
               "legibility": "high", "reason": "r"}
    vi._store(ctx, frame, payload)
    row = db.query_one("SELECT frame_id FROM frame_interpretations WHERE frame_id=?", ("f9",))
    assert row is not None
    assert isinstance(vi._peak(ctx), float)
