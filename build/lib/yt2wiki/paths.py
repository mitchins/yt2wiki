"""Filesystem layout and path helpers for a yt2wiki data directory.

Everything a job produces is addressed from a single data root so that a whole
library can be moved, backed up or bind-mounted as one directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "YT2WIKI_HOME"
ENV_HF_HOME = "HF_HOME"

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "yt2wiki"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_slug(value: str, *, maxlen: int = 64, fallback: str = "item") -> str:
    """Turn arbitrary text into a filename-safe token."""
    cleaned = _SAFE.sub("-", str(value)).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:maxlen].rstrip("-.") or fallback


def content_hash(data: bytes, *, prefix: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:prefix]


def file_hash(path: Path, *, prefix: int = 16, chunk: int = 1 << 20) -> str:
    # S8707: validate before opening — tainted CLI/web path must be a regular file
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"not a regular file: {p}")
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()[:prefix]


@dataclass(frozen=True)
class Paths:
    """Resolved locations inside the yt2wiki data root."""

    root: Path

    # ---- construction -------------------------------------------------
    @classmethod
    def resolve(cls, data_dir: str | Path | None = None) -> "Paths":
        if data_dir is None:
            data_dir = os.environ.get(ENV_HOME)
        root = Path(data_dir).expanduser() if data_dir else DEFAULT_DATA_DIR
        root = root.resolve()
        return cls(root=root)

    # ---- top level ----------------------------------------------------
    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / "yt2wiki.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def articles_dir(self) -> Path:
        return self.root / "articles"

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def hf_home(self) -> Path:
        return self.root / "models"

    def ensure(self) -> "Paths":
        for d in (
            self.db_dir,
            self.media_dir,
            self.frames_dir,
            self.work_dir,
            self.articles_dir,
            self.uploads_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # ---- per object ---------------------------------------------------
    def source_dir(self, source_id: str) -> Path:
        return self.media_dir / source_id

    def source_meta(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "meta.json"

    def source_video(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "video.mp4"

    def source_audio(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "audio_16k.wav"

    def source_frames(self, source_id: str) -> Path:
        return self.frames_dir / source_id

    def upload_path(self, upload_id: str, filename: str) -> Path:
        return self.uploads_dir / f"{upload_id}-{safe_slug(Path(filename).name)}"

    def job_dir(self, job_id: str) -> Path:
        return self.work_dir / job_id

    def job_stage_dir(self, job_id: str, stage: str) -> Path:
        return self.job_dir(job_id) / stage

    def article_dir(self, article_id: str) -> Path:
        return self.articles_dir / article_id

    def article_markdown(self, article_id: str) -> Path:
        return self.article_dir(article_id) / "article.md"

    def article_html(self, article_id: str) -> Path:
        return self.article_dir(article_id) / "article.html"

    def article_assets(self, article_id: str) -> Path:
        return self.article_dir(article_id) / "assets"

    def export_bundle(self, article_id: str) -> Path:
        return self.article_dir(article_id) / "export"

    def source_by_id(self, source_id: str) -> Path | None:
        d = self.source_dir(source_id)
        return d if d.is_dir() else None


def source_id_for_youtube(url: str) -> str:
    """Stable id derived from the canonical YouTube video id."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    vid = ""
    if "youtu.be" in host:
        vid = parsed.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(parsed.query)
        vid = (qs.get("v") or [""])[0]
        if not vid:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                vid = parts[1]
    vid = safe_slug(vid, maxlen=32) if vid else ""
    if not vid:
        vid = content_hash(url.encode(), prefix=10)
    return f"yt-{vid}"


def source_id_for_file(path: Path) -> str:
    """Stable id derived from file content (deduplicates re-uploads)."""
    return f"file-{file_hash(path, prefix=16)}"
