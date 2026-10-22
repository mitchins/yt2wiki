"""V9-Z1 rubric judge unit tests (no models, no network, stub backend)."""

from __future__ import annotations

import pytest

from yt2wiki.v9 import judge as J


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


class StubVLM:
    """Canned backend: labels for classify prompts, JSON for rubric prompts."""

    def __init__(self, labels=(), rubric="", subject="widget thing device"):
        self.labels = list(labels)
        self.rubric = rubric
        self.subject = subject
        self.calls = 0

    def describe_batch(self, images, prompt, **kw):
        self.calls += 1
        return [self.labels.pop(0) if self.labels else "instructional"
                for _ in images]

    def rank_images(self, images, prompt, **kw):
        self.calls += 1
        if "Classify this single image" in prompt:
            return self.labels.pop(0) if self.labels else "instructional"
        if "main subject" in prompt:
            return self.subject
        return self.rubric


def _cand(fid, path, t):
    return {"frame_id": fid, "t": float(t), "path": path, "kind": "scene"}


def _sec():
    return {"section_id": "sec-01", "heading": "Do the thing",
            "body": "Do the thing with the widget.", "t_start": 0.0, "t_end": 60.0}


def _rubric_json(entries, decision="KEEP", winner=1, pair=(),
                 explanation="exp", direction="none", weakness="shows the widget clearly in place"):
    import json
    return json.dumps({
        "scores": [{"idx": e[0], "relevance": e[1], "clarity": e[2],
                    "instructional": e[3], "payoff": e[4],
                    "veto": e[5] if len(e) > 5 else False,
                    "veto_reason": "", "context_ok": e[6] if len(e) > 6 else False,
                    "weakness": weakness} for e in entries],
        "decision": decision, "winner": winner, "pair": list(pair),
        "explanation": explanation,
        "refinement": {"direction": direction, "detail": "d"}})


# --- pure policy ------------------------------------------------------------

def test_decide_keep_on_marginal_gain():
    scored = [{"idx": 1, "total": 6, "relevance": 2, "veto": False, "context_ok": False},
              {"idx": 2, "total": 7, "relevance": 2, "veto": False, "context_ok": False}]
    assert J._decide(scored, 1, rounds_left=2)["action"] == "KEEP"


def test_decide_replace_on_material_gain():
    scored = [{"idx": 1, "total": 4, "relevance": 2, "veto": False, "context_ok": False},
              {"idx": 2, "total": 7, "relevance": 2, "veto": False, "context_ok": False}]
    d = J._decide(scored, 1, rounds_left=2)
    assert d["action"] == "REPLACE" and d["winner_idx"] == 2


def test_decide_relevance_gate_blocks_single():
    scored = [{"idx": 1, "total": 7, "relevance": 1, "veto": False, "context_ok": False}]
    assert J._decide(scored, 1, rounds_left=1)["action"] == "EXPAND"
    assert J._decide(scored, 1, rounds_left=0)["action"] == "OMIT"


def test_decide_context_ok_exception_for_diagrams():
    scored = [{"idx": 1, "total": 6, "relevance": 1, "veto": False, "context_ok": True}]
    assert J._decide(scored, 1, rounds_left=0)["action"] == "KEEP"


def test_decide_veto_never_wins():
    scored = [{"idx": 1, "total": 5, "relevance": 2, "veto": False, "context_ok": False},
              {"idx": 2, "total": 8, "relevance": 2, "veto": True, "context_ok": False}]
    d = J._decide(scored, 1, rounds_left=0)
    assert d["action"] == "KEEP" and d["winner_idx"] == 1


def test_decide_weak_best_expands_then_omits():
    scored = [{"idx": 1, "total": 3, "relevance": 2, "veto": False, "context_ok": False}]
    assert J._decide(scored, 1, rounds_left=2)["action"] == "EXPAND"
    assert J._decide(scored, 1, rounds_left=0)["action"] == "OMIT"


# --- parsing -----------------------------------------------------------------

