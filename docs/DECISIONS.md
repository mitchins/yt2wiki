# Engineering decisions and recorded failures

Significant decisions, the evidence behind them, and what failed.  Ordered
roughly chronologically within each theme.

---

## 1. Environment

**Decision.** Build a `uv` venv at `.venv` with `--system-site-packages` pointed
at the existing `vllm-latest` conda environment, rather than creating a new
conda environment.

**Why.** The root filesystem (`/dev/sda2`, 915 GB) was at 100% with only
~990 MB free, so a new conda environment under `/home/mitch/miniconda3/envs`
could not be installed. `vllm-latest` already carries `torch 2.11.0+cu130`,
`transformers 5.12.1`, `opencv-python-headless`, `pillow` and `numpy`, all built
for CUDA 13 / Blackwell (`sm_120`). Reusing it avoided ~5 GB of duplicate wheels.

**Consequence.** The venv must be created from the *conda interpreter* (not
`/usr/bin/python3`), otherwise `--system-site-packages` points at the wrong
tree. Cache and temp directories must live on `/usbpool`.

**Failure.** `uv pip install ... "bitsandbytes>=0.47"` first resolved to
0.50.2, which pulled `torch==2.14.1` from the default (CPU-cu130) index and
replaced the conda CUDA torch. The 16 GB Whisper run then died. Fixed by
installing `bitsandbytes==0.48.2 --no-deps` and re-pinning
`torch==2.11.0` from `https://download.pytorch.org/whl/cu130`. Recorded in
`docs/MEASUREMENTS.md`.

---

## 2. Speech recognition

**Decision.** faster-whisper (CTranslate2) `large-v3` for every profile, with
`int8_float16` on the 8/12 GB profiles and FP16 on 16 GB. Smaller
`small.en` is the recorded emergency fallback.

**Measured.** 649 s of screencast audio in ~85 s on an RTX 3060 (12 GB) —
≈0.13 s real-time per second of audio, peak VRAM ≈3 GB.

**Failure 1.** `Library libcublas.so.12 is not found or cannot be loaded`.
CTranslate2's CUDA build needs CUDA 12 libraries, but the venv's `nvidia-*`
packages are CUDA 13. Fixed by installing `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` and `nvidia-cuda-runtime-cu12` alongside the cu13 stack.

**Failure 2 — important.** Installing `nvidia-cuda-nvrtc-cu12` as well made
every torch CUDA kernel fail with
`nvrtc: error: failed to open libnvrtc-builtins.so.13.0`. The cu12 `nvrtc`
shared library shadowed torch's cu13 one through the `nvidia` package search
path. Fix: **do not install `nvidia-cuda-nvrtc-cu12`**; CTranslate2 works
without it. This is a genuine cross-version hazard and is now part of the setup
notes in `AGENTS.md`.

**Failure 3.** faster-whisper 1.2.1 calls
`av.open(..., metadata_errors="ignore")`, which PyAV 19.0.1 does not accept.
Rather than pin PyAV down (and pull a second FFmpeg copy into the venv), the
pipeline already normalises audio itself with ffmpeg, so `backends/asr.py`
decodes the 16 kHz mono PCM16 directly with the stdlib `wave` module and hands
faster-whisper a float32 array. This is both faster and avoids the
incompatibility.

---

## 3. Visual model

**Decision.** Qwen3-VL-2B / 4B / FP8-8B by profile. Specialised VLM rather than
one large multimodal model for everything.

**Measured (RTX 3060, 12 GB).**
- Qwen3-VL-4B BF16: 8.9 GB resident, loads in ~80 s (cold page cache).
- Reading a synthetic 1280×720 frame with two lines of technical text: 6.5 s
  end-to-end in the first, non-batched, implementation.

**Optimisation that mattered.** The first implementation ran one forward pass
per frame plus four more for the dense-text tile pass — 20 s per frame, i.e.
17.8 minutes for a 10-minute video. Two changes:

1. Real batching in `backends/vlm.py::describe_batch` (one `processor(...)`
   call with `text=[...]`, `images=[...]`, `padding=True`). 8 synthetic frames
   went from ~56 s (8 × 7 s) to 4.4 s at batch 4.
2. Bounding the *pixel* budget (`vision_image_tokens`, default 512) and the
   *generated* token count (`vision_max_new_tokens`, default 448) separately.
   The original single `vision_max_tokens=768` was doing both jobs at once and
   was far too generous for generation.

Result: 53 frames in 557 s (10.5 s/frame including the dense pass), down from
1069 s. The dense tile pass is only triggered for frames whose text looks small
or whose `visible_text` is sparse, so narration-only frames cost one forward
pass.

**Failure.** `device_map="auto"` with `max_memory={index: "11.0GiB"}` triggered
`caching_allocator_warmup` and an 8.27 GB allocation on a card that already
had a competing job. Fixed by picking a free GPU (highest free memory) when no
explicit `--device` is given, and by making the budget configurable per
profile.

---

## 4. Text model and quantisation

**Decision.** Qwen3.5-4B BF16 on `standard`, Qwen3.5-4B 4-bit NF4 on `compact`,
Qwen3.5-9B 4-bit NF4 on `enhanced`.

**Why not FP8 for the text model.** Qwen ships FP8 checkpoints for the VL
family and for 27B, but not for the 3.5 text sizes, so BF16 + bitsandbytes NF4
is the only route to the 9B on a 16 GB card. The evaluation suite is required
to compare exact-value fidelity of the 4-bit model against the BF16 baseline —
that comparison is still outstanding (see `docs/MILESTONES.md`).

