"""Verification rules: bidirectional checks and exact-value protection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yt2wiki.backends.llm import extract_json, repair_truncated_json
from yt2wiki.pipeline.stages.verify import (
    _confirm_llm_findings,
    _rule_omissions,
    _rule_unsupported,
    _rule_value_mismatch,
    _supported_values,
)
from yt2wiki.records import Evidence
from yt2wiki.literals import Literal

from .fixtures import SAMPLE_EVIDENCE


def _evidence_items() -> list[Evidence]:
    out = []
    for d in SAMPLE_EVIDENCE:
        out.append(
            Evidence(
                evidence_id=d["evidence_id"],
                kind=d["kind"],
                text=d["text"],
                literals=[Literal(**lit) for lit in d["literals"]],
                t_start=d["t_start"],
                t_end=d["t_end"],
                frame_ids=d["frame_ids"],
                confidence=d["confidence"],
            )
        )
    return out


GOOD_ARTICLE = """# Install nginx 1.24.0 on Ubuntu 24.04

> Compiled from the video

## Scope and applicability

Covers a fresh install only.

## Prerequisites

- A 64-bit Ubuntu 24.04 machine with sudo access.

## Procedure

1. **Update the package index.**

   ```bash
   sudo apt update
   ```

   *Source: [0:04](https://youtu.be/x?t=4)*

2. **Set the current limit to 42 mA.**

   *Source: [0:09](https://youtu.be/x?t=9)*

   > **Warning** — this will overwrite the existing nginx configuration.

3. **Listen on port 8080, not 80.**

## Expected outcome

## Source and provenance

- Source URL
"""

UNSUPPORTED_ARTICLE = """# Do the thing

## Procedure

1. **Rebuild the kernel with CONFIG_FOO enabled and flash it with dd.**

   ```bash
   dd if=/dev/zero of=/dev/sda
   ```

## Source and provenance
"""


def _ctx(tmp_path):
    from yt2wiki.config import Settings
    from yt2wiki.db.schema import Database
    from yt2wiki.pipeline import StageContext

    settings = Settings.load(data_dir=tmp_path, profile="compact", device_index=None)
    db = Database(tmp_path / "db" / "t.sqlite3")
    db.migrate()
    return StageContext(job_id="j", source_id="s", settings=settings, db=db,
                        paths=settings.paths, fingerprint="fp")


def test_good_article_has_no_critical_findings(tmp_path):
    ctx = _ctx(tmp_path)
    items = _evidence_items()
    findings = (
        _rule_unsupported(ctx, GOOD_ARTICLE, items)
        + _rule_omissions(ctx, GOOD_ARTICLE, items)
        + _rule_value_mismatch(ctx, GOOD_ARTICLE, items, _supported_values(items))
    )
    critical = [f for f in findings if f["severity"] == "critical"]
    assert not critical, [f["message"] for f in critical]


def test_unsupported_article_is_flagged(tmp_path):
    ctx = _ctx(tmp_path)
    items = _evidence_items()
    findings = _rule_unsupported(ctx, UNSUPPORTED_ARTICLE, items)
    assert any(f["code"] == "unsupported_action" for f in findings)
    assert any(f["code"] == "unsupported_command" for f in findings)


def test_omission_detection_requires_critical_kind():
    items = _evidence_items()
    md = "Article with no mention of the docker configuration or the current limit."
    class _Ctx:
        source_id = "s"
        job_id = "j"
    findings = _rule_omissions(_Ctx(), md, items)
    codes = {f["code"] for f in findings}
    assert "omitted_warning" in codes
    assert "omitted_measurement" in codes


def test_value_mismatch_flags_invented_numbers(tmp_path):
    ctx = _ctx(tmp_path)
    items = _evidence_items()
    supported = _supported_values(items)
    md = "Set the current limit to `99 mA` and install `nginx 9.9.9`."
    findings = _rule_value_mismatch(ctx, md, items, supported)
    assert findings
    assert all(f["code"] == "value_mismatch" for f in findings)


def test_value_mismatch_accepts_source_values(tmp_path):
    ctx = _ctx(tmp_path)
    items = _evidence_items()
    supported = _supported_values(items)
    md = "Set the current limit to `42 mA`."
    findings = _rule_value_mismatch(ctx, md, items, supported)
    assert findings == []


def test_llm_findings_require_corroboration():
    raw = [
        {
            "direction": "article->evidence",
            "severity": "critical",
            "code": "unsupported_action",
            "message": "The article claims you must format the disk.",
            "quote": "format the disk",
            "evidence_ref": "ev-does-not-exist",
        },
        {
            "direction": "article->evidence",
            "severity": "warning",
            "code": "omitted_action",
            "message": "The article omits updating the package index.",
            "quote": "",
            "evidence_ref": "",
        },
    ]
    items = _evidence_items()
    confirmed = _confirm_llm_findings(raw, items)
    # the first cites a non-existent observation: recorded as a broken
    # citation, never silently dropped; the second has no rule basis
    codes = [f["code"] for f in confirmed]
    assert "broken_citation" in codes
    assert all(f["detail"].get("evidence_ref", "") != "ev-does-not-exist" for f in confirmed)
    assert not any(f["code"] == "llm_unsupported_action" for f in confirmed)


def test_verifier_cannot_pass_an_empty_article(tmp_path):
    ctx = _ctx(tmp_path)
    items = _evidence_items()
    findings = (
        _rule_unsupported(ctx, "", items)
        + _rule_omissions(ctx, "", items)
    )
    assert findings, "an article with no content must produce findings"
    assert any(f["direction"] == "evidence->article" for f in findings)


# ----------------------------------------------------------------------
# JSON robustness


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n[1, 2]\n```') == [1, 2]


def test_repair_truncated_json():
    truncated = '{"observations": [{"kind": "action", "text": "do a"}, {"kind": "warn'
    repaired = repair_truncated_json(truncated)
    assert repaired is not None
    value = json.loads(repaired)
    assert value["observations"][0]["text"] == "do a"


def test_extract_json_recovers_truncated_reply():
    reply = """```json
{
  "observations": [
    {"kind": "action", "text": "install nginx", "confidence": 0.9},
    {"kind": "warning", "text": "this will overwr"""
    value = extract_json(reply)
    assert value["observations"][0]["text"] == "install nginx"


def test_extract_json_raises_when_no_json():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


# ----------------------------------------------------------------------
# findings scoping and evidence filtering


def test_findings_are_replaced_per_article(tmp_path):
    """A verification run must not merge findings from a previous article."""
    from yt2wiki.db.schema import json_dumps, json_loads
    from yt2wiki.pipeline.stages.verify import _persist

    ctx = _ctx(tmp_path)
    db = ctx.db
    article_id = "art-1"
    db.execute(
        "INSERT INTO sources(source_id, kind, created_at) VALUES('s','youtube',0)"
    )
    _persist(ctx, article_id, [{"direction": "rules", "severity": "critical",
                                "code": "value_mismatch", "message": "old"}])
    _persist(ctx, article_id, [{"direction": "rules", "severity": "warning",
                                "code": "omitted_action", "message": "new"}])
    rows = db.query("SELECT message FROM findings WHERE article_id=?", (article_id,))
    assert [r["message"] for r in rows] == ["new"]


def test_incidental_identifiers_are_filtered_out():
    from yt2wiki.pipeline.stages.evidence import _incidental_identifier
    from yt2wiki.records import new_evidence

    incidental = new_evidence("s", "identifier", "home", t_start=1, t_end=2)
    real = new_evidence("s", "identifier", "The hostname is host-9f21c", t_start=1, t_end=2)
    assert _incidental_identifier(incidental)
    assert not _incidental_identifier(real)
    assert not _incidental_identifier(new_evidence("s", "command", "ls", t_start=1, t_end=2))


def test_step_citations_suppress_unsupported_findings():
    from yt2wiki.pipeline.stages.verify import _step_citations

    md = """## Procedure

1. **Connect over ssh.**
   *Source: [0:04](https://youtu.be/x?t=4)*

2. **Do a thing with no citation.**
   *Expected: something.*
"""
    citations = _step_citations(md)
    assert citations.get(1) == 1
    assert 2 not in citations


def test_unsupported_rule_checks_cited_steps_against_cited_records(tmp_path):
    ctx = _ctx(tmp_path)
    md = """## Procedure

1. **Update the package index.**
   *Source: [0:04](https://youtu.be/x?t=4)*
"""
    findings = _rule_unsupported(ctx, md, _evidence_items())
    assert findings == []


def test_citation_to_unrelated_passage_does_not_excuse_claim(tmp_path):
    ctx = _ctx(tmp_path)
    md = """## Procedure

1. **Reinstall the kernel from scratch and repartition the disk.**
   *Source: [0:04](https://youtu.be/x?t=4)*
"""
    findings = _rule_unsupported(ctx, md, _evidence_items())
    assert any(f["code"] in ("unsupported_action", "mismatched_citation")
               for f in findings), findings


# ----------------------------------------------------------------------
# compose must not publish unsupported commands


def test_compose_suppresses_commands_without_evidence():
    from yt2wiki.pipeline.stages.compose import _collect_commands, _values_supported
    from yt2wiki.records import Procedure, ProcedureStep

    class _Ctx:
        artifacts: dict = {}
        source_id = "s"
        job_id = "j"

        def progress(self, *a, **k):
            return None

    evidence = _evidence_items()
    # no command literals in this fixture -> nothing may be published
    procedure = Procedure(
        procedure_id="p", title="t", summary="",
        steps=[ProcedureStep(position=1, text="Run dd if=/dev/zero of=/dev/sda",
                             detail="Run: dd if=/dev/zero of=/dev/sda")],
    )
    ctx = _Ctx()
    published = _collect_commands(ctx, evidence, procedure)
    assert published == []
    assert ctx.artifacts["suppressed_command_count"] >= 1
    assert _values_supported("dd if=/dev/zero", []) is False
    assert _values_supported("42 mA", ["42 mA"]) is True


def test_provenance_footer_is_not_audited_as_a_claim():
    from yt2wiki.pipeline.stages.verify import _rule_value_mismatch

    class _Ctx:
        def __init__(self):
            self.metrics = {}

        def record(self, **kw):
            self.metrics.update(kw)

    md = """# Title

Run `ls -la` and check the `4096` byte size.

## Source and provenance

- **Compiled with local profile:** `standard`
- **Configuration fingerprint:** `8ec6e438793bf40d`
"""
    ev = [Evidence(evidence_id="e1", kind="command", t_start=1.0, text="ls -la shows 4096 bytes",
                  frame_ids=[], literals=[Literal(literal="4096", kind="measurement")])]
    findings = _rule_value_mismatch(_Ctx(), md, ev, {"4096"})
    assert findings == []
