"""HTTP ASR backend tests (no network, no weights).

Covers the /asr wire contract, response parsing, bidirectional validation
and the no-silent-fallback guarantees: a text-only server is a hard error
when words are required, absent confidence stays NULL, and unsupported
options are reported, never emulated.
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest import mock

import pytest

from yt2wiki.backends import asr_http
from yt2wiki.backends.asr import Segment, Transcript, Word
from yt2wiki.backends.asr_http import (
    AsrHttpError,
    HttpAsrBackend,
    build_query,
    parse,
    post_asr,
    validate,
    wav_duration_s,
    write_sine_wav,
)


# ------------------------------------------------------------------ fixtures
def _word(start, end, text, prob=0.9):
    return {"start": start, "end": end, "word": text, "probability": prob}


def _payload(**over):
    base = {
        "language": "en",
        "text": "hello world from the server",
        "segments": [
            {"id": 0, "seek": 0, "start": 0.0, "end": 1.2, "text": "hello world",
             "tokens": [1, 2], "avg_logprob": -0.2, "no_speech_prob": 0.01,
             "compression_ratio": 1.1, "temperature": 0.0,
             "words": [_word(0.0, 0.5, "hello"), _word(0.5, 1.0, "world")]},
            {"id": 1, "seek": 120, "start": 1.2, "end": 2.4, "text": "from the server",
             "tokens": [3], "avg_logprob": -0.3, "no_speech_prob": 0.02,
             "compression_ratio": 1.0, "temperature": 0.0,
             "words": [_word(1.2, 1.7, "from"), _word(1.7, 2.4, "server")]},
        ],
    }
    base.update(over)
    return base


def _wav(tmp_path: Path, seconds: float = 3.0) -> Path:
    return write_sine_wav(tmp_path / "probe.wav", seconds=seconds)


def _no_models(*a, **k):
    import httpx as _httpx
    raise _httpx.ConnectError("no /models")


# ------------------------------------------------------------------ wire bits
def test_build_query_drives_server_options():
    q = build_query(language="en", word_timestamps=True, vad_filter=True)
    assert "output=json" in q and "word_timestamps=true" in q and "vad_filter=true" in q
    assert "language=en" in build_query(language="en", word_timestamps=False, vad_filter=False)
    with pytest.raises(AsrHttpError, match="no language"):
        build_query(language="  ", word_timestamps=False, vad_filter=False)


def test_sine_wav_is_valid_mono_16k(tmp_path):
    p = _wav(tmp_path)
    assert abs(wav_duration_s(p) - 3.0) < 0.01
    import wave
    with wave.open(str(p), "rb") as wf:
        assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (16000, 1, 2)


def test_wav_duration_rejects_missing(tmp_path):
    with pytest.raises(AsrHttpError):
        wav_duration_s(tmp_path / "nope.wav")


# ------------------------------------------------------------------ parse
def test_parse_valid_response():
    t = parse(_payload(), duration_s=3.0, require_words=True, server="s")
    assert isinstance(t, Transcript)
    assert t.language == "en"
    assert len(t.segments) == 2
    assert [w.word for w in t.segments[0].words] == ["hello", "world"]
    assert t.segments[0].avg_logprob == pytest.approx(-0.2)
    assert t.segments[0].words[0].probability == pytest.approx(0.9)
    assert t.text.startswith("hello world")


def test_parse_absent_metrics_stay_null_not_zero():
    data = _payload()
    for seg in data["segments"]:
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            seg.pop(key)
        for w in seg["words"]:
            w.pop("probability")
    t = parse(data, duration_s=3.0, require_words=True, server="s")
    assert t.segments[0].avg_logprob is None
    assert t.segments[0].words[0].probability is None
    # NULLs survive the JSONL round-trip
    d = t.to_dict()
    assert d["segments"][0]["avg_logprob"] is None


def test_parse_text_only_server_is_hard_error_when_words_required():
    data = _payload()
    for seg in data["segments"]:
        seg["words"] = []
    with pytest.raises(AsrHttpError, match="no word timestamps"):
        parse(data, duration_s=3.0, require_words=True, server="s (text-only)")


def test_parse_wordless_accepted_when_words_not_required():
    data = _payload()
    for seg in data["segments"]:
        seg["words"] = []
    t = parse(data, duration_s=3.0, require_words=False, server="s")
    assert len(t.segments) == 2 and t.segments[0].words == []


def test_parse_collects_all_problems():
    data = {"language": "", "text": "", "segments": [
        {"start": "x", "end": 1.0, "text": "bad"},
        {"start": 5.0, "end": 2.0, "text": "  "},
        {"start": 1.0, "end": 2.0, "text": "ok",
         "words": [{"start": 9.0, "end": 9.5, "word": "far"}]},
    ]}
    with pytest.raises(AsrHttpError) as exc:
        parse(data, duration_s=3.0, require_words=False, server="s")
    msg = str(exc.value)
    assert "s" in msg and "non-numeric" in msg
    assert "bad span" in msg or "empty text" in msg
    assert "outside segment" in msg


def test_parse_zero_duration_words_warn_not_fail():
    data = _payload()
    for seg in data["segments"]:
        for w in seg["words"][:2]:
            w["end"] = w["start"]  # server could not align these
    t = parse(data, duration_s=3.0, require_words=True, server="s")
    assert len(t.segments) == 2
    assert len(t.warnings) == 1 and "zero-duration" in t.warnings[0]


def test_parse_empty_segments_is_error():
    with pytest.raises(AsrHttpError, match="segments"):
        parse({"language": "en", "text": "", "segments": []},
              duration_s=3.0, require_words=False, server="s")


# ------------------------------------------------------------------ validate
def _t(*segs) -> Transcript:
    return Transcript(language="en", language_probability=1.0, duration_s=10.0,
                      segments=list(segs))


def _s(start, end, text="words here", words=()):
    return Segment(start=start, end=end, text=text, words=list(words))


def _w(start, end, text="w"):
    return Word(start=start, end=end, word=text)


def test_validate_accepts_clean_transcript():
    t = _t(_s(0.0, 2.0, words=[_w(0.0, 1.0, "a"), _w(1.0, 2.0, "b")]),
           _s(2.0, 4.0, words=[_w(2.0, 3.0, "c")]))
    problems, warnings = validate(t, 10.0, require_words=True)
    assert problems == [] and warnings == []


def test_validate_rejects_bad_bounds_and_order():
    t = _t(_s(2.0, 4.0, words=[_w(2.0, 3.0)]),
           _s(1.0, 1.5, words=[_w(1.0, 1.5)]),   # starts before previous
           _s(9.5, 99.0))                          # exceeds duration
    problems, _ = validate(t, 10.0, require_words=False)
    assert any("before previous" in p for p in problems)
    assert any("exceeds duration" in p for p in problems)


def test_validate_rejects_empty_text_and_word_outside_segment():
    t = _t(_s(0.0, 1.0, text="   ", words=[_w(0.0, 0.5)]),
           _s(1.0, 2.0, words=[Word(start=5.0, end=5.5, word="far")]))
    problems, _ = validate(t, 10.0, require_words=False)
    assert any("empty text" in p for p in problems)
    assert any("outside segment" in p for p in problems)


def test_validate_rejects_nonfinite_and_empty():
    t = Transcript(language="  ", language_probability=None,
                   duration_s=0.0, segments=[])
    problems, _ = validate(t, 0.0, require_words=False)
    assert any("duration" in p for p in problems)
    assert any("language" in p for p in problems)
    assert any("no segments" in p for p in problems)
    bad = _t(Segment(start=math.nan, end=1.0, text="x"))
    bad_problems, _ = validate(bad, 10.0, require_words=False)
    assert any("non-finite" in p for p in bad_problems)


# ------------------------------------------------------------------ post_asr
def _client_mock(**kwargs):
    m = mock.Mock()
    for k, v in kwargs.items():
        setattr(m, k, v)
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = m
    return ctx


def test_post_asr_reports_http_failure(tmp_path):
    err = mock.Mock(status_code=500, text="boom")
    err.raise_for_status.side_effect = RuntimeError("500")
    with (mock.patch("httpx.Client", return_value=_client_mock(post=lambda *a, **k: err)),
          pytest.raises(AsrHttpError, match="HTTP 500")):
        post_asr("http://asr:9000", _wav(tmp_path), query="output=json",
                 timeout=5.0)


def test_post_asr_reports_non_json(tmp_path):
    ok = mock.Mock(status_code=200, text="<html>nope</html>")
    ok.json.side_effect = ValueError("no json")
    with (mock.patch("httpx.Client", return_value=_client_mock(post=lambda *a, **k: ok)),
          pytest.raises(AsrHttpError, match="non-JSON")):
        post_asr("http://asr:9000", _wav(tmp_path), query="output=json",
                 timeout=5.0)


def test_post_asr_reports_non_dict(tmp_path):
    ok = mock.Mock(status_code=200)
    ok.json.return_value = ["not", "a", "dict"]
    with (mock.patch("httpx.Client", return_value=_client_mock(post=lambda *a, **k: ok)),
          pytest.raises(AsrHttpError, match="expected an object")):
        post_asr("http://asr:9000", _wav(tmp_path), query="output=json",
                 timeout=5.0)


def test_post_asr_reports_connection_failure_with_server(tmp_path):
    import httpx
    with (mock.patch("httpx.Client", side_effect=httpx.ConnectError("refused")),
          pytest.raises(AsrHttpError, match="http://asr:9000")):
        post_asr("http://asr:9000", _wav(tmp_path), query="output=json",
                 timeout=5.0)


# ------------------------------------------------------------------ backend
def _spec(**variant):
    from yt2wiki.config import ModelSpec
    return ModelSpec(key="asr-http", role="asr", engine="asr-http",
                     repo="whisper-asr-webservice/asr-http",
                     variants={"compact": variant})


def test_backend_requires_url():
    with pytest.raises(AsrHttpError, match="no ASR server"):
        HttpAsrBackend(_spec(), {}, "cpu", None, base_url="")


def test_backend_transcribe_records_unsupported_and_progress(tmp_path):
    wav = _wav(tmp_path)
    backend = HttpAsrBackend(_spec(), {}, "cpu", None,
                             base_url="http://asr:9000", model="large-v3",
                             timeout=60.0)
    backend.load()
    seen = {}
    with mock.patch.object(asr_http, "post_asr", return_value=_payload()) as post, \
         mock.patch.object(asr_http, "detect_language", return_value=("en", 0.98)):
        t = backend.transcribe(wav, language="en", vad_filter=True,
                               initial_prompt="vocab hint",
                               on_progress=lambda *a: seen.update(done=a))
    assert len(t.segments) == 2
    assert sum(len(s.words) for s in t.segments) == 4
    assert t.language_probability == pytest.approx(0.98)
    assert "initial_prompt" in backend.last_unsupported
    assert seen["done"][0] == 2  # segments completed
    post.assert_called_once()
    assert "word_timestamps=true" in post.call_args.kwargs["query"]


def test_backend_transcribe_detect_failure_keeps_none(tmp_path):
    wav = _wav(tmp_path)
    backend = HttpAsrBackend(_spec(), {}, "cpu", None, base_url="http://asr:9000")
    backend.load()
    with mock.patch.object(asr_http, "post_asr", return_value=_payload()), \
         mock.patch.object(asr_http, "detect_language", return_value=(None, None)):
        t = backend.transcribe(wav)
    assert t.language_probability is None
    # no hint -> initial_prompt not listed as unsupported
    assert "initial_prompt" not in backend.last_unsupported


def test_backend_transcribe_wordless_server_fails_loudly(tmp_path):
    wav = _wav(tmp_path)
    backend = HttpAsrBackend(_spec(), {}, "cpu", None, base_url="http://asr:9000")
    backend.load()
    data = _payload()
    for seg in data["segments"]:
        seg["words"] = []
    with (mock.patch.object(asr_http, "post_asr", return_value=data),
          pytest.raises(AsrHttpError, match="no word timestamps")):
        backend.transcribe(wav)


# ------------------------------------------------------------------ registry
def test_default_asr_is_http_not_bundled(tmp_path):
    from yt2wiki.config import Settings
    settings = Settings.load(data_dir=tmp_path, profile="standard")
    spec = settings.model("asr")
    assert spec.engine == "asr-http" and spec.key == "asr-http"
    assert settings.model_with_fallbacks("asr") == [spec]  # no silent fallback


def test_local_asr_missing_extra_is_actionable(tmp_path, monkeypatch):
    import sys

    from yt2wiki.backends.asr import WhisperBackend
    from yt2wiki.config import Settings
    settings = Settings.load(data_dir=tmp_path, profile="compact",
                             model_overrides={"asr": "whisper-large-v3"})
    spec = settings.model("asr")
    assert spec.engine == "faster-whisper"
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    backend = WhisperBackend(spec, spec.variant("compact"), "cpu", None)
    with pytest.raises(RuntimeError, match="asr-local"):
        backend.load()


def test_role_timeout_defaults():
    from yt2wiki.backends.loader import _role_timeout
    assert _role_timeout("asr", None) == 300.0
    assert _role_timeout("writer", None) == 90.0
    assert _role_timeout("asr", 45.0) == 45.0


# ------------------------------------------------------------------ stage wiring
def test_transcribe_stage_records_server_and_unsupported(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    sid = "yt-http001"
    import time as _time
    db.execute("INSERT INTO sources(source_id, kind, origin_url, title, uploader, duration_s, created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (sid, "youtube", "https://youtu.be/http001", "T", "u", 3.0, _time.time()))
    ctx = StageContext(job_id="j1", source_id=sid, settings=settings, db=db,
                       paths=settings.paths, fingerprint=settings.fingerprint())
    wav = _wav(tmp_path)
    audio = Path(ctx.paths.source_audio(sid))
    audio.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(wav, audio)
    ctx.artifacts["audio_path"] = str(audio)
    ctx.artifacts["source_duration_s"] = 3.0

    from yt2wiki.backends.asr import Transcript as _T
    fake_t = _T(language="en", language_probability=None, duration_s=3.0,
                segments=[Segment(start=0.0, end=1.0, text="hi",
                                  words=[Word(start=0.0, end=1.0, word="hi")])])

    class FakeBackend:
        server = "http://asr:9000 (model large-v3)"
        last_unsupported = ["initial_prompt"]
        last_warnings = ["2 zero-duration words in 1 segment(s)"]

        def transcribe(self, *a, **k):
            return fake_t

    class FakeManager:
        def __init__(self, *a, **k): pass

        def acquire(self, role):
            assert role == "asr"
            from contextlib import contextmanager

            @contextmanager
            def _ctx():
                yield FakeBackend()

            return _ctx()

    with mock.patch("yt2wiki.pipeline.stages.transcribe.ModelManager", FakeManager):
        from yt2wiki.pipeline.stages import transcribe as stage
        metrics = stage.run(ctx)
    assert metrics["mode"] == "asr"
    assert metrics["asr_server"].startswith("http://asr:9000")
    assert "initial_prompt" in metrics["asr_unsupported_options"]
    assert metrics["asr_warnings"] == ["2 zero-duration words in 1 segment(s)"]
    rows = db.query("SELECT * FROM transcript_segments WHERE source_id=?", (sid,))
    assert len(rows) == 1 and rows[0]["text"] == "hi"


# ------------------------------------------------------------------ inference probe
def test_probe_asr_accepts_transcription_shape(monkeypatch):
    from yt2wiki.web import inference as inf

    class Resp:
        status_code = 200

        def json(self):
            return _payload()

    monkeypatch.setattr("httpx.get", _no_models)
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    out = inf.test_endpoint("http://asr:9000", timeout=5.0)
    assert out["ok"] is True and out["qualified_for_asr"] is True
    assert out.get("kind") == "asr"


def test_probe_asr_rejects_non_shape(monkeypatch):
    from yt2wiki.web import inference as inf

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "i am an llm"}}]}

    def _boom(*a, **k):
        raise _no_models()

    monkeypatch.setattr("httpx.get", _boom)
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    out = inf.test_endpoint("http://asr:9000", timeout=5.0)
    assert out["ok"] is False and out["qualified_for_asr"] is False


def test_probe_asr_unreachable_is_not_ok(monkeypatch):
    import httpx

    from yt2wiki.web import inference as inf

    def _boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("httpx.get", _boom)
    monkeypatch.setattr("httpx.post", _boom)
    out = inf.test_endpoint("http://asr:9000", timeout=5.0)
    assert out["ok"] is False


def test_probe_asr_requires_200(monkeypatch):
    from yt2wiki.web import inference as inf

    class Resp:
        status_code = 404
        text = "Not Found"

    monkeypatch.setattr("httpx.get", _no_models)
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    out = inf.test_endpoint("http://asr:9000", timeout=5.0)
    assert out["ok"] is False


# ------------------------------------------------------- coverage: wire edges
def test_wav_duration_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    with pytest.raises(AsrHttpError):
        wav_duration_s(bad)


def test_wav_duration_rejects_bad_rate(tmp_path):
    import wave as _wave
    p = tmp_path / "zero.wav"
    with _wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
    # rewrite header with a zero rate via mock
    with (mock.patch("wave.open", side_effect=ValueError("nope")),
          pytest.raises(AsrHttpError)):
        wav_duration_s(p)


def test_detect_language_branches(tmp_path):
    from yt2wiki.backends import asr_http as _m
    wav = _wav(tmp_path)

    def _resp(status=200, payload=None, boom=False):
        import httpx as _h
        if boom:
            raise _h.ConnectError("down")
        m = mock.Mock(status_code=status)
        if payload == "badjson":
            m.json.side_effect = ValueError("nope")
        else:
            m.json.return_value = payload
        return m

    def _client(resp):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.post.return_value = resp
        return ctx

    with mock.patch("httpx.Client", return_value=_client(_resp(500))):
        assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)
    import httpx as _h
    with mock.patch("httpx.Client", side_effect=_h.ConnectError("x")):
        assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)
    with mock.patch("httpx.Client", return_value=_client(_resp(200, ["nope"]))):
        assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)
    with mock.patch("httpx.Client", return_value=_client(_resp(200, "badjson"))):
        assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)
    with mock.patch("httpx.Client", return_value=_client(
            _resp(200, {"language_code": "", "confidence": "high"}))):
        assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)
    with mock.patch("httpx.Client", return_value=_client(
            _resp(200, {"language_code": "en", "confidence": 0.97}))):
        assert _m.detect_language("http://s", wav, timeout=5.0) == ("en", 0.97)


def test_parse_shape_problems():
    data = {"language": "en", "text": "x", "segments": [
        ["not", "a", "dict"],
        {"start": 0.0, "end": 1.0, "text": "ok", "words": "notalist"},
        {"start": 2.0, "end": 3.0, "text": "ok2",
         "words": [["bad"], {"start": 2.0, "end": 2.5},
                   {"start": "x", "end": 2.5, "word": "w"}]},
    ]}
    with pytest.raises(AsrHttpError) as exc:
        parse(data, duration_s=5.0, require_words=False, server="s")
    msg = str(exc.value)
    assert "not an object" in msg and "not a list" in msg
    assert "bad timestamp/text" in msg


def test_validate_word_edges():
    bad_finite = _t(_s(0.0, 2.0, words=[Word(start=float("nan"), end=1.0, word="x")]))
    p, _ = validate(bad_finite, 10.0, require_words=False)
    assert any("non-finite" in x for x in p)
    bad_span = _t(_s(0.0, 2.0, words=[Word(start=1.5, end=1.0, word="x")]))
    p, _ = validate(bad_span, 10.0, require_words=False)
    assert any("bad span" in x for x in p)
    ooo = _t(_s(0.0, 3.0, words=[_w(1.0, 2.0, "a"), _w(0.0, 0.5, "b")]))
    p, _ = validate(ooo, 10.0, require_words=False)
    assert any("out of order" in x for x in p)


def test_backend_auth_header_and_unload_and_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2WIKI_PROBE_KEY", "sekret")
    b = HttpAsrBackend(_spec(), {}, "cpu", None, base_url="http://asr:9000",
                       api_key_env="YT2WIKI_PROBE_KEY")
    assert b._headers.get("Authorization") == "Bearer sekret"
    b.load()
    b.unload()
    assert not b.loaded
    with pytest.raises(RuntimeError, match="not loaded"):
        b.transcribe(_wav(tmp_path))


def test_resolve_env_timeout_fallback(monkeypatch):
    from yt2wiki.backends.asr_http import resolve_env_variant
    monkeypatch.setenv("YT2WIKI_ASR_TIMEOUT", "bogus")
    out = resolve_env_variant({"timeout": 300.0})
    assert out["timeout"] == 300.0
    monkeypatch.setenv("YT2WIKI_ASR_TIMEOUT", "45")
    assert resolve_env_variant({})["timeout"] == 45.0


def test_post_asr_returns_dict_on_success(tmp_path):
    ok = mock.Mock(status_code=200)
    ok.json.return_value = {"language": "en", "text": "hi", "segments": []}
    with mock.patch("httpx.Client", return_value=_client_mock(post=lambda *a, **k: ok)):
        assert post_asr("http://asr:9000", _wav(tmp_path), query="output=json",
                        timeout=5.0)["language"] == "en"


def test_wav_duration_rejects_zero_rate(tmp_path):
    import contextlib

    class _BadWav:
        def getnframes(self): return 100
        def getframerate(self): return 0

    @contextlib.contextmanager
    def _fake_open(*a, **k):
        yield _BadWav()

    with (mock.patch("wave.open", _fake_open),
          pytest.raises(AsrHttpError, match="sample rate")):
        wav_duration_s(tmp_path / "x.wav")


def test_backend_empty_key_env_sets_no_header(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2WIKI_EMPTY_KEY", "")
    b = HttpAsrBackend(_spec(), {}, "cpu", None, base_url="http://asr:9000",
                       api_key_env="YT2WIKI_EMPTY_KEY")
    assert "Authorization" not in b._headers


def test_endpoint_models_bad_json_falls_back_to_asr_probe(monkeypatch):
    from yt2wiki.web import inference as inf

    class BadJson:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class NotFound:
        status_code = 404
        text = "nope"

    monkeypatch.setattr("httpx.get", lambda *a, **k: BadJson())
    monkeypatch.setattr("httpx.post", lambda *a, **k: NotFound())
    out = inf.test_endpoint("http://x:9000", timeout=5.0)
    assert out["ok"] is False and out.get("kind") == "asr"


def test_probe_asr_non_json_body(monkeypatch):
    from yt2wiki.web import inference as inf

    class Resp:
        status_code = 200

        def json(self):
            raise ValueError("html, not json")

    monkeypatch.setattr("httpx.get", _no_models)
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    out = inf.test_endpoint("http://x:9000", timeout=5.0)
    assert out["ok"] is False and "transcription JSON" in out.get("error", "")


def test_probe_asr_cleanup_failure_ignored(monkeypatch, tmp_path):
    from yt2wiki.web import inference as inf

    class Resp:
        status_code = 200

        def json(self):
            return _payload()

    monkeypatch.setattr("httpx.get", _no_models)
    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    import pathlib as _pl
    real_unlink = _pl.Path.unlink
    monkeypatch.setattr(_pl.Path, "unlink", lambda self, **k: (_ for _ in ()).throw(OSError("busy")))
    try:
        out = inf.test_endpoint("http://asr:9000", timeout=5.0)
    finally:
        monkeypatch.setattr(_pl.Path, "unlink", real_unlink)
    assert out["ok"] is True and out["qualified_for_asr"] is True


def test_head_wav_slices_long_audio(tmp_path):
    long = _wav(tmp_path / "long.wav", seconds=60.0)
    head = __import__("yt2wiki.backends.asr_http", fromlist=["_head_wav"])._head_wav(long)
    assert head != long and abs(__import__("yt2wiki.backends.asr_http", fromlist=["wav_duration_s"]).wav_duration_s(head) - 35.0) < 0.1
    head.unlink()
    head.parent.rmdir()


def test_transcribe_timeout_scales_with_duration(tmp_path, monkeypatch):
    seen = {}

    def fake_post(base, wav, *, query, timeout, headers=None):
        seen["timeout"] = timeout
        return _payload()

    with mock.patch.object(asr_http, "post_asr", side_effect=fake_post), \
         mock.patch.object(asr_http, "detect_language", return_value=(None, None)):
        b = HttpAsrBackend(_spec(), {}, "cpu", None, base_url="http://asr:9000", timeout=300.0)
        b.load()
        b.transcribe(_wav(tmp_path, seconds=3.0))
        assert seen["timeout"] == 300.0
        b.transcribe(_wav(tmp_path, seconds=900.0))
        assert seen["timeout"] == 900.0


def test_detect_language_cleanup_failure_ignored(tmp_path, monkeypatch):
    from yt2wiki.backends import asr_http as _m
    wav = _wav(tmp_path, seconds=60.0)

    def fake_head(path, *, seconds=35.0):
        p = tmp_path / "head.wav"
        import shutil as _sh
        _sh.copy(path, p)
        return p

    monkeypatch.setattr(_m, "_head_wav", fake_head)
    real_unlink = __import__("pathlib").Path.unlink
    monkeypatch.setattr(__import__("pathlib").Path, "unlink",
                        lambda self, **k: (_ for _ in ()).throw(OSError("busy")))
    import httpx as _h

    class Resp:
        status_code = 200

        def json(self):
            return {"language_code": "en", "confidence": 0.9}

    def _client(*a, **k):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.post.return_value = Resp()
        return ctx

    monkeypatch.setattr(_h, "Client", _client)
    try:
        assert _m.detect_language("http://s", wav, timeout=5.0) == ("en", 0.9)
    finally:
        monkeypatch.setattr(__import__("pathlib").Path, "unlink", real_unlink)


def test_llm_path_also_qualifies_dual_asr(monkeypatch):
    from yt2wiki.web import inference as inf

    class Models:
        status_code = 200

        def json(self):
            return {"data": [{"id": "m"}]}

    class Shape:
        status_code = 200

        def json(self):
            return {"language": "en", "text": "hi", "segments": []}


    def _post(*a, **k):
        # probe uploads a real sine wav; accept and report the shape
        return Shape()

    monkeypatch.setattr("httpx.get", lambda *a, **k: Models())
    monkeypatch.setattr("httpx.post", _post)
    out = inf.test_endpoint("http://dual:9000", timeout=5.0)
    assert out["ok"] is True and out.get("kind") == "llm"
    assert out.get("qualified_for_asr") is True


def test_qualify_vision_list_content_and_retry_error(monkeypatch):
    import httpx as _h

    from yt2wiki.web import inference as inf

    def _msg(content):
        return {"choices": [{"message": {
            "content": content, "reasoning_content": ""}}]}

    def _list_resp(*a, **k):
        return _h.Response(200, json=_msg(
            [{"type": "text", "text": '{"broken": '}]))

    def _flaky(*a, **k):
        if not hasattr(_flaky, "n"):
            _flaky.n = 0
        _flaky.n += 1
        if _flaky.n == 1:
            raise _h.ConnectError("blip")
        return _h.Response(200, json=_msg(
            '{"scores": [{"idx": 1, "relevance": 2, "clarity": 2, '
            '"instructional": 2, "payoff": 2, "weakness": "ok", '
            '"veto": false, "context_ok": false}, '
            '{"idx": 2, "relevance": 1, "clarity": 2, '
            '"instructional": 1, "payoff": 1, "weakness": "plain", '
            '"veto": false, "context_ok": false}], '
            '"decision": "KEEP", "winner": 1, "explanation": "fine", '
            '"images_received": 2}'))

    monkeypatch.setattr(_h, "post", _list_resp)
    assert inf._qualify_vision("http://x", {}, "m", 5.0) is False
    monkeypatch.setattr(_h, "post", _flaky)
    assert inf._qualify_vision("http://x", {}, "m", 5.0) is True


def test_qualify_vision_adapts_when_server_rejects_schemas_with_images(monkeypatch):
    import httpx as _h

    from yt2wiki.web import inference as inf

    def _msg(content):
        return {"choices": [{"message": {"content": content}}]}

    def _schema_refuser(*a, **k):
        payload = k.get("json", {})
        if payload.get("response_format"):
            return _h.Response(400, json={
                "error": {"message": "SCHEMA on a request with images is not supported"}})
        return _h.Response(200, json=_msg(
            '{"scores": [{"idx": 1, "relevance": 2, "clarity": 2, '
            '"instructional": 2, "payoff": 2, "weakness": "ok", '
            '"veto": false, "context_ok": false}, '
            '{"idx": 2, "relevance": 1, "clarity": 2, '
            '"instructional": 1, "payoff": 1, "weakness": "plain", '
            '"veto": false, "context_ok": false}], '
            '"decision": "KEEP", "winner": 1, "explanation": "fine", '
            '"images_received": 2}'))

    monkeypatch.setattr(_h, "post", _schema_refuser)
    # Same runtime adaptation: schema first, validated plain JSON after.
    assert inf._qualify_vision("http://x", {}, "m", 5.0) is True


def _qualify_reply(monkeypatch, content):
    import httpx as _h

    from yt2wiki.web import inference as inf

    def _ok(*a, **k):
        return _h.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(_h, "post", _ok)
    return inf._qualify_vision("http://x", {}, "m", 5.0)


def _two_scores(idxs=(1, 2), **over):
    import json as _json
    scores = [{"idx": i, "relevance": 2, "clarity": 2, "instructional": 2,
               "payoff": 1, "weakness": "ok", "veto": False,
               "context_ok": False} for i in idxs]
    doc = {"scores": scores, "decision": "KEEP", "winner": 1, "pair": [],
           "explanation": "fine", "images_received": 2}
    doc.update(over)
    return _json.dumps(doc)


def test_qualify_vision_rejects_partial_score_coverage(monkeypatch):
    # One score for a two-image probe: the endpoint ignores the schema and
    # must not qualify (the outdated c86087f-era mock shape).
    assert _qualify_reply(monkeypatch, _two_scores(idxs=(1,))) is False


def test_qualify_vision_rejects_coercible_images_received(monkeypatch):
    assert _qualify_reply(monkeypatch, _two_scores(images_received=2.0)) is False
    assert _qualify_reply(monkeypatch, _two_scores(images_received="2")) is False
    assert _qualify_reply(monkeypatch, _two_scores(images_received=True)) is False


def test_qualify_vision_rejects_no_image(monkeypatch):
    assert _qualify_reply(monkeypatch, _two_scores(
        decision="NO_IMAGE", images_received=0)) is False


def test_qualify_vision_accepts_full_valid_verdict(monkeypatch):
    assert _qualify_reply(monkeypatch, _two_scores()) is True


def test_detect_language_head_failure_returns_none(tmp_path, monkeypatch):
    from yt2wiki.backends import asr_http as _m
    wav = _wav(tmp_path, seconds=60.0)
    monkeypatch.setattr(_m, "_head_wav", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert _m.detect_language("http://s", wav, timeout=5.0) == (None, None)


def test_stage_fingerprint_includes_asr_identity(tmp_path, monkeypatch):
    from yt2wiki.config import Settings
    from yt2wiki.pipeline import StageContext
    from yt2wiki.db.schema import Database

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    ctx = StageContext(job_id="j", source_id="s", settings=settings, db=db,
                       paths=settings.paths, fingerprint="x")
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://one:9000")
    fp1 = ctx.stage_fingerprint("transcribe")
    compose1 = ctx.stage_fingerprint("compose")
    monkeypatch.setenv("YT2WIKI_ASR_URL", "http://two:9000")
    # The transcribe fingerprint must move with the server (or a stale
    # transcript is reused); unrelated stages must stay put.
    assert ctx.stage_fingerprint("transcribe") != fp1
    assert ctx.stage_fingerprint("compose") == compose1


def test_detect_language_cleans_temp_head(tmp_path, monkeypatch):
    from yt2wiki.backends import asr_http as _m
    import httpx as _h
    wav = _wav(tmp_path, seconds=60.0)

    class Resp:
        status_code = 200

        def json(self):
            return {"language_code": "en", "confidence": 0.9}

    def _client(*a, **k):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.post.return_value = Resp()
        return ctx

    monkeypatch.setattr(_h, "Client", _client)
    import tempfile as _tf
    real_mkdtemp = _tf.mkdtemp
    made = []

    def _recording_mkdtemp(*a, **k):
        k.setdefault("prefix", "yt2wiki-asr-head-")
        d = real_mkdtemp(dir=str(tmp_path), *a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(_tf, "mkdtemp", _recording_mkdtemp)
    assert _m.detect_language("http://s", wav, timeout=5.0) == ("en", 0.9)
    # The head must be created inside the sandbox and removed afterwards:
    # no WAV fragments may leak into the real system temp dir.
    assert made and all(not __import__("pathlib").Path(d).exists() for d in made)