**Verification model.** `HuggingFaceTB/SmolLM3-3B` — deliberately a different
family from the composing model. The verifier only ever supplies *candidate*
findings; a candidate is discarded unless a rule in the verification layer can
corroborate it (`_confirm_llm_findings`). This implements "a second model may
assist verification but must not be treated as an infallible judge".

---

## 5. Pipeline architecture

**Decision.** Ten stages, each returning a metrics dict, each with its own
artifact directory and a fingerprint.

**Failure: over-broad cache invalidation.** The first fingerprint used the whole
`Tuning` object, so adding the contradiction-detection prompt invalidated every
stage of every video in the library and forced a full retranscription. Fixed
with `pipeline.STAGE_TUNING_KEYS`, which lists the tuning knobs each stage
actually reads. The rule now also covers model variants, so a VLM swap does not
retranscribe.

**Failure: transcript chunking produced 378 windows instead of 9.** The overlap
carry computed `keep_back = r.end - start` on rows *before* the boundary, which
is always negative, so the condition `keep_back < overlap_s` never fired and
every row started a new chunk. That produced 378 LLM calls at ~58 s each.
Fixed by accumulating duration backwards from the boundary. The bug is now
covered by `tests/test_basics.py::test_chunk_transcript_windows_overlap_and_are_bounded`.

**Failure: `json.dumps(..., default=str)` stringified dataclasses.** Evidence
`Literal` objects were written to SQLite as `"Literal(kind=..., literal=...)"`
strings, so `Evidence.from_row` crashed on the next stage. Replaced with a
`_json_default` that expands dataclasses via `dataclasses.asdict`. This class
of bug is now covered by `tests/test_pipeline.py::test_json_dumps_handles_dataclasses_and_paths`.

**Decision: deterministic consolidation.** The first implementation asked the
LLM to consolidate observations, which cost an unbounded number of extra calls
and, worse, made the evidence set non-reproducible. Replaced with a
deterministic merge (`pipeline/stages/evidence.py::_consolidate`) that collapses
exact and near duplicates and *keeps* contradictory pairs, then asks the model
about at most 12 rule-detected value disagreements in a single call
(`_adjudicate_contradictions`).

**Failure: truncated JSON.** The extraction model frequently hit the token limit
inside a code fence. `backends/llm.py::extract_json` now strips fences, finds a
balanced span, and if that fails repairs the fragment by walking the bracket
stack and appending closers (`repair_truncated_json`). `chat_json` retries once
with a stricter instruction. Thread-local SQLite connections were also needed
because FastAPI handlers run off the request thread.

---

## 6. Frame selection

**Decision.** Candidate generation is a single low-resolution streamed pass, not
a decode of the video. `visual.py::stream_signatures` reads a 32×18 greyscale
image per second from ffmpeg's rawvideo pipe (~576 bytes per second) and derives
both a 72-bit difference hash and a mean/std, which is enough for:

- hard-cut detection (ffmpeg `select='gt(scene,T)'`),
- screen-state change detection (for screencasts that never hard-cut),
- perceptual deduplication of candidates.

On the 10-minute test video this cost 17 s total and found 10 scene cuts, 76
screen-state changes, 162 periodic slots, 3 transcript anchors and 0 speech
gaps, reducing 19 500 frames to 53 candidates (5.1 MB of JPEG).

**Decision: frames are cached on disk, keyed by source id and timestamp**, never
held as a list. Each candidate is one `extract_frame` call at a fast-seek
timestamp.

---

## 7. Verification

**Decision: rules first, model second.** The article→evidence and
evidence→article checks are pure functions over the article Markdown and the
evidence records. An LLM may only *add* candidates that a rule can corroborate.

Two rule bugs found by the test suite and fixed:

- `evidence->article` initially used Jaccard similarity against the whole
  article, which fails because the article's token count dwarfs the
  observation's. Replaced with `token_coverage` (fraction of the observation's
  tokens present in the article).
- `exact_values` extraction from the article stripped code spans entirely,
  which removed the very values it was supposed to check. Code-span content is
  now retained.

A shared `content_tokens` lives in `yt2wiki/records.py` so the verifier, the
evidence matcher and the evaluation metrics cannot drift apart.

---

## 8. Web application

**Decision.** FastAPI + server-rendered Jinja2 templates, no build step, no CDN,
no external fonts. The article is the primary surface; the video player is
secondary and lazily loaded (`preload="none"`).

**Failure.** `templates.TemplateResponse("index.html", {...})` — the two-positional-argument
form — raised `TypeError: unhashable type: 'dict'` because the installed
starlette (fastapi 0.136.3) requires `(request, name, context)`. Switched to the
current signature.

**Decision: one worker thread.** `web/app.py::JobRunner` is a single-threaded
queue. This enforces the sequential GPU-use non-negotiable at the application
level rather than relying on callers remembering.

---

## 9. Out-of-scope things deliberately not done

- No distributed infrastructure, no Redis, no Celery. SQLite + a thread pool is
  the right size for a personal library.
- No agent orchestration. The pipeline is a fixed sequence of stages; the only
  adaptive behaviour is recorded fallback selection.
- No automatic "repair" of a generated article by a stronger model. Adding
  unverified prose to fix a verification failure would defeat the point of the
  verifier.
