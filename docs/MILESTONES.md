# Milestone reports

Format per milestone: what works, what was tested, what failed, quantitative
results, decisions and their evidence, next work.

---

## M1 — End-to-end vertical slice — **complete**

### What works

A 10-minute screencast goes from a file on disk to a published, illustrated
article with all ten stages succeeding in one unattended run:

```
acquisition 0.2s · inspect 0.5s · transcribe 87.3s · frame_candidates 19.3s
visual_interpret 557.5s · evidence 578.6s · procedures 91.6s · compose 10.4s
verify 107.0s · publish 0.1s      total 1447s
```

- No mock responses: every stage calls a real local model.
- Article contains title, scope, prerequisites, 17 numbered steps with copyable
  commands and expected outcomes, a warnings section, a frame gallery, a
  commands appendix, and full provenance.
- Restartability verified: killing a run mid-way and restarting resumes from
  the last completed stage; `yt2wiki run --stages …` runs a subset.

### What was tested

`tests/` — 72 tests, no network, no weights, no GPU:

- paths and content addressing (YouTube URL forms, upload hashing)
- exact-value extraction (versions, units, paths, ports, IPs, MACs, commands)
  including a precision test that prose is *not* captured as a command
- ffmpeg probes, audio extraction, scene detection, frame extraction, WAV
  round-trip
- perceptual-hash signature streaming and deduplication
- Markdown → HTML rendering, self-contained embedding, escaping
- evidence record stability, transcript windowing/overlap
- bidirectional verification rules in both directions
- `extract_json` truncation repair
- SQLite schema, FTS5 search, per-thread connections, dataclass serialisation
- service layer, job creation, cancel
- FastAPI routes including asset traversal rejection
- GPU headroom planning

### What failed (and was fixed)

| Failure | Root cause | Fix |
|---|---|---|
| CUDA kernel explosion on torch | `nvidia-cuda-nvrtc-cu12` shadowed torch's cu13 nvrtc | do not install the cu12 nvrtc |
| `libcublas.so.12` missing for CTranslate2 | venv had cu13 wheels only | install the three cu12 nvidia libs |
| `metadata_errors` crash in faster-whisper | PyAV 19 dropped the kwarg faster-whisper 1.2.1 passes | decode the normalised WAV directly |
| 378 evidence windows instead of 9 | overlap carry accumulated negative duration | accumulate backwards from the boundary |
| `Literal` objects written to SQLite as strings | `json.dumps(..., default=str)` | `default=` expands dataclasses |
| whole library retranscribed after a prompt tweak | fingerprint covered the entire tuning object | `pipeline.STAGE_TUNING_KEYS` |
| partial runs invalidated downstream fingerprints | only the requested stages loaded their artifacts | load artifacts for every completed stage |
| 20 s per interpreted frame | one forward pass per image | real batching + separate image/output token budgets |
| `Run: <cmd>` commands in the article | command detection used substring matching | `yt2wiki/commands.py` whitelist + cue-aware extraction |

### Quantitative results

See `docs/MEASUREMENTS.md`. Verification on the first complete article reported
64 findings (25 critical) and a `fail` verdict, correctly identifying that
on-screen identifiers were dropped and commands carried narration prefixes.

### Decisions and their evidence

- **Specialised models per role rather than one multimodal model.** The 2B VLM
  at a 512-token budget read `ssh root@96.126.117.9` from a browser screenshot
  and the terminal sessions from the same video; a single large model would
  have cost more VRAM than the whole 16 GB budget.
- **Batching before optimisation.** Batching gave a 3.6× end-to-end win on
  frame interpretation for ~20 lines of code.
- **Rules-first verification.** The LLM verifier corroborated only 1 of 64
  findings; the rest came from rules that a second model cannot reproduce or
  argue with.
- **Deterministic article skeleton.** Section selection is driven by which
  evidence kinds are present, so a purely explanatory video gets no
  "Procedure" section at all.

### Next work

1. Force a fresh visual pass so the repaired JSON parser produces clean frame
   captions (this run).
2. Raise the 120-observation procedure digest cap — it is a real correctness
   gap for videos with more observations.
3. M2: strengthen exact-value protection and contradiction handling.
4. M4: run the three-config evaluation.

---

## M2 — Evidence integrity — **complete (with recorded caveats)**

