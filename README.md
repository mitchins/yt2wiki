# yt2wiki

**A local, evidence-grounded compiler that turns a YouTube URL or an uploaded video into an illustrated wiki-style technical article.**

This is not a summariser. Every actionable statement in a generated article is expected to trace back to a timestamped span of the source video. When the source does not support a statement, the pipeline says so instead of filling the gap.

## Workflow

1. **Add a video** — paste a YouTube link or upload a local file. The service fetches metadata, downloads the stream, and extracts audio and frames.
2. **Transcribe & structure** — the narration is transcribed (faster-whisper) and divided into stable, transcript-covered sections. No part of the transcript is silently dropped.
3. **Write** — a text model writes one section at a time from the transcript slice, preserving exact commands and values and citing the source time as `[m:ss]`.
4. **Illustrate** — a vision model ranks frames per section through a batched veto + rubric judge (KEEP/REPLACE/EXPAND/USE_PAIR/OMIT) and installs the chosen frames.
5. **Read, search & edit** — guides live in a local SQLite library with FTS5 search, a reader with a table of contents, and an editor that versions every edit.

## Current status

- **V9 illustrated publisher** is the default path (narration-first, ASR-led). The V9 writer, frame discoverer, and rubric judge are integrated; V8-P3 visual work remains in history but is not the default web path.
- **Web service** (FastAPI + SQLite + Jinja) provides a library, reader, editor, job queue, and inference settings. The service itself is lightweight: it does not embed model weights.
- **Inference** is pluggable via OpenAI-compatible endpoints. Configure a provider (e.g. a vLLM or llama.cpp server) in **Settings → Inference**, test reachability, and qualify it for writing and/or image judging. A qualification checks real JSON generation and genuine multi-image judging, not just `/models`. Jobs capture the effective provider identities at submission time so later settings changes do not rewrite history.
- **Portable fallback:** Qwen3.5-4B GGUF + projector remains qualified for the 8 GB profile and can be run via an external `llama-server` or optional sidecar container. The default web service does not require PyTorch, Transformers, or local GGUF weights; model loading lives in the serving runtime, not the web app, and the service never acquires Docker socket privileges to manage it.

## Architecture

- **FastAPI** app (`src/yt2wiki/web/`) — library, reader, editor, jobs, settings, static assets.
- **SQLite + FTS5** — `articles`, `sources`, `jobs`, `stages`, `frames`, `article_assets`, `article_revisions`, plus FTS indexes. One job row shows the whole run; `recover()` rebuilds the queue after restart. One model set at a time; sequential GPU use.
- **Article library** — on disk under the data root (default `~/.local/share/yt2wiki` or `$YT2WIKI_HOME`), e.g. `articles/<article_id>/article.md`, `article.html`, `article.standalone.html` (self-contained), `article.zip`, `assets/`, `v9_report.json`.
- **Pipelines** — `src/yt2wiki/pipeline/` (10 legacy stages) and `src/yt2wiki/v9/` (writer, frames, select, judge, publish). Registry-driven model selection via `config/models.yaml` with fingerprint-based stage invalidation.

## Installation

Requires Python 3.11+, FFmpeg, Tesseract (for OCR veto), and SQLite.

```bash
# System deps (Ubuntu/Debian example)
sudo apt update && sudo apt install ffmpeg tesseract-ocr

# The project reuses CUDA tooling from the environment when present
uv venv --python 3.11 .venv --system-site-packages  # or conda python
export UV_CACHE_DIR=$PWD/.cache/uv TMPDIR=$PWD/.cache/tmp HF_HOME=$PWD/.cache/huggingface
uv pip install -e .  # or: pip install -r requirements.txt
```

Supported options:

- **Implemented:** local library, YouTube and upload ingest, transcription, V9 writing and image selection against a local or remote vision model, reader/editor with revision history, inference endpoint configuration and qualification.
- **Planned / not yet default:** automatic caption generation for installed images remains optional until a larger blind review qualifies it; standard-profile (12–16 GB) editorial quality is bake-off qualified but not yet re-smoked after the V9 wiring.

## Development

```bash
export HF_HOME=$PWD/.cache/huggingface  # must live where there is space
.venv/bin/python -m pytest tests -q          # unit + integration (no GPU, no network)
.venv/bin/yt2wiki info                       # profile, GPUs, model assignment
.venv/bin/yt2wiki run <url-or-file>          # CLI full pipeline
.venv/bin/yt2wiki serve --port 8080          # web app + worker
```

Tests are deterministic fixtures only; anything needing real models goes through `eval/` against reference annotations.

## Data & privacy

All user data stays under the data root you choose. No cloud inference, no telemetry, and no external assets at read time. Secrets are never stored: provider settings keep the *name* of an environment variable (e.g. `GEMMA_API_KEY`), not the key itself.

## Licence

A licence has not yet been decided for this repository. All rights reserved until an explicit licence file is added.

