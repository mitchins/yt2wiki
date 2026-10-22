# Measurements

Everything below is a number actually recorded on this machine, not an estimate.
Hardware (development):

| Card | VRAM | Notes |
|---|---|---|
| GeForce RTX 5060 | 8 GB | shared with other work during measurement |
| GeForce RTX 5060 Ti | 16 GB | production budget; shared during measurement |
| GeForce RTX 3060 | 12 GB | mostly free; used for the timing runs below |

System: 24 CPU threads, 78 GB RAM, root filesystem full (drives the
`/usbpool` cache-dir constraint).

Test video: `gd7BXuUQ91w` — "60 Linux Commands you NEED to know (in 10 minutes)"
by NetworkChuck, 649.6 s, 1920×1080 @ 29.97 fps, AV1 + Opus, 48 MB.
Sourced as a local upload to keep acquisition out of the measurement.

Run profile: `standard` (asr large-v3 int8, Qwen3-VL-4B BF16, Qwen3.5-4B BF16,
SmolLM3-3B BF16) unless stated.

## End-to-end

| Stage | Wall | Peak VRAM | Notes |
|---|---|---|---|
| acquisition | 0.4 s | 0 | local file; YouTube adds download time |
| inspect | 0.4 s | 0 | 20.8 MB WAV, 28.3 MB proxy |
| transcribe | 84.6 s | 3.1 GB | 419 segments, 2619 words, 551 s of speech |
| frame_candidates | 17.0 s | 0 | 19 500 frames → 53 candidates, 5.1 MB JPEG |
| visual_interpret | 557.5 s | 8.9 GB | 53 frames, batched at 4 |
| evidence | 578.6 s | 9.3 GB | 9 transcript windows → 186 observations |
| procedures | 91.6 s | 9.3 GB | 17 steps (120-observation digest cap) |
| compose | 10.4 s | 0 | deterministic skeleton |
| verify | 68–157 s | 7.1–7.2 GB | rules + independent LLM model |
| publish | 0.2 s | 0 | md + standalone HTML + zip |
| **total** | **≈2100 s** (with GPU contention) | **9.9 GB** | ≈35 min per 11-minute video |

Real-time factor: 22–35× (22–35 minutes of compute per 11 minutes of video) on
the slowest card in the machine, and that card was shared with unrelated work
for part of the run. The production card (5060 Ti) is expected to cut this
substantially; that measurement is outstanding.

## Resource ceilings observed

- Highest single-stage peak: 9.3 GB (Qwen3.5-4B BF16 with device_map offload).
- Highest total process RSS: ~16 GB.
- The RTX 3060 has 12 GB total, so `standard` fits with ~2 GB spare.

## Visual interpretation throughput

| Implementation | Per frame | 53 frames |
|---|---|---|
| one forward pass per frame, no batching | 20 s | 1069 s |
| batched (4), 512 image tokens, 448 output tokens | 10.5 s | 557 s |
| synthetic benchmark, batch 4, no dense pass | 0.55 s | 4.4 s / 8 frames |

## Frame selection yield

| Signal | Count |
|---|---|
| hard cuts (ffmpeg scene score > 0.30) | 10 |
| screen-state changes (adaptive 1 fps signature diff) | 76 |
| periodic coverage (every 4 s) | 162 |
| transcript anchors (visual cue phrases) | 3 |
| speech gaps (> 6 s) | 0 |
| candidates before budget | 226 |
| **frames extracted** | **53** |

## Model footprints (declared in `config/models.yaml`, confirmed by load)

| Profile | Role | Model | Weights | Peak VRAM at load |
|---|---|---|---|---|
| compact | vision | Qwen3-VL-2B BF16 | 4.3 GB | 3.96 GB |
| standard | vision | Qwen3-VL-4B BF16 | 8.9 GB | 8.9 GB |
| enhanced | vision | Qwen3-VL-8B FP8 | 10.6 GB | not measured |
| compact/standard | text | Qwen3.5-4B BF16 / NF4 | 9.3 GB / 3.2 GB | 9.3 GB measured (BF16) |
| enhanced | text | Qwen3.5-9B NF4 | 6.5 GB | not measured |
| all | asr | faster-whisper large-v3 | — | 3.1 GB (int8_float16) |
| all | verify | SmolLM3-3B BF16 | 6.3 GB | not measured |

## Verification yield (progression on the same video)

| Run | findings | critical | dominant codes |
|---|---|---|---|
| initial | 64 | 25 | `omitted_identifier` 35, `unsupported_command` 33 |
| after command-literal fix | 31 | 7 | `llm_omitted_warning` 17 |
| after identifier filter | 40 | 26 | `llm_unsupported_action` 19 |
| after placeholder normalisation | 18 | 9 | `llm_contradiction` 7, `unsupported_command` 3 |
| final (34 steps, 18 timestamp anchors) | 42 | 10 | `llm_unsupported_action` 17, `omitted_identifier` 7 |

Every reduction came from fixing a root cause in the pipeline, never from
loosening the verifier.

This is the expected behaviour of a verifier that has not yet been fed a
correct article: it accurately reported that the article had not preserved
every on-screen identifier and that several commands carried narration
prefixes. Both root causes were then fixed and are covered by tests.
See `docs/MILESTONES.md` for the running record.

## Outstanding measurements

1. Enhanced profile end-to-end on the 16 GB card.
2. Compact profile end-to-end on an 8 GB card (the RTX 5060).
3. Three-config evaluation (A/B/C) over the 24-item reference set.
4. Exact-value fidelity: Qwen3.5-4B NF4 vs BF16 vs Qwen3.5-9B NF4.
5. `docker compose build` and `up` on the host.
6. A clean-cache run with no competing jobs on the production 16 GB card.