### What works

- Every evidence record carries its timestamp span, the frames that support it,
  and the extracted literals with their `literal`/`normalized` split.
- Observations are stored separately from generated prose: the article is
  rendered from `procedure_steps`, never the reverse.
- Bidirectional verification runs on every article and writes findings into
  `findings` with a severity and a machine-readable code.
- The LLM verifier uses a different model family (SmolLM3-3B) and cannot
  contribute a finding without a corroborating rule.
- Superseded observations are marked rather than merged away.

### What was tested

`tests/test_verification.py` — 14 tests:
a clean article yields no critical findings; an invented command/action is
flagged in both directions; a warning omitted from a fluent article is flagged;
an invented numeric value is flagged; the model's value in the source is *not*
flagged; LLM findings without rule corroboration are dropped; an empty article
always produces findings.

### What was added after M2

- **Cache correctness bug found and fixed:** a downstream stage's fingerprint
  only covered configuration and *input file* hashes, so regenerating a
  procedure left compose serving a stale article. Stages now record the content
  hash of their own primary artifact (`StageContext.record_asset_hash`).
- **Verification scope bug:** findings from a previous run were merged with the
  current one. Each verification run now replaces the article's findings.
- **Placeholder handling in the value check:** `<file>` and `[name]` are not
  values, so they are stripped before comparing a command to its evidence.
- **Unsupported commands are suppressed, not published.** `_collect_commands`
  only emits commands with a supporting observation, and counts what it
  dropped.
- **Steps without a *timestamped* citation are demoted**, not published: the
  step moves into "Unresolved details" explaining the source demonstrated
  something the extractor could not anchor.

### Verification yield progression

| Run | findings | critical |
|---|---|---|
| initial | 64 | 25 |
| after command-literal precision | 31 | 7 |
| after incidental-identifier filter | 40 | 26 |
| after placeholder normalisation | 18 | 9 |
| final | 42 | 10 |

Every reduction came from fixing a root cause; none came from loosening the
verifier.

### Remaining caveats

- The evidence→article omission rule is lexical; a paraphrase can still slip
  past. An embedding/NLI check is the fix.
- Contradiction adjudication is limited to 12 rule-detected pairs per video.
- LLM variance in the procedure stage means step counts fluctuate between runs
  (34 and 42 on the same video); the coverage-extension pass partly compensates
  but does not make the count stable.

---

## M3 — Visual intelligence — **partially complete**

### What works

- Full-timeline coverage from one cheap signature pass (1 fps, 576 B/s).
- Screen-state change detection catches screencasts that never hard-cut.
- Second higher-resolution tiled read for dense frames.
- Frames are deduplicated perceptually, not just by time proximity.

### Numbers

19 500 frames → 53 candidates (0.27%), 5.1 MB, in 17 s of CPU work.
53 frames interpreted in 557 s including dense passes.

### Open

- Small on-screen text: `touch -d tomorrow CreateAVMforONLY0075centsanhour.txt`
  was read as `touch -d tomorrow CreateAVMfORONLY075centsanhour.txt` by the 4B
  model. The enhanced profile's 640-token budget and the FP8-8B model are the
  obvious candidates and have not been benchmarked.
- Long identifiers, resistor colour codes, register names: not yet qualified.

---

## M4 — Evaluation and model qualification — **not started**

Harness exists and is unit-tested (`tests/test_eval.py`, 9 tests):

- `eval/manifest.py` — 24 videos across 9 required categories with a rationale
  per item.
- `eval/build_references.py` — builds reference annotations from each source's
  own chapters and captions, never from pipeline output; 24 annotations are on
  disk, all `reviewed: false`.
- `eval/metrics.py` — all eight required metrics plus per-category breakdown and
  recorded failures.
- `eval/run_eval.py` — configurations A (transcript only), B (transcript +
  periodic screenshots), C (full pipeline) using real tuning knobs, not
  stubs.

**No numbers have been produced yet.** Until they are, no model or profile is
"qualified".

---

## M6 — Paired video–article benchmark — **validity gate PASSED (M6-Q)**

`eval/paired/M6Q_DECISION.md` records the PASS with evidence. Summary:

- Original five pairs audited: 0 FULL, 1 PARTIAL, 4 RELATED (diagnostics).
- Qualified set: 5 FULL_COMPANION candidates (Geerling ×4, Fireship ×1)
  with frozen snapshots and fitness manifest.
