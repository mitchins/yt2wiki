"""CLI argument parsing and command behaviour (no GPU, no network)."""

from __future__ import annotations

import json

import pytest

from yt2wiki.cli import build_parser


def test_common_options_work_on_both_levels():
    parser = build_parser()
    for argv in (
        ["--data-dir", "/tmp/x", "info"],
        ["info", "--data-dir", "/tmp/x"],
        ["--data-dir", "/tmp/x", "--profile", "compact", "models"],
        ["run", "/tmp/x", "--data-dir", "/tmp/x", "--profile", "enhanced"],
    ):
        args = parser.parse_args(argv)
        assert args.data_dir == "/tmp/x", argv


def test_profile_defaults_to_none():
    parser = build_parser()
    assert parser.parse_args(["info"]).profile is None
    assert parser.parse_args(["info", "--profile", "compact"]).profile == "compact"


def test_run_stage_subset_is_parsed():
    parser = build_parser()
    args = parser.parse_args(
        ["run", "yt-abc", "--data-dir", "/tmp/x", "--stages", "evidence,verify"]
    )
    assert args.target == "yt-abc"
    assert args.stages == "evidence,verify"
    assert args.force is False


def test_schemas_command_emits_valid_json(capsys, tmp_path):
    from yt2wiki.cli import main

    rc = main(["schemas"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stages"][0] == "acquisition"
    assert payload["stages"][-1] == "publish"
    assert "command" in payload["evidence_kinds"]


def test_info_and_models_commands(tmp_path, capsys):
    from yt2wiki.cli import main

    rc = main(["--data-dir", str(tmp_path), "info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "yt2wiki" in out and "asr" in out

    rc = main(["--data-dir", str(tmp_path), "models"])
    assert rc == 0
    reg = json.loads(capsys.readouterr().out)
    assert set(reg["profiles"]) == {"compact", "standard", "enhanced"}


def test_jobs_command_lists_jobs(tmp_path, capsys):
    from yt2wiki.cli import main
    from yt2wiki.db.schema import Database
    from yt2wiki.services import Service
    from yt2wiki.config import Settings

    settings = Settings.load(data_dir=tmp_path, profile="compact")
    db = Database(settings.paths.db_path)
    db.migrate()
    service = Service(settings, db)
    sid = service.create_youtube_source("https://youtu.be/abc")
    job = service.create_job(sid, profile="compact")

    rc = main(["--data-dir", str(tmp_path), "jobs"])
    assert rc == 0
    out = capsys.readouterr().out
    assert job in out


def test_export_command_reports_missing_article(tmp_path, capsys):
    from yt2wiki.cli import main

    rc = main(["--data-dir", str(tmp_path), "export", "art-nope"])
    assert rc == 2
    assert "no such article" in capsys.readouterr().err
