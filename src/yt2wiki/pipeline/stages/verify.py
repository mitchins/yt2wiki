"""Stage 9 — verification.

Two independent directions plus a rule layer:

* **article -> evidence** — every actionable statement in the article must be
  supported by a stored observation (unsupported actionable claim rate).
* **evidence -> article** — every critical observation must appear in the
  article (omission recall).  A fluent article that drops a warning is a failure.

The LLM verifier is a *different model family* from the composer and only ever
produces findings that rules then have to confirm; it is never treated as ground
truth.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

from ...backends.loader import ModelManager
from ...literals import extract_literals, normalize_value, values_match
from ...records import CRITICAL_KINDS, Evidence, Procedure
from .. import StageContext

log = logging.getLogger("yt2wiki.stage.verify")

# Canonical verifier direction label for findings sourced from the article
# side of the bidirectional check (see also the prompt schema below).
_DIR_ARTICLE_TO_EVIDENCE = "article->evidence"

PROMPT_VERSION = "v3"
SYSTEM = (
    "You audit a generated technical article against the observations extracted "
    "from its source video. You report problems only. You never rewrite the article."
)

AUDIT_PROMPT = """You are given:
1. A technical article that was generated from a tutorial video.
2. The observations that were extracted from that video, with timestamps.

Find defects. Be strict and specific.

Check:
(a) UNSUPPORTED: any instruction, command, value or warning in the article that
    is NOT backed by an observation.
(b) OMITTED: any critical observation (action, command, config, measurement,
    warning, prerequisite, correction, identifier) that is missing or materially
    weakened in the article.
(c) CONTRADICTION: two statements in the article that disagree, or a statement
    that contradicts an observation.
(d) EXACT VALUES: any value in the article that differs from the source value.

Citation system (this is important): the article cites its video evidence
with `*Source: [t](url&t=t)*` timestamp links, `(ev-xxxx)` evidence
references and `<!-- cite: ... -->` machine bindings.  A claim carrying
such a citation IS backed when the cited observation supports it.  Report
a missing link as code "missing_citation" (severity info), a wrong link as
"broken_citation" (severity warning), and only a claim with no supporting
observation anywhere as "unsupported_action" (or the matching code below).
Never ask for external websites, links or sources: the video observations
are the only ground truth.

Return JSON:
{{
  "findings": [
      {{"direction": "article->evidence" | "evidence->article", "severity": "critical"|"warning"|"info",
       "code": "unsupported_action|unsupported_claim|missing_citation|broken_citation|omitted_action|omitted_warning|value_mismatch|contradiction|unclear",
      "message": "one sentence naming the claim and the observation timestamp",
      "quote": "the article text at issue",
      "evidence_ref": "ev-... or ''"}}
  ],
  "overall": "pass" | "fail"
}}

ARTICLE:
\"\"\"
{article}
\"\"\"

OBSERVATIONS ({count}):
\"\"\"
{evidence}
\"\"\"