def test_parse_rubric_clamps_and_computes_totals():
    raw = _rubric_json([(1, 2, 9, -4, 1)])
    p = J._parse_rubric(raw, 2)
    assert p["scores"][0]["clarity"] == 2
    assert p["scores"][0]["instructional"] == 0
    assert p["scores"][0]["total"] == 2 + 2 + 0 + 1


def test_parse_rubric_rejects_garbage():
    assert J._parse_rubric("not json at all", 2) is None
    assert J._parse_rubric('{"decision": "KEEP"}', 2) is None
    assert J._parse_rubric(_rubric_json([(9, 2, 2, 2, 2)]), 2) is None


# --- refinement windows -------------------------------------------------------

def test_refine_directions_cover_windows():
    frames = [{"frame_id": f"f-{t}", "t": float(t)} for t in range(0, 200, 5)]
    sec = {"t_start": 100.0, "t_end": 120.0}
    seen = set()
    early = J.refine_candidates(frames, sec, seen, "earlier", "")
    assert early and max(f["t"] for f in early) <= 110.0
    late = J.refine_candidates(frames, sec, seen, "later", "")
    assert late and min(f["t"] for f in late) >= 110.0
    dense = J.refine_candidates(frames, sec, seen, "denser", "", anchor_t=115.0)
    assert dense and all(100.0 <= f["t"] <= 130.0 for f in dense)
    pair = J.refine_candidates(frames, sec, seen, "pair", "")
    assert len(pair) == 2 and pair[0]["t"] < pair[-1]["t"]


# --- end-to-end with stub backend ----------------------------------------------

def _shuffled_positions(n, seed):
    import random
    order = list(range(n))
    random.Random(seed).shuffle(order)
    # order[k] = index into shown[] at position k+1; invert: shown[i] -> pos
    pos_of = {shown_i: k + 1 for k, shown_i in enumerate(order)}
    return pos_of


def test_z1_keep_flow(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20), _cand("f-a2", tiny_pngs[2], 30)]
    frames = [init, *alts]
    pos = _shuffled_positions(3, 3)
    # shown = [init, a1, a2]; init total 7, alternatives weaker
    by_fid = {"f-init": (2, 2, 2, 1), "f-a1": (2, 1, 1, 1), "f-a2": (2, 1, 1, 0)}
    entries = [(pos[i], *by_fid[f]) for i, f in enumerate(["f-init", "f-a1", "f-a2"])]
    be = StubVLM(rubric=_rubric_json(entries, decision="KEEP", winner=pos[0]))
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec["decision"] == "KEEP"
    assert rec["final"] == ["f-init"]
    assert rec["vlm_calls"] >= 2  # batched prefilter + comparative pass


def test_z1_replace_flow(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20)]
    frames = [init, *alts]
    pos = _shuffled_positions(2, 3)
    by_fid = {"f-init": (2, 1, 1, 0), "f-a1": (2, 2, 2, 2)}
    entries = [(pos[i], *by_fid[f]) for i, f in enumerate(["f-init", "f-a1"])]
    be = StubVLM(rubric=_rubric_json(entries, decision="REPLACE", winner=pos[1]))
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec["decision"] == "REPLACE"
    assert rec["final"] == ["f-a1"]
    assert "material margin" in rec["reason"]


def test_z1_pair_flow(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20), _cand("f-a2", tiny_pngs[2], 30)]
    frames = [init, *alts]
    pos = _shuffled_positions(3, 3)
    by_fid = {"f-init": (2, 1, 1, 1), "f-a1": (2, 2, 1, 1), "f-a2": (2, 2, 1, 1)}
    entries = [(pos[i], *by_fid[f]) for i, f in enumerate(["f-init", "f-a1", "f-a2"])]
    be = StubVLM(rubric=_rubric_json(entries, decision="USE_PAIR", winner=pos[1],
                                      pair=(pos[1], pos[2]),
                                      explanation="before/after states"))
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec["decision"] == "USE_PAIR"
    assert rec["final"] == ["f-a1", "f-a2"]


