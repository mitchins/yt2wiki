# Architecture

## Data flow

```
                     ┌──────────────────────────── disk (data/) ────┐
                     │  media/<src>/video.mp4, audio_16k.wav,       │
                     │           proxy.mp4, captions.srt.json       │
   acquire ──► inspect ──► transcribe ──► frame_candidates         │
   (yt-dlp)  (ffmpeg)  (whisper |    (signature pass, ffmpeg       │
                       supplied      select, per-timestamp extract)│
                       captions)            │                       │
                     ┌───────────────────────┼───────────────┐      │
                     ▼                       ▼               │      │
              evidence ◄──── visual_interpret (VLM, batched, ┘      │
                     │       dense-text tiled re-read)              │
                     ▼                                              │
              procedures ──► compose ──► verify ──► publish          │
              (LLM)         (LLM prose  (rules +    (md, html,     │
                             + deterministic  LLM      zip, FTS5)  │
                             skeleton)      candidates)            │
                     └──────────────────────────────────────────────┘
                                  SQLite (WAL, per-thread conns)
```

## Contracts between stages

Each stage returns a metrics dict and reads/writes:

| Stage | Reads | Writes |
|---|---|---|
| acquisition | `sources` row | `media/<sid>/video.mp4`, `meta.json`, captions |
| inspect | video | `audio_16k.wav`, `proxy.mp4`, probe |
| transcribe | audio, meta | `transcript_segments`, `transcript_words`, `transcript.jsonl` |
| frame_candidates | proxy, transcript | `frames` rows + `frames/<sid>/*.jpg` |
| visual_interpret | frames | `frame_interpretations` (model_key, prompt_version, profile) |
| evidence | transcript + interpretations | `evidence` rows with `literals` and `t_start/t_end` |
| procedures | evidence | `procedures`, `procedure_steps` |
| compose | procedure + evidence + frames | `articles`, `article_assets`, `article.md`, `article.html` |
| verify | article + evidence | `findings` |
| publish | article | `article.standalone.html`, `article.zip`, FTS5 index |

A stage never reads the previous stage's in-memory return value except through
`StageContext.artifacts`, which is serialised into `stages.artifacts_json` — so
a restarted process reconstructs exactly the same state.

## Fingerprinting

`StageContext.stage_fingerprint(stage)` hashes:

- the profile,
- `model repo@revision:variant JSON` for all four roles,
- only the tuning keys listed in `pipeline.STAGE_TUNING_KEYS[stage]`,
- the content hashes of the inputs this stage consumed (`_input_hashes`).

A stage is skipped only when its stored fingerprint matches. Changing the VLM
invalidates `frame_candidates` (it changes the budget), `visual_interpret` and
nothing else. Changing the extraction prompt invalidates `evidence`,
`procedures`, `compose`, `verify`, `publish` and not `transcribe`.

## Model lifecycle

`backends/loader.py::ModelManager.acquire(role)` is a context manager:

1. `headroom.plan()` picks the card and the per-process cap from
   `nvidia-smi` + the registry's declared footprint, recording why if it had
   to move or cap.
2. Tries the registry's model for the role; on failure records the exception
   in `model_loads` and tries the declared fallback chain.
3. Yields the backend.
4. On exit: unloads the model, `gc.collect()`, `torch.cuda.synchronize()`,
   `empty_cache()`, `ipc_collect()`.

`web/app.py::JobRunner` is single-threaded so only one model set is ever
resident, which is what makes the 16 GB budget honest.

## Evidence model

An evidence record is an *observation*, never prose:

```
Evidence(evidence_id, kind, text, literals[Literal], t_start, t_end,
         frame_ids, transcript_spans, confidence, model_key, prompt_version,
         superseded_by, status)
```

`Literal` keeps `literal` (verbatim from the source, never reformatted) and
`normalized` (only cosmetic differences collapsed). The article is rendered
from `procedure_steps`, which must cite at least one `evidence_id`; a step that
cannot is dropped and recorded.

## Verification

```
article ──► rules ──► findings ──► DB
   │                 ▲
   └── LLM candidates └── must be corroborated by a rule to survive
```

Both directions:

- `article -> evidence`: each numbered step and each fenced command is checked
  against evidence by literal-value support and token coverage. Unsupported
  actionable statements are `critical`.
- `evidence -> article`: each critical observation (action, command, config,
  measurement, warning, prerequisite, correction, identifier) must appear in the
  article, measured by `token_coverage` or literal presence.

## Web application

- Server-rendered Jinja2, no build step, no CDN, no external fonts.
- The article view embeds the rendered HTML of the article with asset paths
  rewritten to `/assets/<article>/assets/…`; the video player is a secondary,
  lazily-loaded panel.
- Endpoints: URL/upload submission, job status with per-stage state, library,
  FTS5 search over articles and evidence, article reading, frame gallery,
  timestamped evidence, verification findings, provenance, edit + revision
  history, Markdown/zip/standalone exports, reprocessing with a different
  profile or model.

## Frontend contract for search

`articles_fts`, `evidence_fts` and `transcript_fts` are FTS5 tables with
`porter unicode61` tokenisation. The API converts a user query into an
`AND`-joined set of quoted prefixes.
