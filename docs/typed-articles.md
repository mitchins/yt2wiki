# Typed articles (V9 editorial path)

A video becomes the *kind of useful article its content supports* — procedure,
recipe, report, explainer, review, discussion or experiment — without asking
the operator to pick a template. The agent selects one versioned article type
per generation, plans within that type's slot contract, and validates the
result against type-aware checks before publication. `no_article` (music-only,
no usable evidence) is a refusal result, not a type.

## The seven types

| Type | Reader promise | Lead component |
|---|---|---|
| `procedure` | A reproducible task | — (numbered steps) |
| `recipe` | Cookable dish, exact measures | Ingredient list |
| `report` | What happened, when, as known then | Key facts |
| `explainer` | Understand a subject | Key facts |
| `review` | An evaluation (verdict is the reviewer's) | Verdict card |
| `discussion` | Viewpoints and disagreements | Position list |
| `experiment` | A test and what it establishes | Measured comparison |

Type names and slot contracts are versioned (`template_version: 1`) and
immutable at runtime. Historical kinds `experimental` (alias of `experiment`),
`comparison` and `teardown` (patterns within review/explainer/experiment)
keep their old reader labels; anything untyped shows a neutral `Article` badge.

## Pipeline (all gated until qualified)

V9 runs the typed path only when the job is started with the typed flag
(currently an explicit `enable_typed` run option; the web route runs legacy
V9 until the flag is enabled after qualification). Stages:

1. `v9_plan` — bounded transcript scout (whole-video windows, per-window
   digests, ad/housekeeping/music flags), one frozen JSON type decision
   against the closed enum, one locked multi-window plan. Empty or
   music-only sources refuse here without model calls.
2. `v9_write` — per-type writer prompts over each section's own admitted
   window union (never the enclosing min–max interval), bounded retries,
   request-shaped lead block (missing lead is non-fatal).
3. `v9_discover` / `v9_select` — image ranking filtered to the union of
   admitted section windows.
4. `v9_validate` — deterministic type-aware gate (see below). `ready`
   publishes; `needs_review` keeps a private candidate plus a finding
   ledger under the job's stage directory and finishes without touching
   any published article; `refused` publishes nothing.
5. `v9_caption` / `v9_publish` — as before, plus the stored lead Markdown
   (so exports carry the same lead as the reader), manifest `editorial`
   (selection, plan, index hash, lead blocks, validation) and the
   `articles.article_type` badge source. An untyped republish never clears
   a recorded type.

## The gate

`src/yt2wiki/v9/validate.py` runs on the same normalized bodies the reader
sees: slot invariants, executable-block rules (only procedures carry them),
exact-value support per section span (quantities, prices, names, measures —
word numerals, indefinite articles before hour-plus durations ("after a
year" is one year) and unit spellings normalised symmetrically),
proper-name support (near-miss spellings are critical, plural-only style
is major, no transcript neighbour starts as major for operator
adjudication), span bounds, excluded-span quarantine, coverage
accounting, reader-integrity (no internal ids, raw dicts or model HTML),
procedure step ordering (use-before-define), and prompt-injection echo
rejection. Entity triage is semantic, not lexical (gate v2,
`VALIDATOR_VERSION = 2`): every `unattested_entity` candidate goes
through bounded evidence-grounded entailment — entailed-with-verbatim-
quote discharges with rationale recorded; uncertain stays for human
review with evidence; contradicted blocks; probe failure retains the
lexical finding. Only `critical` findings and genuinely uncertain
semantic verifications block publication; `major` findings are recorded
in the report and manifest.

Exact domestic values live in `src/yt2wiki/v9/values.py`, which reuses
`yt2wiki.literals` normalization and adds cooking measures, fractions,
currency and comma-thousands. The shared extractor is untouched.

## Operator reading

- Type badge + `Needs review` chip on the article; lead component under the
  lede; "About this article" provenance panel with model identities.
- Job progress shows the `v9_plan` / `v9_validate` stages; validation
  findings are in `v9_report.json` and the generation manifest.
- Review candidates (`needs_review`) are private files under the job's
  `v9_validate` stage directory — never served as articles.

## Qualification status

MERGED and qualified on `main`: PR #30 (contracts + reader), PR #31
(editorial pass), PR #32 (validation + matrix), PR #33 (`--typed`
operator entry, default off), PR #34 (gate v2 semantic verification,
`VALIDATOR_VERSION = 2`). Ten-video qualification corpus
(`data/qual-shapes-01`, private): five genres, same cached sources and
pinned model for baseline and candidate. Full per-case ledger in the
PR C report.

Canary-01 (private, `data/canary-01`, CLOSED BY OPERATOR at 7 of 10
attempted; first-pass records frozen): 5 articles approved through
operator gates (3 intervention-free), 2 faithful-but-unverifiable
candidates correctly held (one exact-value fabrication, one
verbatim-literalism false-positive pair later discharged by gate v2
replays). Disposition: typed contracts and autonomous classification
COMPLETE; source-fidelity gate v2 QUALIFIED; human-reviewed typed
publishing READY; unattended publishing NOT QUALIFIED.

- Published with zero critical findings: DART `report` (v10,
  verification-fix revision — see below), Vox `report`, Hermes
  `procedure`, Git `explainer` (type dissent, see below),
  LTT `review`, DF `report` (v2, verification-fix revision), WVFRM
  `discussion`, recipe `recipe` (via evidenced multi-span synthesis,
  see below).
- Correct refusal: silent montage (no speech; transcribe fails closed).
- Close-01 verification fixes (recorded as `verification-fix`
  revisions, pre-fix content restorable): DART lost a caption-mislabeled
  speaker attribution (Fauci), a wrong orbit body (Didymos for
  Dimorphos), an invented surname (O'Shea for O'Brien), an unverified
  descriptor and twelve mangled timestamp citations (rebuilt from
  their correct `t=` targets); DF lost one caption-counter artifact
  (`133` quoted as content). Post-fix offline revalidation with the
  real plans: DART ready/zero findings, DF ready/one major.
- Long-form limitation: the 97 min Dwarkesh discussion still fails
  section writes stochastically (server-side empty replies on dense
  multi-window spans despite strided evidence, compact JSON,
  halved-evidence + minimal-schema retry at varied temperature and a
  plain-chat/guided fallback ladder — each probed in isolation).
  Isolated, demonstrated blocker; writer-side decomposition beyond
  the two-attempt contract is future work, explicitly out of scope.
- Caption-counter hygiene: the writer strips one leading counter per
  evidence line; the normaliser rebuilds mangled labels from honest
  `t=` targets; mid-text counters are a documented residual risk.
- Open dissent: the Git how-to classifies `explainer` across four
  independent decisions (commands preserved in order and syntax);
  the matrix expected `procedure`. Operator decides whether to amend
  the matrix or steer harder.

Gate contract (enforced, not aspirational): a value with *no*
transcript span anywhere is critical and blocks publication; a
value stated elsewhere than the citing span is a major
misplaced-span finding (fix the citation, not a fabrication); only
critical findings block. Frontmatter (title/summary/lead) is checked
against full-transcript evidence minus excluded spans; review
candidates never overwrite published articles; untyped republishes
never clear a recorded type.

Known gate limits (documented, not silent): transcript-scoped
section evidence (visual-only claims need a human); decades conflate
centuries only in explicitly ambiguous forms; bare integers are
checked as values; no second production model; remote writer is
pinned gemma4 (Halogen untyped here, reachable but unqualified);
bare `in` and definition-list colons are not values.
