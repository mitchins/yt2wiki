"""HTTP speech recognition backend (maintained Whisper HTTP service).

Default transcription backend. Audio is POSTed to a separately operated
Whisper HTTP service; yt2wiki owns no ASR weights and performs no local
inference on this path.

Selected service: ``ahmetoner/whisper-asr-webservice`` (the community
go-to: 3.3k stars, maintained — v1.10.0, faster-whisper v1.2.1 engine),
qualified against a digest-pinned image (see ``docker-compose.yml``).

Protocol note (explicitly non-OAI): this service does NOT implement the
OpenAI ``POST /v1/audio/transcriptions`` route (verified: 404). It speaks
its own stable contract:

    POST {base}/asr?output=json&task=transcribe&language=<code>
                     &word_timestamps=<bool>&vad_filter=<bool>
    multipart field: ``audio_file`` (the pipeline's normalised 16 kHz WAV)

    -> {"language": <code>,
        "segments": [{"start", "end", "text",
                      "words": [{"start", "end", "word", "probability"}],
                      "avg_logprob", "no_speech_prob", "compression_ratio", ...}],
        "text": <full transcript>}

An OpenAI-compatible server (e.g. speaches-ai/speaches, SYSTRAN-endorsed)
could be attached by adding its ``verbose_json`` shape to :func:`parse`
— the validation below is shape-agnostic. That path is documented but
NOT claimed: registry access blocked ghcr.io from the qualification
machine, so no OAI-protocol server was run here.

What is preserved / recorded:

- timestamped segments and word-level timestamps (required — a response
  without words while ``word_timestamps`` is requested is a hard error,
  never a silent downgrade),
- language, duration (from the WAV header — the service returns none),
  per-segment confidence metadata when the server provides it
  (``avg_logprob`` / ``no_speech_prob`` / ``compression_ratio`` /
  per-word ``probability``; ``None`` when absent — never fabricated),
- model provenance as the operator-declared server model
  (``YT2WIKI_ASR_MODEL``); the ``/asr`` response carries no model id,
- HTTP failures, timeouts and malformed responses as :class:`AsrHttpError`
  naming the server, the status and a body excerpt.

Explicitly unsupported by the ``/asr`` contract (recorded on every job,
never silently emulated): vocabulary hints / initial prompt, beam size,
condition-on-previous-text, decoding temperature, translation task.
"""

from __future__ import annotations

import logging
import math
import os
import time
import wave
from pathlib import Path
from typing import Any

from ..config import ModelSpec
from ..gpu import BackendReport
from .asr import Segment, Transcript, Word
from .base import BaseBackend

log = logging.getLogger("yt2wiki.asr_http")

# Query params this backend drives; everything else is server-side default.
# Options with no equivalent in the /asr contract. The transcribe stage
# records these per job so the gap is visible, not silent.
UNSUPPORTED_OPTIONS = (
    "initial_prompt",  # no vocabulary-hint / prompt param on /asr
    "beam_size",  # server-side decoding default, not settable per request
    "condition_on_previous_text",  # server-side default, not settable
    "temperature",  # no temperature param on /asr
    "translate",  # task=translate exists server-side; yt2wiki never requests it
)

# Bounds tolerance: servers round to ~10 ms; allow a small overrun past the
# probed duration rather than failing a good transcript on rounding.
_END_TOLERANCE_S = 1.0
_WORD_TOLERANCE_S = 0.05


class AsrHttpError(RuntimeError):
    """Transcription over HTTP failed — server identity included."""


# ----------------------------------------------------------------------
# wire protocol
def build_query(*, language: str, word_timestamps: bool, vad_filter: bool) -> str:
    from urllib.parse import quote

    lang = (language or "").strip()
    if not lang:
        raise AsrHttpError("no language for the ASR request: refusing to silently fall back to 'en'")
    return (
        "output=json&task=transcribe"
        f"&language={quote(lang, safe='')}"
        f"&word_timestamps={'true' if word_timestamps else 'false'}"
        f"&vad_filter={'true' if vad_filter else 'false'}"
    )


