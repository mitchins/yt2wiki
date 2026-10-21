"""M6 paired benchmark tests (deterministic fixtures; no network, no GPU)."""

from __future__ import annotations

import pytest
pytest.importorskip("eval")

from eval.paired.compare import compare_articles
from eval.paired.dataset import PAIRED_SET, by_id
from eval.paired.extract_reference import (
    ReferenceFact,
    ReferenceFacts,
    alignment_score,
    classify_facts,
    extract_reference,
)
from eval.paired.metrics import compute_metrics, delta_against

HTML = """<html><head><title>Task Guide</title>
<meta property="article:published_time" content="2024-03-27" />
<meta name="author" content="Thomas Frank" />
<script type="application/ld+json">{"dateModified": "2024-04-01"}</script>
</head><body><nav>Menu noise</nav><article>
<h1>Task Guide</h1>
<h2>Setup</h2>
<p>You will need a Notion workspace before you start.</p>
<p>Be careful: deleting the database cannot be undone.</p>
<p>Create a Tasks database, then add a Due date property.</p>
<p>Subscribe to my newsletter for more templates.</p>
<pre><code class="language-bash">sudo apt install -y notion-desktop
</code></pre>
<p>Set <code>Due=2024-01-01</code> and open <code>/tasks/inbox</code>.</p>
<img src="/img/setup.png" alt="Tasks database setup" />
</article></body></html>"""


def _ref():
    ref = extract_reference("tf-test", "https://example.com/guide", HTML,
                            final_url="https://example.com/guide", fetched_at=1.0)
    assert ref.title == "Task Guide"
    assert ref.published == "2024-03-27"
    assert ref.updated == "2024-04-01"
    assert ref.author == "Thomas Frank"
    return ref


def test_dataset_has_five_unique_pairs():
    assert len(PAIRED_SET) == 5
    assert len({p.pair_id for p in PAIRED_SET}) == 5
    assert by_id("tf-task-manager").video_id == "32dLXdB4ozs"
    for p in PAIRED_SET:
        assert p.video_url.startswith("https://")
        assert p.article_url.startswith("https://")


def test_extract_reference_structures_facts():
    ref = _ref()
    kinds = {f.kind for f in ref.facts}
    assert {"command", "value", "warning", "prerequisite", "action", "boilerplate",
            "image"} <= kinds
    assert any(f.literal == "Due=2024-01-01" for f in ref.facts if f.kind == "value")
    assert any("setup" in (f.section or "").lower() for f in ref.facts)
    assert ref.images and ref.images[0]["alt"] == "Tasks database setup"
    assert all(f.support == "uncertain" for f in ref.facts)


def test_classify_facts_marks_support_and_article_only():
    ref = _ref()
    video_texts = [
        "sudo apt install -y notion-desktop",
        "Create a Tasks database then add a Due date property",
        "Be careful deleting the database cannot be undone",
        "You will need a Notion workspace before you start",
    ]
    out = classify_facts(ref.facts, video_texts=video_texts,
                         video_literals=["sudo apt install -y notion-desktop",
                                         "Due=2024-01-01", "/tasks/inbox"])
    by_kind = {f.kind: f.support for f in out}
    assert by_kind["command"] == "video_supported"
    assert by_kind["warning"] == "video_supported"
    assert by_kind["boilerplate"] == "article_only"
    score = alignment_score(out)
    assert score["facts"] == len(out)
    assert score["video_supported"] + score["article_only"] + score["uncertain"] == len(out)


MD = """# Task Guide

## Prerequisites
- A Notion workspace

## Procedure
1. **Create a Tasks database with a Due date property.**
2. **Run the installer.**

   ```bash
   sudo apt install -y notion-desktop
   ```

   ![Tasks database setup](assets/setup.png)

   Set `Due=2024-01-01` and open `/tasks/inbox`.

## Expected outcome
## Troubleshooting

> **Warning** — be careful: deleting the database cannot be undone.

## Source and provenance
"""


def _supported_ref():
    def fact(kind, text, literal="", support="video_supported"):
        return ReferenceFact(fact_id=f"{kind}:{text[:8]}", kind=kind, text=text,
                             literal=literal, section="Setup", locator="block-0",
                             support=support, reason="fixture")

    return ReferenceFacts(
        pair_id="tf-test", article_url="https://example.com", final_url="https://example.com",
        fetched_at=1.0, sha256="x", title="Task Guide",
        headings=["Task Guide", "Setup"],
        blocks=[{"kind": "image", "src": "https://example.com/img/setup.png",
                 "alt": "Tasks database setup", "section": "Setup", "index": 0}],
        facts=[
            fact("value", "Set Due=2024-01-01", "Due=2024-01-01"),
            fact("command", "sudo apt install -y notion-desktop",
                 "sudo apt install -y notion-desktop"),
            fact("config", "Set Due=2024-01-01", "Due=2024-01-01"),
            fact("action", "Create a Tasks database with a Due date property"),
            fact("prerequisite", "You will need a Notion workspace"),
            fact("warning", "Be careful deleting the database"),
            fact("boilerplate", "Subscribe to my newsletter", support="article_only"),
        ])


