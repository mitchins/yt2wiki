"""Eval harness smoke tests (no models, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.manifest import EVAL_SET, by_category, summary
from eval.metrics import (
    article_frames,
    article_sections,
    article_steps,
    article_values,
    by_category as metrics_by_category,
    score_item,
)
from eval.reference import ReferenceItem, containment, jaccard, load_reference

from tests.fixtures import SAMPLE_EVIDENCE


def test_manifest_covers_required_categories():
    cats = summary()
    assert set(cats) >= {
        "install", "terminal", "electronics", "mechanical", "silent",
        "small-text", "corrected", "sponsored", "explanatory",
    }
    assert len(EVAL_SET) >= 24


def test_manifest_entries_are_unique_and_populated():
    ids = [i.video_id for i in EVAL_SET]
    assert len(set(ids)) == len(ids)
    for item in EVAL_SET:
        assert item.duration_s > 0
        assert item.why.strip()
        assert item.category


def test_by_category_groups_every_item():
    grouped = by_category()
    assert sum(len(v) for v in grouped.values()) == len(EVAL_SET)


def test_reference_round_trip(tmp_path):
    ref = ReferenceItem(
        video_id="abc", category="install", title="t", channel="c",
        duration_s=10, essential_actions=["install nginx"],
        exact_values=["1.24.0"], reviewed=False,
    )
    import yaml

    path = tmp_path / "abc.yaml"
    path.write_text(yaml.safe_dump(ref.to_dict(), sort_keys=False), encoding="utf-8")
    loaded = load_reference(path)
    assert loaded.video_id == "abc"
    assert loaded.essential_actions == ["install nginx"]
    assert loaded.exact_values == ["1.24.0"]


MD = """# Title

## Prerequisites
- Ubuntu 24.04

## Procedure
1. **Update the package index and install nginx 1.24.0.**

   ```bash
   sudo apt update && sudo apt install -y nginx
   ```

   *Source: [0:04](https://youtu.be/x?t=4)*

2. **Listen on `8080` instead of port 80.**

   > **Warning** — this will overwrite the existing configuration.

## Expected outcome
## Troubleshooting
## Unresolved details
## Source and provenance
"""


def test_article_parsing_helpers():
    steps = article_steps(MD)
    assert len(steps) == 2
    assert "Update the package index" in steps[0]
    assert any("8080" in s for s in steps)
    values = article_values(MD)
    assert "8080" in values
    assert any("apt install" in v for v in values)
    sections = article_sections(MD)
    assert "Prerequisites" in sections and "Source and provenance" in sections
    assert article_frames(MD) == []


def test_score_item_flags_missing_actions_and_values():
    ref = ReferenceItem(
        video_id="abc", category="install",
        essential_actions=["install nginx 1.24.0", "listen on port 8080"],
        exact_values=["1.24.0", "8080", "2.5.0"],
        required_prerequisites=["Ubuntu 24.04"],
        required_warnings=["this will overwrite the configuration"],
        corrections=[{"earlier": "use port 80", "later": "use port 8080",
                      "resolution": "correction in article"}],
        key_moments=[{"t": 10.0, "window": 5}],
    )
    m = score_item(
        ref,
        markdown=MD,
        evidence_texts=["Update the package index.", "Install nginx 1.24.0.",
                        "Listen on port 8080."],
        evidence_literals=["1.24.0", "8080", "sudo apt install -y nginx"],
        frame_times=[9.0, 400.0],
        wall_s=123.4,
        peak_vram_gb=8.9,
    )
    assert m.ok
    assert m.essential_action_recall > 0.5
    assert m.technical_value_accuracy >= 0.6
    assert m.missing_values == ["2.5.0"]
    assert m.correction_handling == 1.0
    assert m.useful_frame_precision == 0.5
    assert m.useful_frame_coverage == 1.0
    assert m.prerequisite_omission == 0.0
    assert m.warning_omission == 0.0
    assert m.frames_used == 0


def test_score_item_flags_unsupported_action():
    ref = ReferenceItem(video_id="xyz", category="terminal",
                        essential_actions=["install nginx"])
    md = ("## Procedure\n\n1. **Flash the firmware with dd and reformat the disk.**\n\n"
          "   ```bash\n   dd if=/dev/zero of=/dev/sda\n   ```\n\n")
    m = score_item(ref, markdown=md, evidence_texts=["install nginx"],
                   evidence_literals=["nginx"], config="A")
    assert m.unsupported_actionable_rate == 1.0
    assert len(m.unsupported_examples) == 1


def test_scoring_utilities():
    assert jaccard("install nginx 1.24.0", "install nginx 1.24.0") == 1.0
    assert containment("nginx", "install nginx") == 1.0
    assert containment("install", "nginx") == 0.0
    assert jaccard("install nginx", "install nginx") == 1.0
    assert jaccard("install nginx", "install apache") < 0.4
    assert containment("nginx and more", "nginx") == 0.5
    assert containment("nginx", "install nginx on ubuntu 24.04") == 1.0


def test_eval_configs_use_real_tuning_knobs():
    from yt2wiki.config import Tuning
    from eval.run_eval import CONFIGS

    available = set(Tuning().fingerprint_dict())
    for key, spec in CONFIGS.items():
        for knob in spec["tuning"]:
            assert knob in available, f"config {key} uses unknown knob {knob}"