def test_z1_omit_when_all_vetoed_no_rounds(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20)]
    frames = [init, *alts]
    be = StubVLM(labels=["talking_head", "promo_or_sales"],
                 rubric=_rubric_json([(1, 0, 0, 0, 0, True)]))
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames,
                                  max_rounds=0, seed=3)
    assert rec["decision"] == "OMIT"
    assert rec["final"] in (None, [])


def test_z1_falls_back_to_veto_loop_on_bad_json(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20)]
    frames = [init, *alts]
    be = StubVLM(rubric="complete nonsense, no json here")
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec.get("z1_fallback") == "v9z-veto"


def test_z1_backstop_drops_vacuous_winner_without_overlap(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20)]
    frames = [init, *alts]
    pos = _shuffled_positions(2, 3)
    by_fid = {"f-init": (2, 2, 2, 2), "f-a1": (2, 1, 1, 0)}
    entries = [(pos[i], *by_fid[f]) for i, f in enumerate(["f-init", "f-a1"])]
    be = StubVLM(
        rubric=_rubric_json(entries, decision="KEEP", winner=pos[0],
                            weakness="no visible action"),
        subject="two people reading books in a church")
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec["decision"] == "OMIT"
    assert "subject backstop dropped" in rec["reason"]


def test_z1_backstop_skipped_for_specific_weakness(tiny_pngs, no_ocr):
    init = _cand("f-init", tiny_pngs[0], 10)
    alts = [_cand("f-a1", tiny_pngs[1], 20)]
    frames = [init, *alts]
    pos = _shuffled_positions(2, 3)
    by_fid = {"f-init": (2, 2, 2, 1), "f-a1": (2, 1, 1, 0)}
    entries = [(pos[i], *by_fid[f]) for i, f in enumerate(["f-init", "f-a1"])]
    be = StubVLM(
        rubric=_rubric_json(entries, decision="KEEP", winner=pos[0]),
        subject="two people reading books in a church")
    rec = J.adjudicate_section_z1(be, _sec(), init, alts, frames, seed=3)
    assert rec["decision"] == "KEEP"
    assert rec["final"] == ["f-init"]
    assert "probe skipped" in rec["reason"]


def test_z1_expand_refines_and_replaces(tiny_pngs, no_ocr, monkeypatch):
    import json as _json
    init = _cand("f-init", tiny_pngs[0], 10)
    weak = _cand("f-weak", tiny_pngs[1], 20)
    fresh = _cand("f-fresh", tiny_pngs[4], 14)
    frames = [init, weak, fresh,
              _cand("fx1", tiny_pngs[2], 50), _cand("fx2", tiny_pngs[3], 55)]
    judged = {"n": 0}

    def fake_score(backend, section, ordered, current_pos, cache=None):
        judged["n"] += 1
        if judged["n"] == 1:
            ids = [c["frame_id"] for c in ordered]
            return {"scores": [
                {"idx": i + 1, "relevance": 2, "clarity": 1, "instructional": 1,
                 "payoff": 0, "total": 4, "veto": False, "veto_reason": "",
                 "context_ok": False,
                 "weakness": "shows the widget screen but the specific control is not demonstrated here",
                 "frame_id": fid} for i, fid in enumerate(ids)],
                "decision": "EXPAND", "winner": 1, "pair": [],
                "explanation": "Need a closer view of the control being described in this section",
                "refinement": {"direction": "denser",
                               "detail": "closer view of the widget control"}}, 1
        ids = [c["frame_id"] for c in ordered]
        win = ids.index("f-fresh") + 1 if "f-fresh" in ids else 1
        scores = []
        for i, fid in enumerate(ids):
            hot = fid == "f-fresh"
            scores.append(
                {"idx": i + 1, "relevance": 2, "clarity": 2 if hot else 1,
                 "instructional": 2 if hot else 1, "payoff": 1 if hot else 0,
                 "total": 7 if hot else 4, "veto": False, "veto_reason": "",
                 "context_ok": False,
                 "weakness": "clear close view of the widget control mid demonstration"
                 if hot else "shows the widget screen but not the control",
                 "frame_id": fid})
        return {"scores": scores,
            "decision": "REPLACE", "winner": win, "pair": [],
            "explanation": "The closer view demonstrates the control",
            "refinement": {"direction": "none", "detail": ""}}, 1

    monkeypatch.setattr(J, "score_candidates", fake_score)
    # fresh frame must survive the real prefilter veto: stub labels clean
    be = StubVLM(labels=["instructional"] * 12,
                 subject="widget thing device")
    rec = J.adjudicate_section_z1(be, _sec(), init, [weak], frames,
                                  seed=3, max_rounds=2)
    assert rec["decision"] == "REPLACE", rec
    assert any(r.get("retrieved") for r in rec["rounds"])
    assert rec["rounds"][0]["judgement"]["decision"] == "EXPAND"