Return only JSON."""

_STEP_RE = re.compile(r"^(\d+)\.\s+(.*)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"```(?:bash|sh|ini|text)?\s*\n(.*?)```", re.DOTALL)
_WARNING_RE = re.compile(r">\s*\*\*Warning\*\*\s*—\s*(.+)", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\s*[<\[][^<>\[\]]*[>\]]")


def _strip_placeholders(value: str) -> str:
    """Placeholders (`<file>`, `[name]`) are not values, so they cannot support
    or contradict a claim.  Strip them before comparing."""
    return " ".join(_PLACEHOLDER_RE.sub(" ", str(value)).split())


_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is",
    "are", "be", "that", "this", "it", "as", "at", "by", "from", "you", "your",
    "will", "can", "if", "then", "so", "do", "not", "no",
}


def run(ctx: StageContext) -> dict[str, Any]:
    started = time.time()
    t = ctx.settings.tuning
    article_row = ctx.db.query_one(
        "SELECT * FROM articles WHERE article_id=?", (ctx.artifacts.get("article_id") or "",)
    )
    if article_row is None or not ctx.artifacts.get("article_id"):
        raise RuntimeError("no article stored; run the compose stage first")
    markdown = article_row["markdown"]
    article_id = ctx.artifacts["article_id"]

    evidence = _load_evidence(ctx)
    supported_values = _supported_values(evidence)

    findings: list[dict[str, Any]] = []

    if t.enable_rule_verification:
        findings.extend(_rule_unsupported(ctx, markdown, evidence))
        findings.extend(_rule_omissions(ctx, markdown, evidence))
        findings.extend(_rule_value_mismatch(ctx, markdown, evidence, supported_values))
        findings.extend(_rule_contradictions(markdown, evidence))

    llm_used = False
    if t.enable_llm_verification:
        try:
            with ModelManager(ctx.settings, recorder=_recorder(ctx)).acquire("verify") as backend:
                llm_findings = _llm_audit(ctx, backend, markdown, evidence)
                findings.extend(_confirm_llm_findings(llm_findings, evidence))
                llm_used = True
        except Exception as exc:  # noqa: BLE001
            log.exception("LLM verification failed")
            findings.append(
                {
                    "direction": "rules",
                    "severity": "warning",
                    "code": "verifier_unavailable",
                    "message": f"LLM verification could not run ({type(exc).__name__}: {exc}); "
                               "only rule-based checks were applied.",
                    "detail": {"exception": str(exc)[:400]},
                }
            )

    _persist(ctx, article_id, findings)
    summary = _summarise(findings)
    ctx.artifacts["verification"] = summary
    summary.update(
        {
            "llm_verifier_used": llm_used,
            "elapsed_s": round(time.time() - started, 3),
        }
    )
    return summary


# ----------------------------------------------------------------------
def _recorder(ctx: StageContext):
    def rec(entry: dict) -> None:
        entry.setdefault("stage", "verify")
        entry.setdefault("job_id", ctx.job_id)
        entry.setdefault("source_id", ctx.source_id)
        ctx.db.record_model_load(entry)

    return rec


def _load_evidence(ctx: StageContext) -> list[Evidence]:
    rows = ctx.db.query(
        "SELECT * FROM evidence WHERE source_id=? ORDER BY t_start", (ctx.source_id,)
    )
    return [Evidence.from_row(r) for r in rows]


def _supported_values(evidence: Sequence[Evidence]) -> set[str]:
    out: set[str] = set()
    for e in evidence:
        for lit in e.literals:
            out.add(normalize_value(lit.literal))
        for word in re.findall(r"[A-Za-z0-9_.:/@-]{3,}", e.text):
            out.add(normalize_value(word))
    return out


# ----------------------------------------------------------------------
# rule layer


def _article_steps(markdown: str) -> list[tuple[int, str]]:
    out = []
    for m in _STEP_RE.finditer(markdown):
        out.append((int(m.group(1)), m.group(2).strip()))
    return out


def _article_commands(markdown: str) -> list[str]:
    out: list[str] = []
    for block in _CODE_BLOCK_RE.finditer(markdown):
        for line in block.group(1).splitlines():
            s = line.strip()
            if s:
                out.append(s)
    return out


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_.:/@-]{3,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def _coverage(needle: str, haystack: str) -> float:
    """How much of `needle`'s content tokens appear in `haystack`."""
    tn, th = _tokens(needle), _tokens(haystack)
    if not tn:
        return 1.0
    return len(tn & th) / len(tn)


def _best_evidence(claim: str, evidence: Sequence[Evidence]) -> tuple[Evidence | None, float]:
    best: Evidence | None = None
    best_score = 0.0
    for e in evidence:
        haystack = e.text + " " + " ".join(lit.literal for lit in e.literals)
        score = max(_overlap(claim, haystack), _value_support(claim, e))
        if score > best_score:
            best, best_score = e, score
    return best, best_score