def wav_duration_s(path: Path) -> float:
    """Duration from the WAV header (stdlib only, no decode)."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                raise AsrHttpError(f"invalid WAV sample rate {rate}: {path}")
            return frames / float(rate)
    except AsrHttpError:
        raise
    except Exception as exc:
        raise AsrHttpError(f"cannot read WAV header {path}: {exc}") from exc


def post_asr(
    base_url: str,
    wav_path: Path,
    *,
    query: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST the normalised audio; return the decoded ``/asr`` JSON object."""
    import httpx

    server = base_url.rstrip("/")
    url = f"{server}/asr?{query}"
    try:
        with open(wav_path, "rb") as fh:
            files = {"audio_file": (wav_path.name, fh, "audio/wav")}
            with httpx.Client(timeout=timeout, headers=headers or {}) as client:
                r = client.post(url, files=files)
    except Exception as exc:  # connect / DNS / timeout / TLS
        raise AsrHttpError(
            f"ASR request to {server} failed ({type(exc).__name__}): {str(exc)[:200]}"
        ) from exc
    if r.status_code != 200:
        raise AsrHttpError(
            f"ASR server {server} returned HTTP {r.status_code}: {r.text[:300]}"
        )
    try:
        data = r.json()
    except Exception as exc:
        raise AsrHttpError(
            f"ASR server {server} returned non-JSON output: {r.text[:300]}"
        ) from exc
    if not isinstance(data, dict):
        raise AsrHttpError(
            f"ASR server {server} returned a JSON {type(data).__name__}, "
            f"expected an object with language/segments/text"
        )
    return data


def detect_language(
    base_url: str,
    wav_path: Path,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, float | None]:
    """Ask ``/detect-language`` (first 30 s) for (code, confidence).

    Only the head of the audio is uploaded: the endpoint processes the
    first 30 s anyway, so sending a 900 s file wastes the upload.
    Returns ``(None, None)`` when the endpoint is unavailable — the caller
    falls back to the ``/asr`` language with unknown confidence rather than
    inventing a number.
    """
    import httpx

    server = base_url.rstrip("/")
    head = Path(wav_path)
    made_temp = False
    try:
        head = _head_wav(head, seconds=35.0)
        made_temp = head != Path(wav_path)
        with open(head, "rb") as fh:
            files = {"audio_file": (head.name, fh, "audio/wav")}
            with httpx.Client(timeout=timeout, headers=headers or {}) as client:
                r = client.post(f"{server}/detect-language", files=files)
        if r.status_code != 200:
            return None, None
        data = r.json()
    except Exception:
        return None, None
    finally:
        if made_temp:
            try:
                head.unlink()
                head.parent.rmdir()
            except OSError:  # noqa: BLE001 - best-effort probe cleanup
                pass
    if not isinstance(data, dict):
        return None, None
    code = data.get("language_code")
    conf = data.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None
    if not isinstance(code, str) or not code.strip():
        return None, conf_f
    return code.strip(), conf_f


# ----------------------------------------------------------------------
# response -> Transcript
def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_words(i: int, raw_words: Any, problems: list[str]) -> list[Word]:
    """Parse one segment's word list, recording structural problems."""
    words: list[Word] = []
    if not isinstance(raw_words, list):
        problems.append(f"segment {i}: 'words' is not a list")
        raw_words = []
    for j, rw in enumerate(raw_words):
        if not isinstance(rw, dict):
            problems.append(f"segment {i} word {j}: not an object")
            continue
        ws, we = _opt_float(rw.get("start")), _opt_float(rw.get("end"))
        wtext = rw.get("word") if isinstance(rw.get("word"), str) else ""
        if ws is None or we is None or not wtext:
            problems.append(f"segment {i} word {j}: bad timestamp/text")
            continue
        words.append(Word(start=ws, end=we, word=wtext,
                          probability=_opt_float(rw.get("probability"))))
    return words