def test_parse_rubric_rejects_nested_verdict():
    import json as _json
    nested = _json.dumps({"scores": [{"idx": 1, "relevance": 2, "clarity": 2,
                                      "instructional": 2, "payoff": 2,
                                      "decision": "REPLACE", "winner": 4,
                                      "pair": [], "explanation": "nested"}]})
    assert J._parse_rubric(nested, 2) is None


def test_parse_rubric_tolerates_fences_and_trailing_prose():
    raw = ('```json\n{"scores": [{"idx": 1, "relevance": 2, "clarity": 1,'
           '"instructional": 2, "payoff": 1, "veto": false, "veto_reason": "",'
           ' "context_ok": false, "weakness": "clear view of the widget"}],'
           ' "decision": "KEEP", "winner": 1, "pair": [], "explanation": "fine",'
           ' "refinement": {"direction": "none", "detail": ""}}\n```\nSome trailing prose.')
    p = J._parse_rubric(raw, 2)
    assert p is not None and p["decision"] == "KEEP" and p["winner"] == 1


def test_veto_flags_unclassifiable_label():
    stats = {"chars": 200, "text": "x" * 200, "max_h": 0.05, "area": 0.05, "error": ""}
    assert J._veto_from_label("unreadable", stats)["flagged"] is True
    assert J._veto_from_label("", stats)["flagged"] is True
    assert J._veto_from_label("instructional", stats)["flagged"] is False


def _ok_stats():
    return {"chars": 200, "text": "widget thing device notion",
            "max_h": 0.05, "area": 0.05, "error": ""}


def test_canonical_label_mapping():
    assert J._canonical_label("instructional") == "instructional"
    assert J._canonical_label("  Talking_Head. ") == "talking_head"
    assert J._canonical_label("UI screen") == "instructional"
    assert J._canonical_label("") == "unreadable"
    assert J._canonical_label(None) == "unreadable"
    assert J._canonical_label("unreadable") == "unreadable"
    assert J._canonical_label("notion") == "unreadable"
    assert J._canonical_label("A screenshot of an app") == "unreadable"


def test_veto_flags_empty_unreadable_oov_with_valid_ocr():
    for lab in ["", "unreadable", "notion", "a", "laptop showing notion"]:
        chk = J._veto_from_label(J._canonical_label(lab), _ok_stats(), False)
        assert chk["flagged"] is True, lab
        assert chk["reason"] == "unclassifiable-label"


def test_veto_keeps_clean_known_label_with_valid_ocr():
    chk = J._veto_from_label(J._canonical_label("instructional"), _ok_stats(), True)
    assert chk["flagged"] is False


def test_vet_candidate_shares_canonical_validation(tiny_pngs, no_ocr, monkeypatch):
    be_empty = StubVLM()
    monkeypatch.setattr(J, "classify_frame",
                        lambda backend, path: J._canonical_label(""))
    chk = J._vet_candidate(be_empty, {"frame_id": "f", "path": tiny_pngs[0]}, False)
    assert chk["flagged"] is True
    assert chk["reason"] == "unclassifiable-label"
    chk2 = J._veto_from_label(J._canonical_label(""), _ok_stats(), False)
    assert (chk["flagged"], chk["reason"]) == (chk2["flagged"], chk2["reason"])


