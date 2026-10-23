"""Speech recognition backend (faster-whisper / CTranslate2)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import wave

import numpy as np

from ..config import ModelSpec
from ..gpu import BackendReport
from .base import BaseBackend

log = logging.getLogger("yt2wiki.asr")


@dataclass
class Word:
    start: float
    end: float
    word: str
    # None when the server reports no per-word confidence; SQL NULL, not zero.
    probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "word": self.word,
            "probability": round(self.probability, 4) if self.probability is not None else None,
        }


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    # Server-reported confidence metadata; None when absent (SQL NULL).
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        def _r(v: float | None) -> float | None:
            return round(v, 4) if v is not None else None

        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "avg_logprob": _r(self.avg_logprob),
            "no_speech_prob": _r(self.no_speech_prob),
            "compression_ratio": _r(self.compression_ratio),
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class Transcript:
    language: str
    # None when the server reports a language without confidence (e.g. the
    # /asr contract); stored as SQL NULL, never a fabricated number.
    language_probability: float | None
    duration_s: float
    segments: list[Segment] = field(default_factory=list)
    # Non-fatal data notes from parsing (e.g. zero-duration words the
    # server emitted at an audio boundary); surfaced in job metrics.
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(s.text.strip() for s in self.segments if s.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "language_probability": (
                round(self.language_probability, 4)
                if self.language_probability is not None else None
            ),
            "duration_s": round(self.duration_s, 3),
            "segments": [s.to_dict() for s in self.segments],
        }

    def iter_words(self) -> Iterator[Word]:
        for seg in self.segments:
            yield from seg.words

    def save_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for seg in self.segments:
                fh.write(json.dumps(seg.to_dict(), ensure_ascii=False) + "\n")

    @classmethod
    def load_jsonl(cls, path: Path, *, language: str = "en", duration_s: float = 0.0) -> "Transcript":
        segs: list[Segment] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                segs.append(
                    Segment(
                        start=d["start"],
                        end=d["end"],
                        text=d["text"],
                        words=[Word(**w) for w in d.get("words", [])],
                        avg_logprob=d.get("avg_logprob", 0.0),
                        no_speech_prob=d.get("no_speech_prob", 0.0),
                        compression_ratio=d.get("compression_ratio", 0.0),
                    )
                )
        return cls(language=language, language_probability=1.0, duration_s=duration_s, segments=segs)


class WhisperBackend(BaseBackend):
    """CTranslate2 Whisper with word-level timestamps.

    Audio is decoded lazily from a 16 kHz mono WAV by faster-whisper itself, so
    peak RAM stays proportional to the segment buffer, not the file size.
    """

    def __init__(
        self,
        spec: ModelSpec,
        variant: dict[str, Any],
        device: str,
        index: int | None,
        hf_home: Path | None = None,
    ):
        super().__init__(spec, variant, device, index)
        self.hf_home = hf_home

    def load(self) -> BackendReport:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "local ASR needs the optional 'asr-local' extra "
                "(pip install yt2wiki[asr-local]); or assign the transcription "
                "role to an HTTP provider instead") from exc

        started = time.time()
        os.environ.setdefault("HF_HOME", str(self.hf_home or Path.home() / ".cache" / "huggingface"))
        compute_type = self.variant.get("compute_type", "int8_float16")
        if self.device == "cpu" and compute_type in ("int8_float16", "float16"):
            # CTranslate2 has no efficient float16 kernels on CPU, so the GPU
            # default is unusable there. Use the registry's explicit CPU compute
            # type instead of silently degrading; the GPU default is preserved.
            compute_type = self.variant.get("cpu_compute_type", "int8")
            log.info("asr %s on cpu: compute_type %s (gpu default %s overridden)",
                     self.spec.key, compute_type, self.variant.get("compute_type"))
        self.variant["compute_type"] = compute_type
        kwargs: dict[str, Any] = {
            "device": self.device,
            "compute_type": compute_type,
            "download_root": str(self.hf_home) if self.hf_home else None,
            "local_files_only": False,
        }
        if device_index := (self.index if self.device == "cuda" else None):
            kwargs["device_index"] = device_index
        self.model = WhisperModel(self.spec.repo, **kwargs)
        report = self._finish_load(started)
        log.info(
            "asr %s loaded on %s in %.1fs (peak %.2f GB)",
            self.spec.key, self.device, report.load_seconds, report.peak_vram_gb,
        )
        return report

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str = "en",
        vad_filter: bool = True,
        beam_size: int = 5,
        condition_on_previous_text: bool = True,
        initial_prompt: str | None = None,
        on_progress=None,
    ) -> Transcript:
        if not self.loaded:
            raise RuntimeError("backend not loaded")
        assert self.model is not None
        kwargs: dict[str, Any] = {
            "language": language,
            "word_timestamps": True,
            "vad_filter": vad_filter,
            "beam_size": self.variant.get("beam_size", beam_size),
            "condition_on_previous_text": condition_on_previous_text,
        }
        if initial_prompt and initial_prompt.strip():
            kwargs["initial_prompt"] = initial_prompt[-800:]
        # The pipeline already normalises audio to 16 kHz mono PCM16 with ffmpeg,
        # so decode the PCM directly.  This keeps peak RAM proportional to one
        # audio track and sidesteps a PyAV/faster-whisper signature mismatch.
        audio = load_wav_mono16(wav_path)
        segments, info = self.model.transcribe(audio, **kwargs)
        out: list[Segment] = []
        count = 0
        for seg in segments:
            words = [
                Word(start=w.start, end=w.end, word=w.word, probability=w.probability)
                for w in (seg.words or [])
            ]
            out.append(
                Segment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                    words=words,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                    compression_ratio=seg.compression_ratio,
                )
            )
            count += 1
            if on_progress is not None:
                on_progress(count, seg.end, info.duration)
        return Transcript(
            language=info.language,
            language_probability=info.language_probability,
            duration_s=info.duration,
            segments=out,
        )


def load_vocab_biases(words: Sequence[str] | None) -> dict[str, float] | None:
    """Optional initial-prompt vocabulary bias is applied by callers."""
    if not words:
        return None
    return {w: 1.0 for w in words}


def align_words_to_segments(segments: Sequence[Segment]) -> list[Word]:
    words: list[Word] = []
    for seg in segments:
        words.extend(seg.words)
    return words


def load_wav_mono16(path: Path) -> "np.ndarray":
    """Read a 16 kHz mono PCM16 WAV produced by :func:`yt2wiki.media.extract_audio`."""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != 16000:
            raise ValueError(
                f"expected a 16 kHz WAV for ASR, got {wf.getframerate()} Hz: {path}"
            )
        if wf.getnchannels() == 2:
            raise ValueError(f"expected mono audio for ASR: {path}")
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return samples / 32768.0
