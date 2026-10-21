"""Deterministic fixtures for tests.  No network, no models, no GPU."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

TEST_SOURCE_ID = "test-synthetic-0001"


def make_test_video(path: Path, *, seconds: float = 6.0, fps: int = 8) -> Path:
    """Small video with hard cuts and on-screen text, generated with ffmpeg only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = int(seconds * fps)
    frames = []
    palette = ["#ffffff", "#101010", "#ffffff", "#303030"]
    for i in range(total):
        seg = sum(1 for c in (2.0, 4.0) if (i / fps) >= c)
        bg = palette[seg % len(palette)]
        fg = "#000000" if seg % 2 == 0 else "#ffffff"
        img = Image.new("RGB", (640, 360), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([20, 20, 620, 340], outline=fg, width=3)
        d.text((40, 60), f"segment {seg}", fill=fg)
        d.text((40, 110), f"t={i / fps:.2f}s", fill=fg)
        d.text((40, 150), "sudo apt install -y nginx-1.24.0", fill=fg)
        frames.append(img)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        for i, f in enumerate(frames):
            f.save(Path(td) / f"{i:04d}.png")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-framerate", str(fps),
             "-i", str(Path(td) / "%04d.png"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "16",
             str(path)],
            check=True,
        )
    # a silent audio track so the inspect stage has something to extract
    out = path.with_suffix(".audio.mp4")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
         "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
         "-c:v", "copy", "-c:a", "pcm_s16le", "-shortest", str(out)],
        check=True,
    )
    out.replace(path)
    return path


def write_wav(path: Path, *, seconds: float = 3.0, rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    t = np.linspace(0, seconds, n, endpoint=False)
    tone = (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(tone.tobytes())
    return path


def write_frame(path: Path, *, lines: list[str], size=(1280, 720)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 40
    for line in lines:
        d.text((50, y), line, fill="black")
        y += 40
    img.save(path, quality=2)
    return path


SAMPLE_TRANSCRIPT = [
    {"start": 0.0, "end": 4.0, "text": "In this tutorial we install nginx on Ubuntu 24.04."},
    {"start": 4.0, "end": 9.5, "text": "First run sudo apt update then install nginx."},
    {"start": 9.5, "end": 14.0, "text": "Click on Settings and set the value to 42 mA."},
    {"start": 14.0, "end": 18.5, "text": "Careful, this will overwrite the existing config."},
    {"start": 18.5, "end": 22.0, "text": "Actually use port 8080, not 80."},
]

SAMPLE_EVIDENCE = [
    {
        "evidence_id": "ev-aaa",
        "kind": "action",
        "text": "Update the package index with sudo apt update on Ubuntu 24.04.",
        "t_start": 4.0,
        "t_end": 6.0,
        "frame_ids": ["f-bbb"],
        "literals": [
            {"kind": "command", "literal": "sudo apt update", "normalized": "sudo apt update",
             "context": "", "t_start": 4.0, "t_end": 6.0}
        ],
        "confidence": 0.9,
        "model_key": "text-4b",
        "prompt_version": "v2",
        "status": "active",
    },
    {
        "evidence_id": "ev-ccc",
        "kind": "measurement",
        "text": "Set the current limit to 42 mA.",
        "t_start": 9.5,
        "t_end": 11.0,
        "frame_ids": ["f-ddd"],
        "literals": [
            {"kind": "measurement", "literal": "42 mA", "normalized": "42 mA", "context": "",
             "t_start": 9.5, "t_end": 11.0}
        ],
        "confidence": 0.85,
        "model_key": "text-4b",
        "prompt_version": "v2",
        "status": "active",
    },
    {
        "evidence_id": "ev-eee",
        "kind": "warning",
        "text": "This will overwrite the existing nginx configuration.",
        "t_start": 14.0,
        "t_end": 16.0,
        "frame_ids": [],
        "literals": [],
        "confidence": 0.8,
        "model_key": "text-4b",
        "prompt_version": "v2",
        "status": "active",
    },
    {
        "evidence_id": "ev-fff",
        "kind": "correction",
        "text": "The presenter corrects the port: use 8080, not 80.",
        "t_start": 18.5,
        "t_end": 21.0,
        "frame_ids": [],
        "literals": [
            {"kind": "port", "literal": "port 8080", "normalized": "port 8080", "context": "",
             "t_start": 18.5, "t_end": 21.0}
        ],
        "confidence": 0.9,
        "model_key": "text-4b",
        "prompt_version": "v2",
        "status": "active",
    },
]


def in_memory_db(tmp: Path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database

    settings = Settings.load(data_dir=tmp, profile="compact", device_index=None)
    db = Database(tmp / "db" / "yt2wiki.sqlite3")
    db.migrate()
    return settings, db