def _value_support(claim: str, e: Evidence) -> float:
    claim_vals = [lit.literal for lit in extract_literals(claim, max_values=8)]
    if not claim_vals:
        return 0.0
    source_vals = [lit.literal for lit in e.literals] + re.findall(r"[A-Za-z0-9_.:/@-]{3,}", e.text)
    hits = sum(1 for c in claim_vals if any(values_match(c, s) for s in source_vals))
    return hits / len(claim_vals)


_SOURCE_LINK_RE = re.compile(
    r"^\s*\*Source:\s*\[(\d+:\d{2}(?::\d{2})?)\]\(([^)]+)\)\*", re.MULTILINE
)


def _step_citations(markdown: str) -> dict[int, int]:
    """Count how many explicit *Source:* citation lines each step carries."""
    counts: dict[int, int] = {}
    step_num = 0
    for line in markdown.splitlines():
        m = re.match(r"^(\d+)\. ", line.strip())
        if m:
            step_num = int(m.group(1))
            continue
        if line.strip() == "":
            continue
        if step_num and _SOURCE_LINK_RE.match(line):
            counts[step_num] = counts.get(step_num, 0) + 1
    return counts


_CITE_COMMENT_RE = re.compile(
    r"<!--\s*cite:\s*step=(\S+)\s+ev=(\S+)\s+t=(\S+)\s+frames=(\S+)\s*-->"
)
# Sections whose callouts carry checkable claims (compose emits these names).
_INSTRUCTIONAL_SECTION_RE = re.compile(
    r"procedure|before you start|prereq|troubleshoot|warning|instruction|steps?",
    re.IGNORECASE,
)
_EV_REF_RE = re.compile(r"\((ev-[0-9a-f]{4,})\)")
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?;:])\s+|\n+")


def _parse_span(span: str) -> tuple[float | None, float | None]:
    try:
        if "-" in span:
            a, b = span.split("-", 1)
            return float(a), float(b)
    except ValueError:
        return None, None
    try:
        v = float(span)
        return v, v
    except ValueError:
        return None, None


def _claim_id(text: str) -> str:
    """Deterministic claim identifier: sha1 of the normalised sentence."""
    import hashlib

    norm = re.sub(r"\s+", " ", str(text).strip().lower())
    return "claim-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]



def _absorb_citation(cur: dict[str, Any] | None, line: str) -> None:
    """Add one line to a block, collecting its citations and time anchors."""
    assert cur is not None
    cur["lines"].append(line)
    cm = _CITE_COMMENT_RE.search(line)
    if cm:
        cur["ev_ids"].extend(i for i in cm.group(2).split(",") if i and i != "-")
        cur["t_spans"].append(_parse_span(cm.group(3)))
    for ev in _EV_REF_RE.findall(line):
        if ev not in cur["ev_ids"]:
            cur["ev_ids"].append(ev)
    for ts in re.findall(r"[?&]t=(\d+(?:\.\d+)?)", line):
        cur["t_spans"].append((float(ts), float(ts)))


def _heading_of(s: str) -> str | None:
    """Section title for a 1-3 hash heading, else None (no regex backtracking)."""
    n = len(s) - len(s.lstrip("#"))
    if 1 <= n <= 3 and n < len(s) and s[n].isspace():
        rest = s[n:].lstrip()
        return rest or None
    return None


def _numbered_of(s: str) -> int | None:
    """Step number for a leading `N.` item, else None (no regex backtracking)."""
    j = 0
    while j < len(s) and s[j].isdecimal():
        j += 1
    if j and j + 1 < len(s) and s[j] == "." and s[j + 1].isspace():
        return int(s[:j])
    return None


