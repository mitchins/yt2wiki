# Known limitations and unresolved problems

Everything here is a real, observed limitation, not a hedge.  Items marked
**actionable** have a concrete next step.

## 1. Transcription and speech

- **ASR merges adjacent commands into one literal.** A transcript reading
  "run mkdir myproject, then cd into myproject" yields the single extracted
  command `mkdir myproject cd myproject`. The alternative — rejecting any
  fragment containing more than one known program name — also rejects ordinary
  invocations such as `apt-get install -y ffmpeg`, so it was rejected. The
  article shows the compound literal verbatim rather than inventing a split.
  **Actionable**: split candidates on ASR-likely conjunctions
  ("then", "and then", "next") when *both* halves are valid commands.
- **YouTube auto-captions carry no punctuation, so reference annotation
  extraction has to cue-split them.** This works but over-generates relative to
  a human-written list; recall measurements against it are therefore optimistic
  by an unknown margin. **Actionable**: hand-review the 24 annotations
  (`eval/annotations/*.yaml`, currently `reviewed: false`).

## 2. Visual interpretation

- **Small on-screen text is still the weakest area.** In the 10-minute terminal
  test, `touch -d tomorrow CreateAVMforONLY0075centsanhour.txt` was read as
  `touch -d tomorrow CreateAVMfORONLY075centsanhour.txt` by Qwen3-VL-4B at a
  512-token pixel budget. The dense-tile second pass helps but does not fully
  recover long identifiers. **Actionable**: benchmark the enhanced profile's
  640-token budget and the FP8-8B model on the `small-text` eval items and
  record the numbers.
- **Throughput.** ~10.5 s per interpreted frame on an RTX 3060 with the
  4B model, i.e. ~9 minutes for a 53-frame video. The RTX 5060 Ti (the
  production card) is expected to be 2-3× faster; that measurement is still
  outstanding.

## 3. Evidence and procedure reconstruction

- **The evidence prompt is transcript-window sized (90 s), so an action that
  straddles a boundary can be split into two observations.** Consolidation
  merges near-duplicates within a 45-second proximity window, but a 46-second
  gap survives. **Actionable**: widen the merge window or make the chunker
  overlap-aware for action boundaries rather than pure time.
- **Procedure reconstruction is a single LLM call over at most 120 of the
  observations.** Videos producing more than 120 observations (the test video
  produced 186) silently truncate the digest. This is a *real* correctness gap:
  the procedure may omit actions that are present in the evidence. The
  verifier catches the omissions as findings, but the article still ships with
  them. **Actionable**: two-pass reconstruction — draft from a summary of all
  observations, then refine against the un-truncated set; or chunk by
  procedure-phase and stitch.
- **Composed step wording is currently taken verbatim from the procedure
  stage** rather than rewritten. This is deliberate (a second rewrite pass
  adds a second chance to invent instructions) but it means the article reads
  slightly more clinically than a human-written manual.

## 4. Verification

- **Rule-based unsupported-claim detection is lexical.** A claim that rephrases
  an unsupported idea using the evidence's vocabulary can pass. The LLM
  verifier is allowed to *add* such candidates, but only when a rule
  corroborates them, so it cannot currently flag this case either.
  **Actionable**: add an embedding/NLI check for the "paraphrase of something
  unsupported" case; the model registry already has small encoder candidates.
- **`unsupported_actionable_rate` is 0 when the model produces no numbered
  steps** (an explanatory video with no procedure). That is correct behaviour
  but it means the metric must be read alongside `completeness`.

## 5. Output and storage

- **Every article stores the full Markdown and the full standalone HTML in
  SQLite.** For a library of hundreds of videos with dozens of frames this
  will grow to a few hundred megabytes; SQLite handles that, but the
  standalone HTML with embedded base64 images is the bulk. **Actionable**:
  store only the path and regenerate the standalone HTML on demand.
- **Frames are stored at 1280 px width regardless of source resolution**, so
  a 4K screencast's small text is downsampled before the VLM ever sees it.
  **Actionable**: keep the original-resolution frame for dense-text tiles.

## 6. Deployment

- **The container installs a CPU torch plus the CUDA 12 libraries CTranslate2
  needs**, rather than a full CUDA torch wheel. This keeps the image small but
  means GPU inference depends on the host driver's compatibility with the
  CUDA 12 runtime. Not yet validated on a machine without a CUDA 12.6 driver.
- **`docker compose` has not been run in this environment** (the host has no
  `docker-compose` plugin binary and the root filesystem is full, so image
  layers cannot be built there). The Compose file is correct-by-review only.
  **Actionable**: build and run it on the host and record the output.
- **The three-configuration evaluation has not been executed.** The harness
  exists and is unit-tested; the GPU runs required to produce the numbers in
  `docs/MEASUREMENTS.md` are still to be scheduled.
- **Deleting old job rows deletes the cached stage artifacts they own.** A
  library that was reprocessed several times then pruned will redo the
  expensive stages on the next run. The stage cache is keyed per job, not per
  source. **Actionable**: key stages by (source_id, stage, fingerprint) so
  caching survives job pruning, and dedupe the media directory by source id.

## 7. Deliberately out of scope

- No automatic article repair by a stronger model — that would let unverified
  prose back into an article that failed verification.
- No distributed infrastructure, no message broker, no object store.
- No agent orchestration: the pipeline is a fixed stage sequence with recorded
  fallbacks, not a planner.
