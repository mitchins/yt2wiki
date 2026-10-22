"""Video acquisition via yt-dlp with robust error handling.

Nothing downloaded here is ever executed; captions and descriptions are treated
as untrusted text and stored as data only.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("yt2wiki.acquire")

_SUBTITLE_TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"
)


class AcquisitionError(RuntimeError):
    pass


@dataclass
class Caption:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


@dataclass
class SubtitleTrack:
    lang: str
    auto: bool
    captions: list[Caption] = field(default_factory=list)
    ext: str = "srt"

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.captions if c.text.strip())


@dataclass
class VideoInfo:
    video_id: str
    title: str
    uploader: str
    duration_s: float
    description: str
    webpage_url: str
    license: str | None = None
    upload_date: str | None = None
    view_count: int | None = None
    chapters: list[dict[str, Any]] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": "youtube",
            "video_id": self.video_id,
            "webpage_url": self.webpage_url,
            "title": self.title,
            "uploader": self.uploader,
            "license": self.license,
            "upload_date": self.upload_date,
            "retrieved_at": time.time(),
        }


def _yt_dlp_bin() -> str:
    from importlib import import_module

    try:
        import_module("yt_dlp")
        import sys

        return _module_yt_dlp_path()
    except ImportError:  # pragma: no cover
        raise AcquisitionError("yt-dlp is not installed")


def _module_yt_dlp_path() -> str:  # pragma: no cover - trivial
    import shutil as _sh
    import sys

    # The interpreter's own bin directory first: a service started via
    # .venv/bin/yt2wiki does not necessarily have the venv on PATH.
    from pathlib import Path as _P

    local = _P(sys.executable).parent / "yt-dlp"
    if local.is_file():
        return str(local)
    for name in ("yt-dlp", "yt_dlp"):
        found = _sh.which(name)
        if found:
            return found
    return "yt-dlp"


def _js_runtime_args() -> list[str]:
    """yt-dlp now requires a JS runtime for YouTube signature solving."""
    for runtime in ("node", "nodejs", "deno", "bun", "quickjs"):
        if shutil.which(runtime):
            return ["--js-runtimes", runtime]
    return []


def _base_args() -> list[str]:
    return [
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "4",
        "--fragment-retries", "4",
        "--retry-sleep", "http:exp=1:20",
        "--no-part",
        "--newline",
    ] + _js_runtime_args()


FORMAT = (
    "bv*[height<=1080][protocol^=http]+ba[protocol^=http]/"
    "b[height<=1080][protocol^=http]/"
    "bv*[height<=1080]+ba[acodec!=unknown]/"
    "b[height<=1080]/b"
)


def _run_yt_dlp(args: list[str], url: str, timeout_s: int) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AcquisitionError(f"download timed out after {timeout_s}s: {url}") from exc
    except OSError as exc:
        raise AcquisitionError(f"could not launch yt-dlp: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1200:]
        raise AcquisitionError(f"yt-dlp failed ({proc.returncode}): {tail}")


def download_video(
    url: str,
    out_dir: Path,
    *,
    max_height: int = 1080,
    want_subs: bool = True,
    sub_langs: str = "en.*,en",
    progress: Callable[[str], None] | None = None,
    timeout_s: int = 3600,
) -> tuple[Path, VideoInfo]:
    """Download a video plus (when available) its subtitles.

    Returns the merged media path and parsed metadata.  Raises AcquisitionError
    for unavailable videos, geo-blocks, private/deleted content and timeouts.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "download"
    tmp.mkdir(parents=True, exist_ok=True)
    fmt = FORMAT.replace("1080", str(max_height))

    args = [
        _yt_dlp_bin(), *_base_args(),
        "--format", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--concurrent-fragments", "4",
        "--output", str(tmp / "%(id)s.%(ext)s"),
        "--no-progress",
        url,
    ]

    if progress:
        progress(f"downloading {url}")
    _run_yt_dlp(args, url, timeout_s)

    if want_subs:
        # Subtitles are best-effort: a 429 or missing-caption failure must not
        # fail the acquisition, because local ASR transcribes anyway.
        sub_args = [
            _yt_dlp_bin(), *_base_args(),
            "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", sub_langs,
            "--convert-subs", "srt",
            "--output", str(tmp / "%(id)s.%(ext)s"),
            "--no-progress",
            url,
        ]
        try:
            _run_yt_dlp(sub_args, url, min(timeout_s, 600))
        except AcquisitionError as exc:
            log.warning("subtitle download failed, continuing without captions: %s", exc)

    media = _pick_media(tmp)
    if media is None:
        raise AcquisitionError("yt-dlp reported success but produced no media file")

    info_json = next(iter(sorted(tmp.glob("*.info.json"))), None)
    info = json.loads(info_json.read_text(encoding="utf-8")) if info_json else {}

    final = out_dir / "source.mp4"
    shutil.move(str(media), str(final))
    subs = _parse_subtitles(tmp, info)
    return final, _build_info(info, url, subs)