def _citation_blocks(markdown: str) -> list[dict[str, Any]]:
    """Split the article into checkable blocks with their citations.

    Block boundaries are numbered items and ## headings only: blank lines
    never split a step, so a step's sentences, callouts and Source lines
    stay in one block with one binding.  Each block carries its kind
    (step|callout|prose), section, step number, evidence IDs from
    `<!-- cite: -->` bindings and `(ev-...)` references, and timestamp spans
    from *Source:* links.  Blocks without citations resolve to an empty
    binding (checked against all evidence).
    """
    builder = _BlockBuilder()
    for line in markdown.splitlines():
        builder.feed(line)
    return builder.result()


class _BlockBuilder:
    """Accumulates citation blocks line by line (split of _citation_blocks).

    Owns the in-progress block, current section and step number that used
    to live in nested closures; each feeder handles one line shape.
    """

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.cur: dict[str, Any] | None = None
        self.section = ""
        self.step_num: int | None = None

    def flush(self) -> None:
        if self.cur and any(ln.strip() for ln in self.cur["lines"]):
            self.blocks.append(self.cur)
        self.cur = None

    def fresh(self, kind: str, step: int | None) -> dict[str, Any]:
        return {"kind": kind, "step": step, "section": self.section,
                "lines": [], "ev_ids": [], "t_spans": []}

    def feed_heading(self, heading: str) -> None:
        self.flush()
        self.section = heading
        self.step_num = None

    def feed_step(self, numbered: int, line: str) -> None:
        self.flush()
        self.step_num = numbered
        self.cur = self.fresh("step", self.step_num)
        _absorb_citation(self.cur, line)

    def feed_quote(self, line: str) -> None:
        # Callouts inside a step stay in its block (they inherit its
        # binding); standalone callouts form their own blocks.
        if self.cur is None or self.cur["kind"] != "step":
            if self.cur is None or self.cur["kind"] != "callout":
                self.flush()
                self.cur = self.fresh("callout", self.step_num)
        _absorb_citation(self.cur, line)

    def feed_prose(self, line: str) -> None:
        if self.cur is None:
            self.cur = self.fresh("prose", None)
        _absorb_citation(self.cur, line)

    def feed(self, line: str) -> None:
        s = line.strip()
        heading = _heading_of(s)
        if heading is not None:
            self.feed_heading(heading)
            return
        numbered = _numbered_of(s)
        if numbered is not None:
            self.feed_step(numbered, line)
            return
        if s.startswith(">"):
            self.feed_quote(line)
            return
        self.feed_prose(line)

    def result(self) -> list[dict[str, Any]]:
        self.flush()
        return self.blocks


def _sentences(text: str) -> list[str]:
    out = []
    # Citation lines are provenance, never claims.
    lines = [ln for ln in text.splitlines()
             if not _SOURCE_LINK_RE.match(ln.strip())
             and not ln.strip().startswith("<!--")]
    for part in _CLAIM_SPLIT_RE.split("\n".join(lines)):
        s = re.sub(r"\s+", " ", part).strip(" .;-")
        if len(s) < 12 or "http" in s:
            continue
        if re.fullmatch(r"\[.*\]\(.*\)\*?", s):
            continue  # link remnant from sentence splitting
        out.append(s)
    return out


def _sentence_support(sent: str, records: Sequence[Evidence]) -> float:
    """Best support for one sentence across the given records."""
    best = 0.0
    for e in records:
        haystack = e.text + " " + " ".join(lit.literal for lit in e.literals)
        score = _coverage(sent, haystack)
        if e.literals:
            score = max(score, _value_support(sent, e))
        best = max(best, score)
    return best