def _parse_segment(i: int, raw: Any, problems: list[str], *,
                   require_words: bool, server: str) -> Segment | None:
    """Parse one /asr segment; None when the segment is unusable."""
    if not isinstance(raw, dict):
        problems.append(f"segment {i}: not an object")
        return None
    start = _opt_float(raw.get("start"))
    end = _opt_float(raw.get("end"))
    text = raw.get("text") if isinstance(raw.get("text"), str) else ""
    if start is None or end is None:
        problems.append(f"segment {i}: non-numeric timestamps")
        return None
    words = _parse_words(i, raw.get("words") or [], problems)
    if require_words and not words:
        problems.append(
            f"segment {i} [{start:.2f}-{end:.2f}]: no word timestamps "
            f"(server {server} is text-only for this span)"
        )
    return Segment(
        start=start, end=end, text=text.strip(), words=words,
        avg_logprob=_opt_float(raw.get("avg_logprob")),
        no_speech_prob=_opt_float(raw.get("no_speech_prob")),
        compression_ratio=_opt_float(raw.get("compression_ratio")),
    )


def parse(
    data: dict[str, Any],
    *,
    duration_s: float,
    require_words: bool,
    server: str,
) -> Transcript:
    """Convert an ``/asr`` JSON object into a :class:`Transcript`.

    Raises :class:`AsrHttpError` listing every structural problem found
    (no fail-fast: the operator sees the whole shape mismatch at once).
    Absent confidence metadata becomes ``None`` (stored as SQL NULL),
    never a fabricated zero.
    """
    problems: list[str] = []
    language = data.get("language")
    if not isinstance(language, str) or not language.strip():
        problems.append("missing 'language'")
        language = "en"
    raw_segs = data.get("segments")
    if not isinstance(raw_segs, list) or not raw_segs:
        problems.append("missing or empty 'segments'")
        raw_segs = []
    segments: list[Segment] = []
    for i, raw in enumerate(raw_segs):
        seg = _parse_segment(i, raw, problems, require_words=require_words,
                             server=server)
        if seg is not None:
            segments.append(seg)
    segments.sort(key=lambda s: s.start)
    transcript = Transcript(
        language=language.strip(), language_probability=None,
        duration_s=duration_s, segments=segments,
    )
    v_problems, warnings = validate(transcript, duration_s, require_words=require_words)
    problems.extend(v_problems)
    transcript.warnings = warnings
    if problems:
        raise AsrHttpError(
            f"ASR server {server} returned an unusable transcript "
            f"({len(problems)} problems): " + "; ".join(problems[:12])
        )
    return transcript


def _validate_words(i: int, s: Segment, problems: list[str]) -> int:
    """Per-word shape checks for one segment; returns degenerate-word count."""
    degenerate = 0
    prev_w_end = s.start
    for j, w in enumerate(s.words):
        if not (math.isfinite(w.start) and math.isfinite(w.end)):
            problems.append(f"segment {i} word {j}: non-finite timestamps")
            continue
        if not (w.start <= w.end):
            problems.append(f"segment {i} word {j}: bad span")
        elif w.start == w.end:
            degenerate += 1
        if w.start < s.start - _WORD_TOLERANCE_S or w.end > s.end + _WORD_TOLERANCE_S:
            problems.append(
                f"segment {i} word {j} [{w.start:.2f}-{w.end:.2f}] "
                f"outside segment [{s.start:.2f}-{s.end:.2f}]")
        if w.start < prev_w_end - _WORD_TOLERANCE_S:
            problems.append(f"segment {i} word {j}: out of order")
        prev_w_end = max(prev_w_end, w.start)
    return degenerate


