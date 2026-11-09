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
word numerals and unit spellings normalised symmetrically), proper-name
support (near-miss spellings are critical, plural-only style is major, no
transcript neighbour is major for operator adjudication), span bounds,
excluded-span quarantine, coverage accounting, reader-integrity (no internal
ids, raw dicts or model HTML), procedure step ordering (use-before-define),
and prompt-injection echo rejection. Only `critical` findings block
publication; `major` findings are recorded in the report and manifest.

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

DRAFT stack, unmerged: PR A (contracts + reader), PR B (editorial pass),
PR C (validation + matrix). Ten-video qualification corpus
(`data/qual-shapes-01`, private): five genres, same cached sources and
pinned model for baseline and candidate. Full per-case ledger in the
PR C report.

- Published: Hermes `procedure`, Git `explainer` (type dissent, see
  below), LTT `review`, DART `report` — zero critical findings.
- `needs_review` with private candidate kept, published article
  untouched: recipe (unverifiable inferred count), Vox (cross-span
  decade claim), product-b (chart number with no narration, converted
  units). Each is a replan-away-from-failure, not a dead end: the
  gate names the exact span/value to fix.
- Correct refusal: silent montage (no speech; transcribe fails closed).
- Long-form limitation: 73-97 min discussions intermittently fail at
  plan/write (mega-spans, writer truncation); chunked planning,
  auto-split and strided evidence mitigate, retries close the rest.
- Open dissent: the Git how-to classifies `explainer` across four
  independent decisions (commands preserved in order and syntax);
  the matrix expected `procedure`. Operator decides whether to amend
  the matrix or steer harder.

Known gate limits (documented, not silent): transcript-scoped
evidence (visual-only claims need a human); whole-transcript
attribution is substring-exact per token; decades conflate
centuries only in explicitly ambiguous forms; no second production
model; remote writer is pinned gemma4 (Halogen untested here).