def _rule_unsupported(ctx: StageContext, markdown: str, evidence: Sequence[Evidence]) -> list[dict]:
    """Article -> evidence, per material claim.

    Every sentence of every numbered step and warning callout is checked.
    A sentence supported by the records its own block cites is silent; a
    sentence supported elsewhere points at a mismatched citation; a sentence
    supported nowhere is unsupported; a supported sentence in an uncited
    block is a missing-citation (info).  Scope/prerequisite prose is left
    to the omission direction and the LLM layer.
    """
    out: list[dict] = []
    by_id = {e.evidence_id: e for e in evidence}
    for block in _citation_blocks(markdown):
        if block["kind"] not in ("step", "callout"):
            continue
        if block["kind"] == "callout" and block["step"] is None \
                and not _INSTRUCTIONAL_SECTION_RE.search(block["section"] or ""):
            continue  # top-matter epigraphs, not claims
        bound, broken, span_recs = _block_bindings(block, by_id, evidence)
        label = f"Step {block['step']}" if block["step"] is not None else "Callout"
        for b in broken:
            out.append(_broken_citation_finding(label, block, b))
        check = _BlockCheck(label=label, block=block, bound=bound,
                            span_recs=span_recs, evidence=evidence)
        text = _checkable_text(block)
        for sent in _sentences(text):
            out.extend(_sentence_findings(check, sent))
    out.extend(_unsupported_command_findings(markdown, evidence))
    return out


class _BlockCheck:
    """Per-block context for the sentence support ladder."""

    def __init__(self, *, label: str, block: dict[str, Any],
                 bound: list[Evidence], span_recs: list[Evidence],
                 evidence: Sequence[Evidence]) -> None:
        self.label = label
        self.block = block
        self.bound = bound
        self.span_recs = span_recs
        self.evidence = evidence

    @property
    def anchored(self) -> list[Evidence]:
        """Records the block's own citations anchor it to (may be empty)."""
        return self.bound or self.span_recs

    @property
    def cite_ids(self) -> list[str]:
        return [e.evidence_id for e in self.anchored]


def _block_bindings(block: dict[str, Any], by_id: dict[str, Evidence],
                    evidence: Sequence[Evidence]) -> tuple[list[Evidence], list[str], list[Evidence]]:
    """(bound records, broken citation ids, time-window records) for a block."""
    bound = [by_id[i] for i in block["ev_ids"] if i in by_id]
    broken = [i for i in block["ev_ids"] if i not in by_id]
    span_recs: list[Evidence] = []
    for t0, t1 in block["t_spans"]:
        if t0 is None:
            continue
        lo = min(t0, t1 if t1 is not None else t0) - 45.0
        hi = max(t0, t1 if t1 is not None else t0) + 45.0
        span_recs.extend(e for e in evidence
                         if e.t_start is not None and lo <= e.t_start <= hi)
    return bound, broken, span_recs


def _broken_citation_finding(label: str, block: dict[str, Any], b: str) -> dict:
    return {
        "direction": _DIR_ARTICLE_TO_EVIDENCE,
        "severity": "warning",
        "code": "broken_citation",
        "message": f"{label} cites evidence record {b}, which does not exist.",
        "detail": {"step": block["step"], "claim_id": _claim_id(b),
                    "citation": b},
    }


def _checkable_text(block: dict[str, Any]) -> str:
    return "\n".join(ln for ln in block["lines"]
                     if ln.strip() and not ln.strip().startswith("<!--")
                     and not _SOURCE_LINK_RE.match(ln.strip()))


def _sentence_findings(ctx: _BlockCheck, sent: str) -> list[dict]:
    """The support ladder for one sentence (matching the original order)."""
    block = ctx.block
    claim = {"step": block["step"], "claim_id": _claim_id(sent), "text": sent[:300]}
    score = _sentence_support(sent, ctx.anchored) if ctx.anchored \
        else _sentence_support(sent, ctx.evidence)
    if score >= 0.2:
        if not (ctx.anchored or block["ev_ids"]):
            return [{
                "direction": _DIR_ARTICLE_TO_EVIDENCE,
                "severity": "info",
                "code": "missing_citation",
                "message": f"{ctx.label} states a supported claim with no citation.",
                "detail": {**claim, "support": round(score, 3)},
            }]
        return []
    if ctx.anchored:
        elsewhere, escore = _best_evidence(sent, ctx.evidence)
        if escore >= 0.2 and elsewhere is not None and \
                elsewhere.evidence_id not in ctx.cite_ids:
            return [{
                "direction": _DIR_ARTICLE_TO_EVIDENCE,
                "severity": "warning",
                "code": "mismatched_citation",
                "message": (
                    f"{ctx.label} links a passage that does not support the claim; "
                    f"the claim is supported by {elsewhere.evidence_id} instead."
                ),
                "detail": {**claim, "cited": ctx.cite_ids,
                            "supported_by": elsewhere.evidence_id},
            }]
    return [{
        "direction": _DIR_ARTICLE_TO_EVIDENCE,
        "severity": "critical" if score < 0.06 else "warning",
        "code": "unsupported_action",
        "message": (
            f"{ctx.label} does not map onto any recorded observation "
            f"(best overlap {score:.2f})."
        ),
        "detail": {**claim, "best_score": round(score, 3)},
    }]