- v2 scorer: localised timestamped support, N/A integrity, evidence-linked
  images, pipeline findings separated as labelled self-audit; 15
  adversarial fixtures; v1 frozen under `m6-scorer-v1`.
- Empirical confirmation: jg-truenas end-to-end on the compact profile /
  8 GB card (50 min wall), correspondence audited fact by fact.
- Diagnostic handoff: the 13 "unsupported" steps all trace to evidence
  records — a citation-linkage gap in composition, not fabrication.
  Verifier-strictness vs compose-linkage is the first post-gate experiment.

Harness is implemented, unit-tested (`tests/test_paired.py`, 8 tests), and
committed.  Five Thomas Frank Notion tutorials pair each YouTube video with
its companion written guide:

- `eval/paired/dataset.py` — the five pairs (58/45/40/15/20 min of video).
- `eval/paired/extract_reference.py` — immutable article snapshots plus
  structured facts (commands, configs, values, tools, actions, warnings,
  outcomes, images); facts classified video-supported / article-only /
  uncertain from source-derived video text only.
- `eval/paired/compare.py` — deterministic 8-dimension scoring; article-only
  facts are excluded from failure scoring, uncertain facts are diagnostic.
- `eval/paired/metrics.py` — aggregation, paired per-video deltas, failure
  taxonomy.
- `eval/paired/run.py` — unattended collect → generate → classify → compare.
- `eval/paired/manifest.json` + `eval/paired/  references/` — frozen snapshots
  (263 reference facts; 92 video-supported, 7 article-only, 164 uncertain —
  the three caption-less videos classify mostly-uncertain until the
  post-generation ASR-transcript reclassification).

Bugs the benchmark has already found and fixed: an invalid yt-dlp
`--retry-sleep` expression, a `NameError` on `_best_track` whenever a video
has subtitles, and subtitle-download failures (HTTP 429) failing whole jobs.

Baseline generation is running (standard profile).  Interim scores for the
first three pairs (203 reference facts):

| pair | value | cmd/cfg | action | unsup. | pre/warn | struct | image | concise |
|---|---|---|---|---|---|---|---|---|
| tf-task-manager | 1.00 | 1.00 | 0.75 | 0.00 | 1.00 | 0.45 | 0.46 | 1.00 |
| tf-second-brain | 1.00 | 1.00 | 0.43 | 0.00 | 0.00 | 0.46 | 0.58 | 1.00 |
| tf-habit-tracker | 1.00 | 1.00 | 0.80 | 0.00 | 1.00 | 0.45 | 0.45 | 1.00 |

Pattern: exact fidelity is near-perfect; action coverage is dragged down by
marketing/testimonial prose polluting reference action facts (extraction
weakness F1, slated as improvement experiment #1); structure/image sit
~0.45–0.58 against thin reference headings and 2-image references vs
26-image generated articles.  Pairs 4–5 are queued: shared-GPU contention
forced the VLM into disk offload (~3 min/frame), so the run was stopped
cleanly after pair 3 to free the box; pair 4 keeps 8/81 frame
interpretations and resumes when a card fits vl-4b.  No eval/paired code
may change until the 5-pair baseline lands, so the before/after comparison
stays valid.

---

## M5 — Production packaging — **partially complete**

### Works

- `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`.
- Profile selection by `YT2WIKI_PROFILE`, GPU selection by
  `YT2WIKI_CUDA_DEVICE`, single-GPU reservation, single worker thread.
- Persistent `/data` volume holds database, media, frames, articles and
  weights.
- Standalone HTML export embeds images as data URIs — no network access at
  read time.
- GPU headroom planner moves a model to a card that fits and caps (with CPU
  offload) when none does, recording which happened.

### Open

- `docker compose build` has not been run on this host (no compose plugin
  binary, root filesystem full). Compose correctness is review-only.
- Compact profile peered on the actual 8 GB card (RTX 5060, UUID-pinned)
  during M6-Q §7: whisper int8, vl-2b BF16, text-4b NF4, verify-3b ran
  sequentially with headroom; 7-minute video end-to-end in 50 minutes.
  No GGUF/NVFP4 changes needed (BNB NF4 covers text; 2B VLM fits BF16).