def test_compare_scores_supported_facts_and_ignores_article_only():
    comp = compare_articles(_supported_ref(), markdown=MD, findings=[])
    assert comp.dimensions["exact_value_recall"] == 1.0
    assert comp.dimensions["command_config_preservation"] == 1.0
    assert comp.dimensions["semantic_action_coverage"] == 1.0
    assert comp.dimensions["prerequisite_warning_coverage"] == 1.0
    assert comp.dimensions["unsupported_actionable_rate"] == 0.0
    assert comp.dimensions["image_placement_linkage"] == 1.0
    assert comp.dimensions["structure"] >= 0.7
    assert comp.alignment == {"facts": 7, "video_supported": 6, "article_only": 1,
                              "uncertain": 0, "alignment": round(6 / 7, 4)}
    statuses = {f.fact_id: f.status for f in comp.fact_outcomes}
    assert all(s != "miss" for fid, s in statuses.items() if not fid.startswith("boilerplate"))


def test_compare_flags_missing_values_and_unsupported_claims():
    ref = _supported_ref()
    md = "## Procedure\n\n1. **Do something unrelated.**\n"
    findings = [{"direction": "article->evidence", "severity": "critical",
                 "code": "unsupported_action", "message": "no evidence for this step"}]
    comp = compare_articles(ref, markdown=md, findings=findings)
    assert comp.dimensions["exact_value_recall"] == 0.0
    assert comp.dimensions["unsupported_actionable_rate"] == 1.0
    assert comp.details["contradiction_count"] == 0
    assert "Due=2024-01-01" in comp.details["missing_values"][0]


def test_metrics_aggregate_and_deltas():
    comp = compare_articles(_supported_ref(), markdown=MD, findings=[])
    m = compute_metrics("run-1", [comp.to_dict()])
    assert m.pairs == 1
    assert m.means["exact_value_recall"] == 1.0
    assert m.alignment["video_supported"] == 6
    prev = {"run_id": "run-0",
            "per_pair": [{"pair_id": "tf-test",
                          "dimensions": {"exact_value_recall": 0.5,
                                         "command_config_preservation": 0.5,
                                         "semantic_action_coverage": 0.5,
                                         "unsupported_actionable_rate": 0.0,
                                         "prerequisite_warning_coverage": 0.5,
                                         "structure": 0.5,
                                         "image_placement_linkage": 0.5,
                                         "conciseness": 0.5}}]}
    deltas = delta_against(m, prev)
    assert deltas[0]["delta"]["exact_value_recall"] == 0.5


def test_classify_pair_prefers_frozen_captions(tmp_path):
    import json
    from eval.paired.run import classify_pair
    from eval.paired.dataset import by_id

    item = by_id("tf-tasks-projects")
    d = tmp_path / item.pair_id
    d.mkdir()
    (d / "captions.json").write_text(json.dumps([
        {"start": 0.0, "end": 1.0, "text": "Create a Tasks database with a Due date"},
        {"start": 1.0, "end": 2.0, "text": "sudo apt install -y notion-desktop"},
    ]))
    (d / "reference.json").write_text(json.dumps({
        "pair_id": item.pair_id, "article_url": item.article_url,
        "final_url": item.article_url, "fetched_at": 1.0, "sha256": "x",
        "title": "t", "published": "", "updated": "", "author": "",
        "headings": [], "blocks": [], "images": [],
        "facts": [
            {"fact_id": "a", "kind": "command", "text": "run it",
             "literal": "sudo apt install -y notion-desktop", "section": "",
             "locator": "", "support": "uncertain", "reason": ""},
            {"fact_id": "b", "kind": "boilerplate", "text": "newsletter",
             "literal": "", "section": "", "locator": "", "support": "uncertain",
             "reason": ""},
        ],
        "provenance": {"video_title": item.video_title, "chapters": []},
    }))
    ref = classify_pair(item, None, tmp_path)
    by_id_map = {f.fact_id: f.support for f in ref.facts}
    assert by_id_map["a"] == "video_supported"
    assert by_id_map["b"] == "article_only"
    assert ref.provenance["classify_source"] == "captions"


def test_empty_run_report_and_metrics_do_not_crash(tmp_path):
    from eval.paired.metrics import compute_metrics
    from eval.paired.run import write_report

    m = compute_metrics("empty", [])
    assert m.pairs == 0
    path = write_report(tmp_path / "out", [], [], profile="standard")
    assert path.is_file()