def _validate_segment(i: int, s: Segment, duration_s: float,
                      problems: list[str], prev_start: float) -> float:
    """Per-segment shape checks; returns the updated prev_start marker."""
    if not (math.isfinite(s.start) and math.isfinite(s.end)):
        problems.append(f"segment {i}: non-finite timestamps")
        return prev_start
    if not (0 <= s.start < s.end):
        problems.append(f"segment {i}: bad span [{s.start}, {s.end}]")
    if s.end > duration_s + _END_TOLERANCE_S:
        problems.append(
            f"segment {i}: end {s.end:.2f} exceeds duration {duration_s:.2f}")
    if s.start < prev_start - 0.01:
        problems.append(f"segment {i}: starts before previous segment")
    if not s.text.strip():
        problems.append(f"segment {i}: empty text")
    return max(prev_start, s.start)


def validate(t: Transcript, duration_s: float, *,
             require_words: bool) -> tuple[list[str], list[str]]:
    """Bidirectional shape checks.

    Returns ``(problems, warnings)``. Problems are structural (a corrupt or
    lying server) and fail the transcription. Warnings are degenerate but
    usable data: zero-duration words (``start == end``), which this server
    emits for trailing words it could not align — typically at an audio
    cut. They are kept for their lexical content and counted in job
    metrics, never treated as precise timings.
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not math.isfinite(duration_s) or duration_s <= 0:
        problems.append(f"bad duration {duration_s!r}")
    if not t.language.strip():
        problems.append("empty language")
    if not t.segments:
        problems.append("no segments in transcript")
    degenerate = 0
    degenerate_segs = 0
    prev_start = -math.inf
    for i, s in enumerate(t.segments):
        prev_start = _validate_segment(i, s, duration_s, problems, prev_start)
        seg_degenerate = _validate_words(i, s, problems)
        if seg_degenerate:
            degenerate += seg_degenerate
            degenerate_segs += 1
    if degenerate:
        warnings.append(
            f"{degenerate} zero-duration words in {degenerate_segs} segment(s): "
            f"server could not align them; kept for text, not precise timings")
    return problems, warnings


# ----------------------------------------------------------------------
# backend (ModelManager-compatible)
class HttpAsrBackend(BaseBackend):
    """Transcription over HTTP; no local weights, no GPU use.

    ``transcribe`` accepts the same keyword arguments as
    :class:`WhisperBackend` so the transcribe stage is backend-agnostic.
    Options outside the ``/asr`` contract are listed on
    :attr:`last_unsupported` for the stage to record — never emulated.
    """

    def __init__(
        self,
        spec: ModelSpec,
        variant: dict[str, Any],
        device: str,
        index: int | None,
        *,
        base_url: str,
        model: str = "",
        api_key_env: str = "",
        timeout: float = 300.0,
    ):
        super().__init__(spec, variant, device, index)
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise AsrHttpError(
                "no ASR server configured: assign the transcription role to a "
                "provider (Settings → Inference) or set YT2WIKI_ASR_URL")
        self.server_model = model
        self.timeout = float(timeout)
        self._headers: dict[str, str] = {}
        if api_key_env:
            key = os.environ.get(api_key_env, "")
            if key:
                self._headers["Authorization"] = f"Bearer {key}"
        self.last_unsupported: list[str] = []
        self.last_warnings: list[str] = []

    @property
    def server(self) -> str:
        return f"{self.base_url} (model {self.server_model or 'server-default'})"

    def load(self) -> BackendReport:
        # No local load and no weights to verify; reachability and the real
        # word-timestamp contract are proven per transcription (and by the
        # operator console's test probe), mirroring RemoteOpenAIBackend.
        self.model = object()  # marker for loaded
        report = self._finish_load(
            time.time(),
            extra={"remote_url": self.base_url, "remote_model": self.server_model},
        )
        log.info("http-asr ready at %s", self.server)
        return report

    def unload(self) -> None:
        self.model = None

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str = "en",
        vad_filter: bool = True,
        beam_size: int = 5,  # noqa: ARG002 - accepted for stage compatibility, server-side
        condition_on_previous_text: bool = True,  # noqa: ARG002 - same
        initial_prompt: str | None = None,
        word_timestamps: bool = True,
        on_progress=None,
    ) -> Transcript:
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        unsupported = [o for o in UNSUPPORTED_OPTIONS if o != "translate"]
        if not initial_prompt:
            unsupported = [o for o in unsupported if o != "initial_prompt"]
        self.last_unsupported = unsupported
        self.last_warnings = []
        duration_s = wav_duration_s(Path(wav_path))
        # Long audio needs a long server budget: at least the configured
        # timeout, at least ~5x the measured HTTP RTF (~0.2) as headroom.
        timeout = max(self.timeout, duration_s * 1.0)
        query = build_query(language=language,
                            word_timestamps=word_timestamps,
                            vad_filter=vad_filter)
        data = post_asr(self.base_url, Path(wav_path), query=query,
                        timeout=timeout, headers=self._headers)
        transcript = parse(data, duration_s=duration_s,
                           require_words=word_timestamps, server=self.server)
        self.last_warnings = list(transcript.warnings)
        # Genuine language confidence from /detect-language (first 30 s);
        # None when the endpoint is unavailable — never invented.
        _code, confidence = detect_language(
            self.base_url, Path(wav_path), timeout=min(60.0, self.timeout),
            headers=self._headers)
        if confidence is not None:
            transcript.language_probability = confidence
        if on_progress is not None:
            on_progress(len(transcript.segments), duration_s, duration_s)
        log.info("http-asr transcribed %.1fs into %d segments / %d words via %s",
                 duration_s, len(transcript.segments),
                 sum(len(s.words) for s in transcript.segments), self.server)
        return transcript


def resolve_env_variant(variant: dict[str, Any]) -> dict[str, Any]:
    """Fill an ``asr-http`` registry variant from the environment.

    Registry default path (no inference assignment): the operator points
    yt2wiki at their server with ``YT2WIKI_ASR_URL`` and declares the
    server-side model with ``YT2WIKI_ASR_MODEL`` (informational identity —
    ``/asr`` echoes no model id, so the declaration is recorded, not probed).
    """
    out = dict(variant)
    out["url"] = os.environ.get(str(variant.get("url_env") or "YT2WIKI_ASR_URL"), "")
    out["model"] = os.environ.get(str(variant.get("model_env") or "YT2WIKI_ASR_MODEL"), "")
    timeout_raw = os.environ.get("YT2WIKI_ASR_TIMEOUT", "")
    try:
        out["timeout"] = float(timeout_raw) if timeout_raw else float(variant.get("timeout", 300.0))
    except ValueError:
        out["timeout"] = float(variant.get("timeout", 300.0))
    return out


def _head_wav(path: Path, *, seconds: float = 35.0) -> Path:
    """Copy the first `seconds` of a 16 kHz mono WAV (stdlib only).

    Used for language detection: the endpoint only reads the head anyway.
    Returns `path` itself when the file is already short (no temp copy).
    """
    import tempfile

    with wave.open(str(path), "rb") as wf:
        rate, nch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        if wf.getnframes() <= int(seconds * rate):
            return path
        head_frames = wf.readframes(int(seconds * rate))
    tmp = Path(tempfile.mkdtemp(prefix="yt2wiki-asr-head-")) / "head.wav"
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(sw)
        wf.setframerate(rate)
        wf.writeframes(head_frames)
    return tmp


def write_sine_wav(path: Path, *, seconds: float = 2.0, freq: float = 440.0) -> Path:
    """Write a mono 16 kHz sine WAV (protocol probes; stdlib only)."""
    import array
    import math as _math

    rate = 16000
    n = max(1, int(seconds * rate))
    samples = array.array(
        "h", (int(12000 * _math.sin(2 * _math.pi * freq * i / rate)) for i in range(n)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return path
