"""Unit tests: media primitives, exact-value extraction, rendering, verification rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from yt2wiki.article import format_ts, markdown_to_html, render_markdown, render_html
from yt2wiki.article import Article, FrameRef, Section
from yt2wiki.literals import (
    extract_literals,
    normalize_value,
    values_match,
)
from yt2wiki.paths import safe_slug, source_id_for_file, source_id_for_youtube
from yt2wiki.visual import hamming, screen_change_scores

from .fixtures import (
    SAMPLE_EVIDENCE,
    SAMPLE_TRANSCRIPT,
    make_test_video,
    write_frame,
    write_wav,
)


# ----------------------------------------------------------------------
# paths


def test_source_id_youtube_variants():
    a = source_id_for_youtube("https://www.youtube.com/watch?v=gd7BXuUQ91w")
    b = source_id_for_youtube("https://youtu.be/gd7BXuUQ91w")
    c = source_id_for_youtube("https://www.youtube.com/shorts/gd7BXuUQ91w")
    assert a == b == c == "yt-gd7BXuUQ91w"


def test_source_id_file_stability(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"0123456789abcdef")
    a = source_id_for_file(p)
    b = source_id_for_file(p)
    assert a == b  # S5863: distinct bindings — asserts determinism, not a==a tautology
    q = tmp_path / "other.mp4"
    q.write_bytes(b"different-bytes")
    assert source_id_for_file(p) != source_id_for_file(q)


def test_safe_slug():
    assert safe_slug("Hello World!/x") == "Hello-World-x"
    assert safe_slug("!!!") == "item"
    assert len(safe_slug("a" * 200)) <= 64


# ----------------------------------------------------------------------
# exact values


def test_extract_literals_versions_units_paths():
    text = (
        "Install nginx 1.24.0 from /etc/nginx/nginx.conf and set port 8080. "
        "Use 42 mA and 3.3 V. The server is 192.168.1.24 with mac 00:1A:2B:3C:4D:5E. "
        "To apply it run:\nsudo systemctl restart nginx"
    )
    lits = extract_literals(text)
    by_kind: dict[str, list[str]] = {}
    for lit in lits:
        by_kind.setdefault(lit.kind, []).append(lit.literal)
    assert any(v.startswith("1.24.0") for v in by_kind.get("version", []))
    assert "42 mA" in by_kind.get("measurement", [])
    assert "3.3 V" in by_kind.get("measurement", [])
    assert "/etc/nginx/nginx.conf" in by_kind.get("path", [])
    assert "port 8080" in by_kind.get("port", [])
    assert "192.168.1.24" in by_kind.get("ip_address", [])
    assert "00:1A:2B:3C:4D:5E" in by_kind.get("mac_address", [])
    assert any("systemctl restart nginx" in c for c in by_kind.get("command", []))


def test_exact_values_are_not_normalised_away():
    assert normalize_value(" 42 mA ") == "42 mA"
    assert normalize_value("`1.24.0`") == "1.24.0"
    assert values_match("42 mA", "42mA") is True
    assert values_match("1.24.0", "1.24.1") is False


def test_command_detection_is_conservative():
    assert extract_literals("use the thing somewhere", t_start=1, t_end=2) == []
    cmds = [l.literal for l in extract_literals("$ docker compose up -d --build")]
    assert any("docker compose up -d" in c for c in cmds)


# ----------------------------------------------------------------------
# media


def test_media_probe_and_audio(tmp_path):
    from yt2wiki.media import extract_audio, ffprobe

    video = make_test_video(tmp_path / "v.mp4")
    probe = ffprobe(video)
    assert probe.has_video and probe.has_audio
    assert 5.0 < probe.duration_s < 7.5
    audio = extract_audio(video, tmp_path / "a.wav")
    assert audio.exists()
    import wave

    with wave.open(str(audio)) as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1


def test_scene_detection_finds_cuts(tmp_path):
    from yt2wiki.media import scene_change_times

    video = make_test_video(tmp_path / "v.mp4")
    times = scene_change_times(video, threshold=0.2, min_gap_s=1.0)
    assert len(times) >= 2
    assert times == sorted(times)


def test_frame_extraction(tmp_path):
    from yt2wiki.media import extract_frame

    video = make_test_video(tmp_path / "v.mp4")
    out = extract_frame(video, 1.0, tmp_path / "f.jpg")
    assert out.exists() and out.stat().st_size > 500
    from PIL import Image

    assert Image.open(out).size[0] > 0


def test_wav_roundtrip(tmp_path):
    from yt2wiki.backends.asr import load_wav_mono16

    wav = write_wav(tmp_path / "t.wav", seconds=1.0)
    samples = load_wav_mono16(wav)
    assert samples.dtype.name == "float32"
    assert 15000 < len(samples) < 17000
    assert abs(float(samples.max())) <= 1.0


# ----------------------------------------------------------------------
# visual signatures


def test_signature_stream_and_dedup(tmp_path):
    from yt2wiki.visual import dedupe_by_signature, stream_signatures

    video = make_test_video(tmp_path / "v.mp4")
    sigs = list(stream_signatures(video, fps=2.0))
    assert len(sigs) >= 8
    assert all(0 <= t <= 8 for t in (s.t for s in sigs))

    blank = [t for t in (0.0, 0.1, 0.2)]
    kept = dedupe_by_signature(blank, sigs, max_hamming=2, sample_fps=2.0)
    assert kept == [0.0]


def test_hamming_symmetry():
    assert hamming(0b1010, 0b1010) == 0
    assert hamming(0b1010, 0b0101) == 4


def test_screen_change_scores_spike_on_cut(tmp_path):
    from yt2wiki.visual import stream_signatures

    video = make_test_video(tmp_path / "v.mp4")
    sigs = list(stream_signatures(video, fps=4.0))
    scores = screen_change_scores(sigs)
    assert any(s > 0.15 for _, s in scores)


# ----------------------------------------------------------------------
# markdown / html rendering


def _sample_article() -> Article:
    return Article(
        source_id="s1",
        title="Install nginx 1.24.0 on Ubuntu 24.04",
        summary="Installs the web server.",
        provenance={"url": "https://youtu.be/abc", "uploader": "chan",
                    "profile": "compact", "fingerprint": "deadbeef",
                    "attribution_line": "Compiled from **the video**"},
        sections=[
            Section(heading="Scope and applicability", level=2,
                    body=["This covers a fresh install only."]),
            Section(heading="Procedure", level=2,
                    body=["1. **Update the package index.**\n\n   ```bash\n   sudo apt update\n   ```\n\n   *Source: [0:04](https://youtu.be/abc?t=4)*\n\n   > **Warning** — overwrites config"]),
        ],
    )


def test_markdown_render_contains_all_sections():
    md = render_markdown(_sample_article())
    assert md.startswith("# Install nginx 1.24.0")
    assert "## Scope and applicability" in md
    assert "## Procedure" in md
    assert "sudo apt update" in md
    assert "t=4" in md
    assert "> **Warning** — overwrites config" in md


def test_html_is_self_contained_and_escapes():
    article = _sample_article()
    article.sections[1].frames = [FrameRef(frame_id="f-1", path=Path("x"), caption="Settings <panel>", t=12.5)]
    md = render_markdown(article)
    html = render_html(article, markdown_to_html(md))
    assert "<!DOCTYPE html>" in html
    assert "<script" not in html.lower()
    assert "Settings &lt;panel&gt;" in html
    # images are referenced, not embedded, unless asked for
    assert 'src="assets/f-1.jpg"' in html


def test_html_embed_uses_data_uri(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (8, 8), "red")
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    img.save(asset_dir / "f-1.jpg")
    body = '<figure><img alt="x" src="assets/f-1.jpg"></figure>'
    html = render_html(_sample_article(), body, embed_images=True, asset_dir=asset_dir)
    assert "data:image/jpeg;base64," in html
    assert 'src="assets/f-1.jpg"' not in html


def test_markdown_renderer_handles_lists_code_and_links():
    body = "\n".join([
        "## H",
        "",
        "- one",
        "- two",
        "",
        "1. step",
        "",
        "```bash",
        "echo hi",
        "```",
        "",
        "see [docs](https://x.example)",
        "",
        "![img](assets/a.jpg)",
    ])
    html = markdown_to_html(body)
    assert "<li>one</li>" in html and "<li>two</li>" in html
    assert '<ol class="steps">' in html
    assert "<pre" in html and "echo hi" in html
    assert '<a href="https://x.example">docs</a>' in html
    assert '<img alt="img" ' in html


def test_timestamp_formatting():
    assert format_ts(9.4) == "0:09"
    assert format_ts(65) == "1:05"
    assert format_ts(3725) == "1:02:05"
    assert format_ts(None) == "?"


# ----------------------------------------------------------------------
# records


def test_chunk_transcript_windows_overlap_and_are_bounded(tmp_path):
    from yt2wiki.db.schema import Database
    from yt2wiki.records import chunk_transcript

    settings, db = _db(tmp_path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sources(source_id, kind, created_at) VALUES(?,?,?)",
            ("s1", "upload", 0.0),
        )
        rows = []
        t = 0.0
        for i in range(200):
            rows.append({"idx": i, "start": t, "end": t + 1.0, "text": "x " * 40})
            t += 1.0
    chunks = chunk_transcript(rows, window_s=30.0, overlap_s=5.0)
    assert len(chunks) > 4
    for i in range(1, len(chunks)):
        assert chunks[i][0]["start"] < chunks[i - 1][-1]["end"]


def test_new_evidence_extracts_literals_and_is_stable(tmp_path):
    from yt2wiki.records import new_evidence

    a = new_evidence("s1", "measurement", "Set the current to 42 mA.", t_start=1, t_end=2)
    b = new_evidence("s1", "measurement", "Set the current to 42 mA.", t_start=1, t_end=2)
    assert a.evidence_id == b.evidence_id
    assert [l.literal for l in a.literals] == ["42 mA"]


def _db(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(tmp_path / "db" / "t.sqlite3")
    db.migrate()
    return settings, db


# ----------------------------------------------------------------------
# command-literal precision


def test_command_extraction_is_precise():
    from yt2wiki.literals import extract_literals

    cases = {
        "To connect, run the command ssh root@96.126.117.9": ["ssh root@96.126.117.9"],
        "Run: ssh root@96.126.117.9": ["ssh root@96.126.117.9"],
        "$ sudo apt update && sudo apt install -y nginx":
            ["sudo apt update", "sudo apt install -y nginx"],
        "we install Docker engine by running the command sudo apt install docker.io":
            ["sudo apt install docker.io"],
        "You can check by running the command lsb_release -a": ["lsb_release -a"],
        "execute the command docker compose up -d --build": ["docker compose up -d --build"],
    }
    for text, expected in cases.items():
        got = [lit.literal for lit in extract_literals(text) if lit.kind == "command"]
        assert got == expected, (text, got)


def test_command_extraction_rejects_prose():
    from yt2wiki.literals import extract_literals

    prose = [
        "Open a file for editing using nano.",
        "In nano, save the file by pressing control x, y, and enter.",
        "This operation is irreversible",
        "use vim instead of nano to edit the file",
        "Check the config in /etc/nginx/nginx.conf before reloading.",
        "First install the package, then restart the service.",
        # NOTE: ASR merges "run mkdir X, then cd into X" into one clause, so a
        # compound literal is still accepted.  Documented as a limitation in
        # docs/KNOWN_LIMITATIONS.md rather than "fixed" with a heuristic that
        # would reject ordinary invocations like "apt-get install -y ffmpeg".
    ]
    for text in prose:
        cmds = [lit.literal for lit in extract_literals(text) if lit.kind == "command"]
        assert cmds == [], (text, cmds)


def test_command_extraction_known_limitation_compound_commands():
    """ASR-merged command sequences are recorded as one literal."""
    from yt2wiki.literals import extract_literals

    got = [lit.literal for lit in extract_literals("then run mkdir myproject cd myproject")
           if lit.kind == "command"]
    assert got == ["mkdir myproject cd myproject"]


def test_looks_like_command_requires_a_known_program_for_bare_lines():
    from yt2wiki.literals import looks_like_command

    assert looks_like_command("ssh root@example.com")
    assert looks_like_command("apt-get install -y ffmpeg")
    assert not looks_like_command("You can check by running lsb_release")
    # a bare line ending in prose punctuation is never a command
    assert not looks_like_command("nano myfile.txt.")


def test_version_suffixed_programs_are_recognised():
    from yt2wiki.commands import is_executable, program_name

    assert program_name("sudo systemctl restart nginx") == "systemctl"
    assert is_executable("python3.11 -m venv .venv")
    assert is_executable("docker compose up")
    assert not is_executable("Ensure the directory is empty")
