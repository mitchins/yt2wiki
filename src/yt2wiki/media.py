"""Media primitives built on ffmpeg/ffprobe.

Everything here is *streaming*: frame extraction is per-timestamp or per-range,
never "decode the whole video into RAM".
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Sequence

log = logging.getLogger("yt2wiki.media")

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG = FFMPEG_BIN
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class MediaError(RuntimeError):
    pass


@dataclass
class Probe:
    duration_s: float
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    has_video: bool
    has_audio: bool
    nb_frames: int | None
    format: str | None
    timebase: Fraction | None
    raw: dict

    @property
    def is_video(self) -> bool:
        return self.has_video and self.width is not None


def _run(cmd: Sequence[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc


def ffprobe(path: Path) -> Probe:
    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = _run(cmd, timeout=300)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {proc.stderr[-800:]}")
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    timebase = None
    if video:
        tb = video.get("time_base")
        if tb and "/" in tb:
            try:
                timebase = Fraction(tb)
            except Exception:
                timebase = None
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
        try:
            f = Fraction(rate)
            fps = float(f) if f > 0 else None
        except Exception:
            fps = None
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if not duration and video:
        duration = float(video.get("duration") or 0.0)
    return Probe(
        duration_s=duration,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        fps=fps,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        has_video=video is not None,
        has_audio=audio is not None,
        nb_frames=int(video["nb_frames"]) if video and video.get("nb_frames") else None,
        format=data.get("format", {}).get("format_name"),
        timebase=timebase,
        raw=data,
    )


# ----------------------------------------------------------------------
# normalisation


def extract_audio(src: Path, dst: Path, *, sample_rate: int = 16000) -> Path:
    """16 kHz mono PCM16 WAV.  Works for audio-only sources too."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        "-af", "aresample=resampler=swr:precision=28",
        str(dst),
    ]
    proc = _run(cmd, timeout=1800)
    if proc.returncode != 0:
        raise MediaError(f"audio extraction failed: {proc.stderr[-800:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaError("audio extraction produced an empty file")
    return dst


def normalise_video(src: Path, dst: Path, *, width: int = 1280, crf: int = 30) -> Path:
    """Small, cheap-to-decode proxy used for frame extraction and scrubbing."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        "-vf", f"scale='min({width},iw)':-2:flags=fast_bilinear",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-g", "48",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        str(dst),
    ]
    proc = _run(cmd, timeout=3600)
    if proc.returncode != 0:
        raise MediaError(f"video normalisation failed: {proc.stderr[-800:]}")
    return dst


# ----------------------------------------------------------------------
# frames


_SCENE_LINE = re.compile(r"pts_time:([0-9.]+)")


def scene_change_times(
    src: Path, *, threshold: float = 0.3, min_gap_s: float = 0.0
) -> list[float]:
    """Timestamps of hard cuts detected by ffmpeg's scene score."""
    vf = f"select='gt(scene,{threshold})',showinfo"
    cmd = [FFMPEG, "-nostdin", "-v", "info", "-y", "-i", str(src), "-vf", vf, "-f", "null", "-"]
    proc = _run(cmd, timeout=3600)
    if proc.returncode != 0:
        raise MediaError(f"scene detection failed: {proc.stderr[-800:]}")
    times: list[float] = []
    for line in proc.stderr.splitlines():
        m = _SCENE_LINE.search(line)
        if m:
            try:
                times.append(float(m.group(1)))
            except ValueError:
                continue
    if min_gap_s > 0:
        times = _thin(times, min_gap_s)
    return times


def _thin(times: Sequence[float], min_gap_s: float) -> list[float]:
    out: list[float] = []
    for t in sorted(times):
        if not out or (t - out[-1]) >= min_gap_s:
            out.append(t)
    return out


def extract_frame(src: Path, t: float, dst: Path, *, quality: int = 2) -> Path:
    """Grab one frame at (approximately) time t using a fast seek."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    t = max(0.0, float(t))
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-ss", f"{t:.3f}",
        "-i", str(src),
        "-frames:v", "1", "-q:v", str(int(quality)),
        "-vf", "scale='min(1280,iw)':-2:flags=bilinear",
        "-f", "image2",
        str(dst),
    ]
    proc = _run(cmd, timeout=120)
    if proc.returncode != 0:
        raise MediaError(f"frame extraction failed at t={t:.3f}: {proc.stderr[-400:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaError(f"no frame produced at t={t:.3f}")
    return dst


def extract_frames_batch(
    src: Path, times: Sequence[float], dst_dir: Path, *, quality: int = 2
) -> list[Path | None]:
    """Extract frames with one ffmpeg call per timestamp, in bounded chunks.

    Frames are written as ``{index:05d}.jpg`` inside ``dst_dir`` so the caller
    does not have to guess timestamps from filenames.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path | None] = []
    for i, t in enumerate(times):
        dst = dst_dir / f"{i:05d}.jpg"
        try:
            extract_frame(src, float(t), dst, quality=quality)
            results.append(dst)
        except MediaError as exc:
            log.warning("frame %s skipped: %s", t, exc)
            results.append(None)
    return results


def sample_times(duration_s: float, interval_s: float, *, offset: float = 0.0) -> Iterator[float]:
    if duration_s <= 0 or interval_s <= 0:
        return
    n = int(duration_s / interval_s)
    for i in range(n + 1):
        t = offset + i * interval_s
        if t < duration_s:
            yield t


def duration_simple(path: Path) -> float:
    return ffprobe(path).duration_s
