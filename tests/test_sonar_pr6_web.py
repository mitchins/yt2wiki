"""PR6 web-app complexity refactors: behaviour locks for extracted helpers.

Covers the 12 Sonar HIGH findings in web/app.py (create_app 290,
_swap 36, _set_caption 17, _loop 16), web/inference.py (validate 26,
readiness 24, test_endpoint 22, _qualify_vision 23), web/views.py
(thumbnail 29, _decisions 16, job_view 30) and web/static/editor.js
(nesting). Each extracted helper is exercised directly; pure
restructures (route flattening) are covered by existing web suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_caption_slot_and_set_caption():
    from yt2wiki.web.app import _caption_slot, _set_caption

    md = "text\n![a](assets/f1.jpg)\n\n*old*\nmore"
    assert _set_caption(md, "f1", "new cap") == "text\n![a](assets/f1.jpg)\n\n*new cap*\nmore"
    # no match -> None, markdown unchanged
    assert _caption_slot(md.split("\n"), 0, "f1", "x") is None
    assert _set_caption("no image", "f1", "x") == "no image"
    # image without trailing blank: no slot
    assert _set_caption("![a](assets/f1.jpg)\ntext\n", "f1", "x") == "![a](assets/f1.jpg)\ntext\n"
    # empty caption falls back to Source frame
    out = _set_caption("![a](assets/f1.jpg)\n\n*old*\n", "f1", "")
    assert "*Source frame*" in out


def test_locate_and_move_image():
    from yt2wiki.web.app import (
        _locate_image_block,
        _move_image,
        _section_insert_range,
    )

    lines = "## A\n![a](assets/f1.jpg)\n\n*cap*\ntext\n## B\nmore\n".split("\n")
    at, end = _locate_image_block(lines, "f1")
    assert (at, end) == (1, 4)
    rest = lines[:1] + lines[4:]
    start, stop = _section_insert_range(rest, "B")
    assert rest[start].startswith("## B")
    assert stop >= start
    out = _move_image("\n".join(lines), "f1", "B")
    assert out.index("## B") < out.index("assets/f1.jpg")
    with pytest.raises(ValueError, match="not in the article"):
        _move_image("## A\nx\n", "nope", "Nope")
    with pytest.raises(ValueError, match="Unknown section"):
        _move_image("## A\n![a](assets/f1.jpg)\n\n*cap*\n", "f1", "Nope")


def test_swap_validation_helpers():
    from fastapi import HTTPException

    from yt2wiki.web.app import _apply_new_caption, _check_swap_ids, _ensure_inside

    _check_swap_ids("f1", "f2")
    with pytest.raises(HTTPException):
        _check_swap_ids("bad fid!", None)
    with pytest.raises(HTTPException):
        _check_swap_ids("f1", "bad fid!")
    out = _apply_new_caption("![old](assets/f2.jpg)\n", "f2", "hi")
    assert "assets/f2.jpg" in out

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        assets = Path(tmp)
        (assets / "f1.jpg").write_bytes(b"x")
        _ensure_inside(assets, "f1", None)
        with pytest.raises(HTTPException):
            _ensure_inside(assets, "../evil", None)


def test_revert_helpers_present_and_shallow(tmp_path):
    from yt2wiki.web.app import (
        _drop_image_markdown,
        _restore_missing_asset,
        _route_api_revert,
        _sync_restored_captions,
    )

    assert callable(_restore_missing_asset)
    assert callable(_sync_restored_captions)
    assert callable(_route_api_revert)
    # pure markdown drop: image line + caption removed, surrounding kept
    md = "a\n![x](assets/f1.jpg)\n\n*cap*\nb"
    dropped = _drop_image_markdown(md, "f1")
    assert "assets/f1.jpg" not in dropped
    assert "a" in dropped
    assert "b" in dropped

    # missing frame row: restore is a no-op, never raises
    class _DB:
        def query_one(self, *a, **k):
            return None

    class _App:
        state = type("S", (), {"db": _DB()})()

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "f9.jpg").write_bytes(b"x")
    _restore_missing_asset(_App(), "art1", assets, "missing", "![x](assets/missing.jpg)")
    assert (assets / "f9.jpg").is_file()


def test_normalise_roles():
    from yt2wiki.web.inference import InferenceConfigError, _normalise_roles, validate

    providers = {"a": {"url": "http://x", "model": "m", "api_key_env": ""}}
    assert _normalise_roles({"writer": "a"}, providers)["writer"] == "a"
    with pytest.raises(InferenceConfigError):
        _normalise_roles({"writer": "missing"}, providers)
    with pytest.raises(InferenceConfigError):
        _normalise_roles("bad", providers)
    cfg = validate({"providers": [{"name": "a", "url": "http://x", "model": "m", "api_key_env": ""}],
                    "roles": {"writer": "a"}})
    assert cfg["roles"]["writer"] == "a"


def test_thumbnail_helpers():
    from yt2wiki.web import views

    names, outline = views._outline_frame_names([{"frames": ["f1"]}, {"frames": ["f2"]}])
    assert names == ["f1.jpg", "f2.jpg"]
    ordered = views._ordered_outline_names([{"frames": ["f1"]}, {"frames": ["f2"]}])
    assert set(ordered) == {"f1.jpg", "f2.jpg"}
    assert views._markdown_frame_names("![a](assets/f9.jpg) ![b](assets/f2.jpg)") == ["f9.jpg", "f2.jpg"]

    class _P:
        def __init__(self, root):
            self._root = root

        def article_assets(self, aid):
            return Path(self._root) / aid

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        aid = "art1"
        (Path(tmp) / aid).mkdir(parents=True, exist_ok=True)
        (Path(tmp) / aid / "f2.jpg").write_bytes(b"x")
        thumb = views.thumbnail_for(_P(tmp), aid, json.dumps([{"frames": ["f1"]}, {"frames": ["f2"]}]), "")
        assert thumb == f"/assets/{aid}/f2.jpg"
        assert views.thumbnail_for(_P(tmp), aid, None, "![a](assets/f2.jpg)") == f"/assets/{aid}/f2.jpg"


def test_decisions_and_job_view_helpers():
    from yt2wiki.web.views import (
        _decision_candidates,
        _decisions,
        _job_steps,
        _pipeline_of,
        job_view,
    )

    rec = {"candidates": [{"frame_id": "f1", "t": 1.0}], "chosen": ["f1"]}
    cands = _decision_candidates(rec, "s1", {"f1"}, lambda fid: True)
    assert cands[0]["src"] == "/frames/s1/f1.jpg"
    report = {"selection": [{**rec, "section_id": "s", "judge_decision": "KEEP"}]}
    dec = _decisions(report, "s1", [{"section_id": "s", "heading": "H"}])
    assert dec[0]["heading"] == "H"
    assert _pipeline_of({"pipeline": "v9"}, {}) == "v9"
    assert _pipeline_of({}, {"v9_write": {}}) == "v9"
    assert _pipeline_of({}, {}) == "v9"
    steps = _job_steps("v9", {}, "queued", {"job_id": "j"})
    assert isinstance(steps, list)
    job = {"job_id": "j", "config_json": "{}", "state": "queued", "stages": []}
    view = job_view(job, live=set())
    assert view["state"] == "stale"
    assert view["pipeline"] == "v9"


def test_inference_probe_helpers(monkeypatch):
    from yt2wiki.web import inference

    content, prompt = inference._vision_probe_content("h {heading} b {body} n {n} c {current_pos}")
    assert len(content) == 5
    assert prompt

    class _R:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {"choices": [{"message": {"content": '{"scores": [1], "decision": "KEEP"}'}}]}
            self.text = "" if status == 200 else "schema not supported"

        def json(self):
            return self._body

    assert inference._vision_reply_ok(_R(), lambda raw, n: {"scores": [1], "decision": "KEEP"})
    assert not inference._vision_reply_ok(_R(), lambda raw, n: None)

    import httpx

    def _fail_get(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _fail_get)
    probed = inference._probe_models("http://x", {}, 1.0)
    assert probed["models"] == []
    assert probed["llm_like"] is False

    # schema-reject once then success: retry without response_format succeeds
    calls = {"n": 0}

    def _flaky_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _R(status=400, body={})
        return _R(status=200)

    monkeypatch.setattr(httpx, "post", _flaky_post)
    ok = inference._vision_probe_ok("http://x", {}, {"messages": []}, 1.0,
                                    {"type": "json_schema"}, lambda raw, n: {"scores": [1], "decision": "KEEP"})
    assert ok is True
    assert calls["n"] == 2


def test_create_app_factory_registers_routes(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.web.app import create_app

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    app = create_app(settings)
    paths = {r.path for r in app.routes}
    assert "/" in paths
    assert "/health" in paths
    assert "/api/articles_list" in paths