def _unsupported_command_findings(markdown: str,
                                  evidence: Sequence[Evidence]) -> list[dict]:
    out: list[dict] = []
    for cmd in _article_commands(markdown):
        if len(cmd) < 4:
            continue
        bare = _strip_placeholders(cmd)
        if len(bare) < 2:
            continue
        supported = any(
            values_match(cmd, lit.literal)
            or values_match(bare, _strip_placeholders(lit.literal))
            or _overlap(bare, lit.literal) > 0.7
            for e in evidence for lit in e.literals
        )
        if not supported:
            out.append(
                {
                    "direction": _DIR_ARTICLE_TO_EVIDENCE,
                    "severity": "critical",
                    "code": "unsupported_command",
                    "message": f"Command in article has no supporting observation: `{cmd[:120]}`",
                    "detail": {"command": cmd[:200]},
                }
            )
    return out


def _rule_contradictions(markdown: str, evidence: Sequence[Evidence]) -> list[dict]:
    """Article -> evidence: the same key asserted with a different value.

    Only exact key=value assertions count (code spans and fenced blocks),
    only against active records, and never when the source itself asserts
    several values for the key (ambiguity is not contradiction).
    """
    asserted = _article_keyvalues(markdown)
    by_key, key_source = _evidence_keyvalues(evidence)
    out: list[dict] = []
    for key, value in asserted.items():
        sources = by_key.get(key, set())
        if not sources or len(sources) != 1:
            continue  # no source assertion, or the source itself is ambiguous
        want = next(iter(sources))
        if values_match(value, want):
            continue
        src = key_source[key][want]
        out.append(
            {
                "direction": _DIR_ARTICLE_TO_EVIDENCE,
                "severity": "critical",
                "code": "contradiction",
                "message": (
                    f"Article asserts {key}={value} but the source records "
                    f"{key}={want} ({src.evidence_id})."
                ),
                "detail": {"key": key, "article_value": value,
                            "source_value": want,
                            "evidence_id": src.evidence_id,
                            "claim_id": _claim_id(f"{key}={value}")},
            }
        )
    return out


_KEY_RE = r"[A-Za-z_][\w.\-]*"


def _clean_keyvalue(lit) -> tuple[str, str] | None:
    """A valid `key=value` pair from a literal, else None."""
    if "=" not in lit.literal:
        return None
    k, v = lit.literal.split("=", 1)
    k = k.strip()
    if not re.fullmatch(_KEY_RE, k) or len(k) < 3 or not v.strip():
        return None
    return normalize_value(k).lower(), v.strip()


