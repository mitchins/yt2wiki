"""Cheap image utilities used for candidate generation and deduplication.

Nothing here loads a full video: signatures come from a single low-resolution
raw-video pass streamed from ffmpeg.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from .media import FFMPEG, FFMPEG_BIN, MediaError

THUMB_W, THUMB_H = 32, 18
HASH_W, HASH_H = 9, 8


@dataclass
class FrameSignature:
    t: float
    mean: float
    std: float
    dhash: int

    @property
    def blank(self) -> bool:
        return self.std < 4.0


def stream_signatures(
    src: Path,
    *,
    fps: float = 1.0,
    duration_hint: float | None = None,
    on_progress=None,
) -> Iterator[FrameSignature]:
    """Yield a tiny thumbnail per second, decoded in one streaming pass."""
    w, h = THUMB_W, THUMB_H
    cmd = [
        FFMPEG, "-nostdin", "-v", "error",
        "-i", str(src),
        "-vf", f"fps={fps},scale={w}:{h}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    nbytes = w * h
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(nbytes)
            if not buf or len(buf) < nbytes:
                break
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w).astype(np.float32)
            yield FrameSignature(
                t=idx / fps,
                mean=float(arr.mean()),
                std=float(arr.std()),
                dhash=_dhash(arr),
            )
            idx += 1
            if on_progress is not None and idx % 30 == 0:
                on_progress(idx / fps)
    finally:
        try:
            proc.stdout.close()
        except Exception:  # pragma: no cover
            pass
        proc.wait(timeout=60)
        if proc.returncode not in (0, None):
            err = proc.stderr.read().decode(errors="replace")[-400:] if proc.stderr else ""
            raise MediaError(f"signature stream failed: {err}")


def _dhash(gray32: "np.ndarray") -> int:
    small = _resize_gray(gray32, HASH_H, HASH_W - 1)
    diff = small[:, 1:] > small[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def hamming(a: int, b: int) -> int:
    x = (a ^ b) & ((1 << (HASH_W * HASH_H)) - 1)
    return int(bin(x).count("1"))


def _resize_gray(arr: "np.ndarray", out_h: int, out_w: int) -> "np.ndarray":
    """Area-average resize without OpenCV (avoids a hard dependency here)."""
    h, w = arr.shape
    ys = np.linspace(0, h, out_h + 1).astype(int)
    xs = np.linspace(0, w, out_w + 1).astype(int)
    out = np.empty((out_h, out_w), dtype=np.float32)
    for i in range(out_h):
        for j in range(out_w):
            block = arr[ys[i]:max(ys[i] + 1, ys[i + 1]), xs[j]:max(xs[j] + 1, xs[j + 1])]
            out[i, j] = block.mean() if block.size else 0.0
    return out


def dedupe_by_signature(
    times: Sequence[float],
    signatures: Sequence[FrameSignature],
    *,
    max_hamming: int = 6,
    sample_fps: float = 1.0,
) -> list[float]:
    """Keep the first of each visual-neighbourhood run of timestamps."""
    if not times:
        return []
    kept: list[float] = []
    kept_hashes: list[int] = []
    for t in sorted(times):
        idx = min(int(t * sample_fps), len(signatures) - 1) if signatures else None
        if idx is None or not signatures:
            kept.append(t)
            continue
        dh = signatures[idx].dhash
        if any(hamming(dh, k) <= max_hamming for k in kept_hashes):
            continue
        kept.append(t)
        kept_hashes.append(dh)
    return kept


def screen_change_scores(
    signatures: Sequence[FrameSignature],
) -> list[tuple[float, float]]:
    """Frame-to-frame visual deltas: catches UI changes without a hard cut."""
    out: list[tuple[float, float]] = []
    for i in range(1, len(signatures)):
        prev, cur = signatures[i - 1], signatures[i]
        delta = hamming(prev.dhash, cur.dhash) / (HASH_W * HASH_H)
        delta += abs(prev.mean - cur.mean) / 255.0
        out.append((cur.t, delta))
    return out