def _pick_media(tmp: Path) -> Path | None:
    exts = (".mp4", ".mkv", ".webm", ".m4v", ".mov")
    candidates = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not candidates:
        candidates = [p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() != ".info.json"
                      and not p.name.endswith(".srt")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


_SUB_SUFFIX = re.compile(r"\.(?P<lang>[A-Za-z0-9-]+)\.s?rt$")


def _parse_subtitles(tmp: Path, info: dict[str, Any]) -> list[SubtitleTrack]:
    """Classify downloaded subtitle files as manual vs. automatic.

    yt-dlp appends ``-orig`` to the manual track when a same-language automatic
    track was also written, and reports both tracks in ``subtitles`` /
    ``automatic_captions``.  The combination of the two signals is reliable.
    """
    manual_langs = set((info.get("subtitles") or {}).keys())
    auto_langs = set((info.get("automatic_captions") or {}).keys())
    tracks: list[SubtitleTrack] = []
    for sub in sorted(tmp.glob("*.srt")) + sorted(tmp.glob("*.vtt")):
        ext = sub.suffix.lower().lstrip(".")
        m = _SUB_SUFFIX.search(sub.name)
        if not m:
            continue
        lang = m.group("lang")
        manual = False
        if lang.endswith("-orig"):
            manual, lang = True, lang[: -len("-orig")]
        elif lang in manual_langs and lang not in auto_langs:
            manual = True
        try:
            text = sub.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        caps = parse_srt(text)
        if caps:
            tracks.append(
                SubtitleTrack(lang=lang, auto=not manual, captions=caps, ext=ext)
            )
    return tracks


def _lang_of(name: str) -> str:
    stem = Path(name).stem
    parts = stem.split(".")
    return parts[-1] if len(parts) > 1 else "en"


def parse_srt(text: str) -> list[Caption]:
    """Parse SRT/VTT into flat captions with tags and blank lines removed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[Caption] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        m = None
        for ln in lines:
            m = _SUBTITLE_TS.search(ln)
            if m:
                break
        if not m:
            continue
        start = _ts_to_s(m, 1)
        end = _ts_to_s(m, 5)
        body = " ".join(ln for ln in lines if not _SUBTITLE_TS.search(ln))
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\s{2,}", " ", body).strip()
        if body and body.lower() not in ("[music]", "[applause]"):
            out.append(Caption(start=start, end=end, text=body))
    return _merge_wrapped(out)


def _merge_wrapped(caps: list[Caption], max_gap: float = 1.2) -> list[Caption]:
    """YouTube auto-captions are heavily wrapped; rebuild sentence-ish chunks."""
    merged: list[Caption] = []
    buf: list[str] = []
    start = end = 0.0
    for c in caps:
        if not buf:
            start = c.start
        buf.append(c.text)
        end = c.end
        if len(" ".join(buf)) >= 160 or c.text.rstrip().endswith((".", "!", "?", ":")):
            merged.append(Caption(start=start, end=end, text=" ".join(buf)))
            buf = []
    if buf:
        merged.append(Caption(start=start, end=end, text=" ".join(buf)))
    return merged


def _best_track(tracks: list[SubtitleTrack]) -> SubtitleTrack:
    """Prefer a human-written track, then coverage, then the plainest name."""
    return max(
        tracks,
        key=lambda t: (not t.auto, len(t.lang) <= 2, len(t.captions)),
    )


def _ts_to_s(m: "re.Match[str]", base: int) -> float:
    hours = m.group(base)
    minutes = int(m.group(base + 1))
    seconds = int(m.group(base + 2))
    frac = m.group(base + 3)
    millis = int((frac or "0").ljust(3, "0")[:3])
    return (int(hours) * 3600 if hours else 0) + minutes * 60 + seconds + millis / 1000.0


def _build_info(info: dict[str, Any], url: str, subs: list[SubtitleTrack]) -> VideoInfo:
    chapters = []
    for ch in info.get("chapters") or []:
        chapters.append(
            {
                "title": ch.get("title"),
                "start_time": ch.get("start_time"),
                "end_time": ch.get("end_time"),
            }
        )
    return VideoInfo(
        video_id=str(info.get("id") or _id_from_url(url)),
        title=info.get("title") or "Untitled source",
        uploader=info.get("uploader") or info.get("channel") or "unknown",
        duration_s=float(info.get("duration") or 0.0),
        description=info.get("description") or "",
        webpage_url=info.get("webpage_url") or url,
        license=info.get("license"),
        upload_date=info.get("upload_date"),
        view_count=info.get("view_count"),
        chapters=chapters,
        subtitles=subs,
        raw={k: v for k, v in info.items() if k not in ("formats", "automatic_captions", "subtitles")},
    )


def _id_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if "youtu.be" in parsed.netloc:
        return parsed.path.strip("/")
    qs = parse_qs(parsed.query)
    return (qs.get("v") or [url[-11:]])[0]


def probe_url(url: str, timeout_s: int = 120) -> VideoInfo:
    """Metadata-only fetch, used by the UI before a download is confirmed."""
    args = [
        _yt_dlp_bin(), *_base_args(),
        # Metadata to stdout only: no subtitle or info files on disk.
        "--skip-download", "--dump-single-json", "--no-write-subs",
        url,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AcquisitionError(f"metadata probe timed out") from exc
    if proc.returncode != 0:
        raise AcquisitionError(f"metadata probe failed: {(proc.stderr or '')[-600:]}")
    data = json.loads(proc.stdout)
    return _build_info(data, url, [])


def probe_upload(path: Path) -> dict[str, Any]:
    """Everything we can learn about an uploaded file without executing it."""
    from .media import ffprobe

    probe = ffprobe(path)
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "duration_s": probe.duration_s,
        "width": probe.width,
        "height": probe.height,
        "fps": probe.fps,
        "video_codec": probe.video_codec,
        "audio_codec": probe.audio_codec,
        "format": probe.format,
    }