def test_llama_server_backend_build_and_resolution(tmp_path, monkeypatch):
    from yt2wiki.backends.loader import ModelManager
    from yt2wiki.backends.llama_server import (
        LlamaServerVLM, _device_env_for_child, _free_port)
    from yt2wiki.config import Settings
    # free port is bindable
    import socket
    p = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", p))
    # device env mapping: operator restriction respected, UUID entries kept
    env = _device_env_for_child(0)
    assert "CUDA_VISIBLE_DEVICES" in env
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-aaa,GPU-bbb")
    env2 = _device_env_for_child(1)
    assert env2["CUDA_VISIBLE_DEVICES"] == "GPU-bbb"
    # registry resolves the new vision models per profile
    s = Settings.load(data_dir=str(tmp_path), profile="compact")
    assert s.model("vision").key == "vl-qwen35-4b"
    assert s.model("vision").engine == "llamacpp-server"
    fb = [m.key for m in s.model_with_fallbacks("vision")]
    assert fb[0] == "vl-qwen35-4b" and "vl-2b" in fb
    s2 = Settings.load(data_dir=str(tmp_path), profile="standard")
    assert s2.model("vision").key == "vl-gemma-12b"
    # build dispatches to the server backend without touching the network
    m = ModelManager(s, recorder=lambda e: None)
    be = m.build("vision", s.model("vision"))
    assert isinstance(be, LlamaServerVLM)
    assert be.variant["gguf_file"] == "Qwen3.5-4B-Q4_K_M.gguf"
    # staged files resolve locally (no download attempted)
    gguf = tmp_path / "gguf" / "Qwen3.5-4B-Q4_K_M.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"fake-gguf")
    (tmp_path / "gguf" / "mmproj-F16.gguf").write_bytes(b"fake-mmproj")
    be2 = LlamaServerVLM(s.model("vision"), be.variant, "cuda", 0, tmp_path)
    assert be2._resolve("Qwen3.5-4B-Q4_K_M.gguf").name == "Qwen3.5-4B-Q4_K_M.gguf"
    # missing files are a loud error, never a silent downgrade
    be3 = LlamaServerVLM(s.model("vision"), dict(be.variant, gguf_file="nope.gguf"),
                         "cuda", 0, tmp_path)
    try:
        be3._resolve("nope.gguf")
    except RuntimeError as e:
        assert "missing" in str(e)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected RuntimeError")
    # missing server binary is a loud error
    monkeypatch.delenv("LLAMA_SERVER_DIR", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        be2._server_binary()
    except RuntimeError as e:
        assert "llama-server" in str(e)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected RuntimeError")


def test_llama_image_budget_scaling():
    from yt2wiki.backends.llama_server import LlamaServerVLM
    from PIL import Image
    big = Image.new("RGB", (1280, 720))
    fit = LlamaServerVLM._fit(big, 384)
    assert fit.size[0] * fit.size[1] <= 384 * 1024 + 30000
    small = Image.new("RGB", (100, 100))
    assert LlamaServerVLM._fit(small, 384).size == (100, 100)
    assert LlamaServerVLM._fit(big, 100000).size == (1280, 720)


def test_llama_reasoning_args_accepts_yaml_bool_off():
    from yt2wiki.backends.llama_server import _reasoning_args
    assert _reasoning_args({"reasoning": False}) == ["--reasoning", "off", "--reasoning-budget", "0"]
    assert _reasoning_args({"reasoning": "off"}) == ["--reasoning", "off", "--reasoning-budget", "0"]
    assert _reasoning_args({"reasoning": "OFF"}) == ["--reasoning", "off", "--reasoning-budget", "0"]
    assert _reasoning_args({}) == []
    assert _reasoning_args({"reasoning": "on"}) == []
    assert _reasoning_args({"reasoning": True}) == []


def _ocr_dict(words):
    return {"text": words,
            "height": [10] * len(words),
            "width": [20] * len(words)}


def test_ocr_stats_prefers_richer_psm_pass(monkeypatch, tmp_path):
    pytesseract = pytest.importorskip("pytesseract")
    from PIL import Image
    from yt2wiki.v9 import judge as J
    p = tmp_path / "f.png"
    Image.new("RGB", (1280, 720), (10, 10, 200)).save(p)
    calls = []

    def fake_data(im, config="", output_type=None):
        calls.append(config)
        if "psm 6" in config:
            return _ocr_dict(["The", "TrueNAS", "installation", "succeeded!"])
        return _ocr_dict([])
    monkeypatch.setattr(pytesseract, "image_to_data", fake_data)
    s = J.ocr_stats(str(p))
    assert any("psm 6" in c for c in calls)
    assert s["chars"] == len("The TrueNAS installation succeeded!")
    assert "truenas" in s["text"]
    assert s["error"] == ""


def test_ocr_stats_fail_closed_when_tesseract_fails(monkeypatch, tmp_path):
    pytesseract = pytest.importorskip("pytesseract")
    from PIL import Image
    from yt2wiki.v9 import judge as J
    p = tmp_path / "f.png"
    Image.new("RGB", (64, 64)).save(p)
    monkeypatch.setattr(pytesseract, "image_to_data",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tesseract down")))
    s = J.ocr_stats(str(p))
    assert s["error"] == "ocr-failed"
    assert J._veto_from_label("instructional", s, True)["flagged"] is True


def test_hardware_label_maps_to_instructional():
    from yt2wiki.v9 import judge as J
    assert J._canonical_label("hardware") == "instructional"
    assert J._canonical_label("Hardware.") == "instructional"


def test_subject_backstop_rescue_on_explanation_overlap(monkeypatch, tiny_pngs, no_ocr):
    from yt2wiki.v9 import judge as J
    frames = [{"frame_id": f"f{i}", "t": float(i * 10),
               "path": p, "kind": "scene"} for i, p in enumerate(tiny_pngs[:3])]
    sec = {"section_id": "s", "heading": "ZFS pool throughput",
           "body": "The RAIDZ1 array reached 250 MB/s copying files.",
           "t_start": 0.0, "t_end": 60.0}
    rubric = ('{"scores": [{"idx": 1, "relevance": 2, "clarity": 2,'
              ' "instructional": 2, "payoff": 2, "veto": false,'
              ' "veto_reason": "", "context_ok": false, "weakness": "None"}],'
              ' "decision": "KEEP", "winner": 1, "pair": [],'
              ' "explanation": "Shows the 250 MB/s transfer result.",'
              ' "refinement": {"direction": "none", "detail": ""}}')

    class Stub:
        def __init__(self, subject, rubric):
            self.subject = subject
            self.rubric = rubric
        def describe_batch(self, images, prompt, **kw):
            return ["instructional" for _ in images]
        def rank_images(self, images, prompt, **kw):
            if "Classify this single image" in prompt:
                return "instructional"
            if "main subject" in prompt:
                return self.subject
            return self.rubric

    kept = J.adjudicate_section_z1(
        Stub("file transfer progress bar showing data transfer speed", rubric),
        sec, frames[0], [], frames, max_rounds=0, seed=0)
    assert kept["final"] == ["f0"]
    assert "explanation overlap" in kept["reason"]

    vacuous = rubric.replace(
        "Shows the 250 MB/s transfer result.", "Funny image, very clear.")
    dropped = J.adjudicate_section_z1(
        Stub("funny cat meme about weekends", vacuous),
        sec, frames[0], [], frames, max_rounds=0, seed=0)
    assert dropped["final"] in (None, [])
    assert "subject backstop dropped" in dropped["reason"]
