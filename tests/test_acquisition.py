"""Acquisition unit tests (no network: subprocess is stubbed)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from yt2wiki.acquisition import (
    AcquisitionError,
    SubtitleTrack,
    _best_track,
    _parse_subtitles,
    download_video,
)


def test_best_track_prefers_manual_then_coverage():
    auto = SubtitleTrack(lang="en", auto=True, captions=[])
    manual = SubtitleTrack(lang="en", auto=False, captions=[])
    assert _best_track([auto, manual]) is manual
    assert _best_track([manual, auto]) is manual


def test_stage_module_imports_best_track():
    import yt2wiki.pipeline.stages.acquisition as stage

    assert stage._best_track is _best_track


def _stub_run(monkeypatch, tmp_path):
    calls = []

    class _Proc:
        def __init__(self, returncode=0, out="", err=""):
            self.returncode = returncode
            self.stdout = out
            self.stderr = err

    def fake_run(args, **kwargs):
        calls.append(args)
        if "--skip-download" in args:
            return _Proc(1, "", "ERROR: Unable to download video subtitles: 429")
        # video invocation: lay down a fake media file + info.json
        out_tpl = args[args.index("--output") + 1]
        tmp = Path(out_tpl).parent
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "vid123.mp4").write_bytes(b"\x00" * 64)
        (tmp / "vid123.info.json").write_text(json.dumps({
            "id": "vid123", "title": "t", "uploader": "u", "duration": 10,
            "description": "d", "webpage_url": "https://example.com",
        }))
        return _Proc(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_subtitle_failure_does_not_fail_acquisition(monkeypatch, tmp_path):
    calls = _stub_run(monkeypatch, tmp_path)
    media, info = download_video("https://example.com/v", tmp_path / "out",
                                 timeout_s=30)
    assert media.name == "source.mp4"
    assert info.subtitles == []
    assert any("--skip-download" in a for a in calls[1:])


def test_parse_subtitles_classifies_manual_vs_auto(tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nhello world\n"
    (tmp_path / "vid.en-orig.srt").write_text(srt)
    (tmp_path / "vid.en.srt").write_text(srt)
    tracks = _parse_subtitles(tmp_path, {"subtitles": {"en": []},
                                         "automatic_captions": {"en": []}})
    by_auto = {t.auto: t.lang for t in tracks}
    assert by_auto == {False: "en", True: "en"}