def _article_keyvalues(markdown: str) -> dict[str, str]:
    """First assertion wins: code spans, then fenced blocks line by line."""
    def keyvalues(text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for lit in extract_literals(text, max_values=6):
            kv = _clean_keyvalue(lit)
            if kv is not None:
                found.setdefault(kv[0], kv[1])
        return found

    asserted: dict[str, str] = {}
    for m in re.finditer(r"`([^`\n]{2,120})`", markdown):
        asserted.update({k: v for k, v in keyvalues(m.group(1)).items()
                         if k not in asserted})
    for block in _CODE_BLOCK_RE.finditer(markdown):
        for line in block.group(1).splitlines():
            for k, v in keyvalues(line.strip()).items():
                asserted.setdefault(k, v)
    return asserted


def _evidence_keyvalues(evidence: Sequence[Evidence]) -> tuple[dict[str, set[str]], dict[str, dict[str, Evidence]]]:
    """Every value each active evidence record asserts for a key."""
    by_key: dict[str, set[str]] = {}
    key_source: dict[str, dict[str, Evidence]] = {}
    for e in evidence:
        if e.status != "active":
            continue
        for lit in e.literals:
            kv = _clean_keyvalue(lit)
            if kv is None:
                continue
            kk, vv = kv
            by_key.setdefault(kk, set()).add(normalize_value(vv))
            key_source.setdefault(kk, {}).setdefault(normalize_value(vv), e)
    return by_key, key_source


def _rule_omissions(ctx: StageContext, markdown: str, evidence: Sequence[Evidence]) -> list[dict]:
    out: list[dict] = []
    article_blob = _tokens(markdown)
    for e in evidence:
        if e.kind not in CRITICAL_KINDS:
            continue
        if _coverage(e.text, markdown) >= 0.60:
            continue
        literal_hits = sum(
            1 for lit in e.literals if re.search(re.escape(lit.literal), markdown, re.IGNORECASE)
        )
        if e.literals and literal_hits / len(e.literals) >= 0.6:
            continue
        if e.kind in ("warning", "prerequisite", "correction") and literal_hits:
            continue
        out.append(
            {
                "direction": "evidence->article",
                "severity": "critical" if e.kind in ("warning", "command", "measurement") else "warning",
                "code": f"omitted_{e.kind}",
                "message": (
                    f"Critical {e.kind} observation at {e.t_start:.0f}s is missing from the article: "
                    f"{e.text[:160]}"
                ),
                "detail": {
                    "evidence_id": e.evidence_id,
                    "kind": e.kind,
                    "t_start": e.t_start,
                    "text": e.text[:300],
                    "overlap": round(_coverage(e.text, markdown), 3),
                },
            }
        )
    return out


def _rule_value_mismatch(
    ctx: StageContext, markdown: str, evidence: Sequence[Evidence], supported: set[str]
) -> list[dict]:
    out: list[dict] = []
    body = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    # The provenance footer carries bookkeeping values (profile name, config
    # fingerprint) that are deliberately not source claims; do not audit them.
    body = re.sub(r"(?ms)^##\s+Source and provenance\s*$.*", "", body)
    seen: set[str] = set()
    for m in re.finditer(r"`([^`\n]{2,80})`", body):
        value = m.group(1).strip()
        if not any(ch.isdigit() for ch in value):
            continue
        norm = normalize_value(value)
        if norm.lower() in seen:
            continue
        seen.add(norm.lower())
        if any(values_match(value, s) for s in supported):
            continue
        out.append(
            {
                "direction": _DIR_ARTICLE_TO_EVIDENCE,
                "severity": "critical",
                "code": "value_mismatch",
                "message": f"Article states `{value}` which does not match any value in the source.",
                "detail": {"value": value},
            }
        )
    return out


# ----------------------------------------------------------------------
# LLM layer


def _llm_audit(ctx: StageContext, backend, markdown: str, evidence: Sequence[Evidence]) -> list[dict]:
    digest = [
        {
            "evidence_id": e.evidence_id,
            "kind": e.kind,
            "text": e.text,
            "t": round(e.t_start or 0, 1),
            "literals": [lit.literal for lit in e.literals][:6],
        }
        for e in evidence
    ]
    prompt = AUDIT_PROMPT.format(
        article=markdown[:9000],
        evidence=json.dumps(digest, ensure_ascii=False)[:18000],
        count=len(digest),
    )
    try:
        payload = backend.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            max_new_tokens=int(ctx.settings.tuning.verify_max_tokens),
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("llm audit failed: %s", exc)
        return []
    findings = payload.get("findings") or []
    return [f for f in findings if isinstance(f, dict)]


def _confirm_llm_findings(raw: Sequence[dict], evidence: Sequence[Evidence]) -> list[dict]:
    """An LLM finding only counts when a rule can corroborate it.

    This is the point at which the second model is *not* trusted: it supplies
    candidates, the evidence decides.  A corroborated claim that is already
    cited is dropped (not a finding); a corroborated claim without a citation
    becomes missing_citation; a citation pointing nowhere becomes
    broken_citation instead of vanishing silently.
    """
    confirmed: list[dict] = []
    valid_ids = {e.evidence_id for e in evidence}
    for f in raw:
        ref = str(f.get("evidence_ref") or "")
        quoted = str(f.get("quote") or f.get("message") or "")
        if ref and ref not in valid_ids:
            confirmed.append(
                {
                    "direction": str(f.get("direction") or _DIR_ARTICLE_TO_EVIDENCE),
                    "severity": "warning",
                    "code": "broken_citation",
                    "message": (
                        f"The article links evidence record {ref}, which does not exist: "
                        f"{quoted[:200]}"
                    ),
                    "detail": {"quote": quoted[:300], "citation": ref,
                                "origin": "llm-verifier"},
                }
            )
            continue
        supported = bool(ref) or _has_rule_basis(quoted, evidence)
        if not supported:
            continue
        confirmed.append(
            {
                "direction": str(f.get("direction") or _DIR_ARTICLE_TO_EVIDENCE),
                "severity": str(f.get("severity") or "warning"),
                "code": f"llm_{f.get('code') or 'unspecified'}",
                "message": str(f.get("message") or "")[:400],
                "detail": {
                    "quote": quoted[:300],
                    "evidence_ref": ref,
                    "origin": "llm-verifier",
                    "corroborated": True,
                },
            }
        )
    return confirmed


def _has_rule_basis(quote: str, evidence: Sequence[Evidence]) -> bool:
    if not quote:
        return False
    best, score = _best_evidence(quote, evidence)
    return score <= 0.15 or best is None


# ----------------------------------------------------------------------
def _persist(ctx: StageContext, article_id: str, findings: Sequence[dict]) -> None:
    from ...db.schema import json_dumps
    import time as _time

    with ctx.db.transaction() as conn:
        # Findings describe *this* article.  Never merge them with a previous
        # run's, or the report becomes a union of contradictory verdicts.
        conn.execute("DELETE FROM findings WHERE article_id=?", (article_id,))
        for f in findings:
            fid = f"find-{_hash(f)}"
            conn.execute(
                """INSERT OR REPLACE INTO findings(finding_id, article_id, job_id, source_id,
                       direction, severity, code, message, detail_json, resolved, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fid, article_id, ctx.job_id, ctx.source_id,
                    f.get("direction", "rules"), f.get("severity", "info"),
                    f.get("code", "unspecified"), f.get("message", "")[:1000],
                    json_dumps(f.get("detail", {})), 1 if f.get("resolved") else 0, _time.time(),
                ),
            )
    _write_report(ctx, findings)


def _hash(f: dict) -> str:
    import hashlib

    blob = f"{f.get('code')}|{f.get('message')}"
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _write_report(ctx: StageContext, findings: Sequence[dict]) -> None:
    out = ctx.work_dir("verify") / "findings.json"
    out.write_text(json.dumps(list(findings), ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.artifacts["findings_json"] = str(out)


def _summarise(findings: Sequence[dict]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.get("code", "unspecified")] = counts.get(f.get("code", "unspecified"), 0) + 1
    critical = sum(1 for f in findings if f.get("severity") == "critical")
    warnings = sum(1 for f in findings if f.get("severity") == "warning")
    return {
        "findings": len(findings),
        "critical": critical,
        "warnings": warnings,
        "by_code": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "verdict": "fail" if critical else ("pass_with_warnings" if warnings else "pass"),
    }
