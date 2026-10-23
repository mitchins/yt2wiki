# P3 — Composable inference: qualification report

One integrated mission: make yt2wiki a lightweight video-to-article publisher
that consumes Whisper and a multimodal LLM **over HTTP**, and prove it with a
real illustrated technical article. Starting point `origin/main@85d9f62` (P2).

**Verdict: all completion-gate conditions hold.** The default service is
independent of Whisper/LLM runtimes; external Whisper produces trustworthy
timestamped transcripts; one multimodal LLM (Halogen) handles writing and
image judging with no quality case for a split; a real end-to-end article is
demonstrably useful. Failures and backend identities are visible and
reproducible throughout.

**Recommendation: default to one Halogen provider for `writer`+`vision` and
one Whisper HTTP provider for `asr`.** No split configuration: Gemma 4 26B
fails the production vision contract too often (37% structurally invalid on
the frozen set, plus a hard 4-image cap) to justify a second role assignment.
Qwen3.5-4B could not be re-run (weights absent from this machine; V9-Z2
decisions cover a different source and are not reusable as reference).

Care was taken not to commit private addresses, tokens, inventories or test
media: endpoint URLs below appear as this report's evidence, never in code,
config or compose files (which carry only `http://asr:9000`-style internal
names or documentation placeholders).

## 1. Selected Whisper service and API contract

**Selected: `ahmetoner/whisper-asr-webservice`** — the community go-to by
standardisation/popularity/support (3.3k stars, 583 forks, maintained:
v1.10.0, faster-whisper v1.2.1, CUDA 12.9.2). Pinned and run as the GPU build:
`onerahmet/openai-whisper-asr-webservice@sha256:3deb30…7117ae`
(`latest-gpu`; the `:latest` CPU tag is a different image — verified by
pulling the wrong one first and measuring CPU RTF 1.3×).

**Protocol note (explicitly non-OAI, as anticipated).** The service does NOT
implement OpenAI `POST /v1/audio/transcriptions` (verified HTTP 404). It
speaks its own stable contract, which the adapter (`backends/asr_http.py`)
targets exclusively:

```
POST {base}/asr?output=json&task=transcribe&language=<code>
                 &word_timestamps=<bool>&vad_filter=<bool>
multipart field: audio_file (the pipeline's normalised 16 kHz WAV, streamed)
-> {"language": <code>,
    "segments": [{"start","end","text",
                  "words": [{"start","end","word","probability"}],
                  "avg_logprob","no_speech_prob","compression_ratio",...}],
    "text": <full transcript>}
```

An OAI-compatible server (e.g. speaches-ai/speaches, SYSTRAN-endorsed) could
attach via its `verbose_json` shape, but ghcr.io is unreachable from the
qualification network, so no OAI-protocol server was run here. The adapter
documents this; BYO servers get the same per-job validation.

Preserved: timestamped segments, **word-level timestamps (required — a
wordless response while requested is a hard `AsrHttpError`, never a silent
downgrade)**, language + duration (WAV header; the service returns neither
duration nor language confidence), per-segment confidence metadata when
present (`avg_logprob`/`no_speech_prob`/`compression_ratio`/per-word
`probability`, else SQL NULL — never fabricated zeroes), model provenance
(`YT2WIKI_ASR_MODEL` declares the server-side model; `/asr` echoes no id),
HTTP/timeout/malformed failures naming server + status + body excerpt.

Explicitly unsupported by `/asr` and **recorded per job**
(`asr_unsupported_options`, never emulated): vocabulary hints / initial
prompt, beam size, condition-on-previous-text, temperature, translation.
Validation is bidirectional (monotonic finite timestamps, bounds, text,
word/segment containment and ordering; all problems listed, no fail-fast).
Zero-duration words the server emits at audio cuts are accepted with an
`asr_warnings` entry (kept for text, not treated as precise timings) —
observed live on a sliced fixture (7 trailing words stamped `119.98`),
which first proved the validator fires.

`detect-language` (first 30 s) supplies genuine language confidence
(measured 0.9999 on the qual slice); `None` when unavailable.

Deployment: compose profile `asr` (digest-pinned, persistent `asr-models`
cache, no published ports, `ASR_DEVICE=cuda` set explicitly after proving
the default device mapping OOMs a bare 8 GB card). `YT2WIKI_ASR_URL` points
the service at it; or assign the transcription role to a provider in
Settings → Inference (`qualified_for_asr` via the `/asr` sine-tone protocol
probe; words still enforced per transcription).

Local faster-whisper is now the `asr-local` extra, explicitly selected only
(`--model whisper-large-v3` override); its `load()` fails with an actionable
error without the extra. No implicit fallback anywhere: HTTP failure is a
loud job error.

## 2. Default publisher ships no inference runtime

- Fresh-venv wheel install: `import faster_whisper` → `ModuleNotFoundError`,
  `import torch` → `ModuleNotFoundError`; default registry resolves
  `asr → asr-http`.
- Rebuilt thin image (`yt2wiki:p3`, 779 MB vs P2's 1.16 GB): no
  faster-whisper/ctranslate2/onnx/av/torch/transformers; `pytesseract`
  **added** (see §6 — the V9 veto needs it; pure-Python wrapper, binary
  already in the image); `yt2wiki info` → `asr → whisper-asr-webservice/asr-http`.
- `docker-compose.yml`: app service has no GPU reservation; `asr` profile
  carries the pinned GPU image + device-selection comment.

## 3. Real HTTP transcription, qualified against the local baseline

Same 120 s technical slice (Tailwind tutorial, dense speech), P2 local
recipe vs HTTP (large-v3, GPU, `vad_filter=true`, `word_timestamps=true`):

| path | load | transcribe 120 s | RTF | segs | words | language |
|---|---|---|---|---|---|---|
| local large-v3 int8 (cuda:1) | 11.6 s | 20.9 s | 0.17 | 27 | 461 | en/1.000 |
| HTTP large-v3 (container, cuda device 2) | 0.0 s | 23.0 s | 0.19 | 28 | 447 | en/0.9999 |

Token-level agreement 0.9756 (difflib on normalised tokens; segmentation
differs by one boundary — expected decoder nondeterminism, same engine
family). Every word validated finite, ordered and in-segment. Vocabulary
hint sent by the pipeline is recorded unsupported (the server has no prompt
param); vad/language honoured server-side.

Failure modes verified live: text-only responses raise naming the span;
HTTP 500 / non-JSON / non-object / connection failure raise with server
identity (unit + live); CPU-tag image measured then discarded.

## 4. Halogen writer + vision qualification (V9 workloads)

Endpoint `halogen-qwen3.8-flash-next`, ctx 262144. Single provider assigned
to both roles; `inference test` from inside the deployment:
`qualified_for_writer=true`, `qualified_for_vision=true` (real probes).

- Writer: title probe 7.2 s, valid JSON; full production section prompt
  (150 s technical span, exact-value-dense) 35.7 s, `finish_reason=stop`,
  valid JSON with heading/body/key_points/warnings, body 1210 chars.
  Exact tokens preserved (`npx`, `index`, `npm start`, `[2:xx]`); `CRACO` /
  `PostCSS` absent because the TRANSCRIPT renders them `CCO`/`post CSS`
  (transcript fidelity, not writer invention — no invented steps found).
  Reasoning model: 1001 reasoning tokens inside a 2000 budget; V9's
  `max_new_tokens=1600` per section holds with margin.
- Vision: real 2/4/8-image production rubric requests on library frames —
  3/3 valid (`EXPAND`/`REPLACE`/`REPLACE`, 17.5/20.0/26.1 s isolated),
  scores/vetoes editorially sensible (wrong-context foggy street vetoed,
  blank app screens down-scored). One protocol finding: Halogen returns
  **400 on `response_format: json_object` with image content** — the
  production path (`rank_images`, no `response_format`) is unaffected and
  valid JSON arrives without it; `_qualify_vision`'s plain retry covers
  detection.
- Raw failures preserved throughout (reasoning/content split handled:
  answers read from `content`, never `reasoning_content`); model identity,
  timings and image counts recorded per call.

## 5. Editorial image-choice comparison (principal model question)

Frozen inputs: 19 production V9 sections (`plan_sections` +
`section_transcript`, no chapters) × candidate frames
(`candidates_for_section`, truncated to 8) from 4 videos (3 library + the
P3 Hermes run). Same task to both models; Halogen sees all ≤8, Gemma 26B
sees the first ≤4 (**vLLM hard cap: "At most 4 image(s) may be provided"**,
verified 400 at 5–8 — an 8-image production rubric is impossible on that
endpoint; truncation recorded per call).

| model | valid / n | invalid modes | mean latency |
|---|---|---|---|
| Halogen | 19/19 (100%) | — | 60.6 s (17–31 s isolated; inflated by host contention, see below) |
| Gemma 4 26B | 12/19 (63%) | malformed JSON (unopened 2nd score object, `winner: null`), 7 cases | 4.2 s |

12 both-valid pairs → 6 disagreements. Blind assessment (model identities
hidden during viewing; pixel facts decide): all 6 favour Halogen —
generated `tailwind.config.js` over an earlier `npm install` frame;
complete `fixed top-0 left-0 h-screen w-16` over a partial `fixed top-0`;
opened Deck internals over a wrong-device Steam Machine (Gemma never saw
the best frame — truncation artifact); display-off device face over a heat
gun; tools/settings graphic over a title card; honest EXPAND (folder
contents not visible) over REPLACE on the same frame. 3 agreements verified
genuine (one visually: pick-in-gap frame matches the action exactly).

No invalid output is counted as editorial signal: invalids are reported
separately (§5 table). Gemma's editorial content is often reasonable; its
defects are structural compliance + the 4-image ceiling. No material,
repeatable quality case for a split configuration — and a split cannot fix
the 4-image cap anyway.

Latency note: Halogen calls during the frozen run degraded (48–270 s)
under concurrent load from the end-to-end job on the shared Halogen host
(the same host-compaction stall pattern seen at mission start). Isolated
measurements (§4) are the reference; contention is recorded, not hidden.

## 6. Bugs found and fixed by this mission (all covered by tests)

1. **Reasoning-model classify starvation** (the big one): the V9 veto's
   `max_new_tokens=20` leaves Halogen's 20+ reasoning tokens no room for the
   label → empty reply → every frame `unclassifiable-label` → 0/6 images on
   the first end-to-end run. Raised to 150 with rationale comments; verified
   live (`title_card` parses). Lesson: token budgets must assume reasoning
   overhead.
2. **`text → writer` role gap**: the pipeline asks for role `text` but the
   console configures `writer` — remote writing never engaged (thin image:
   `No module named 'torch'`). `loader._remote_spec` now maps `text → writer`
   (one provider, no duplicate config); unqualified writers fail loudly.
3. **Hallucinated citation URLs**: the section prompt said `[m:ss](url)`
   without substituting any URL — YouTube links in old articles were model
   guesses. New `cite_rule(url)`: real `[m:ss](url&t=SECONDS)` for YouTube,
   plain `[m:ss]` for uploads. Uploads have no origin URL (bare `0:00`
   traced to `origin_url = NULL`); header claim corrected conditionally.
4. **Missing `pytesseract`**: thin image lacked it → `v9_select` failed.
   Added to default deps (wrapper only; binary already present).
5. **V9 runner dropped base provenance**: `_ensure_base` discarded
   `mod.run()` returns and never wrote `artifacts_json` — `asr_server` et al.
   had nowhere to land. Now recorded (metrics + artifacts).
6. **Upload citation formatting**: Halogen writes bare `0:00` despite the
   plain-`[m:ss]` rule (values verified correct against transcript spans).
   Reported as a wart, not re-rolled a fourth time; see §7.

## 7. The real end-to-end article

Video: 919.7 s Hermes Agent tutorial (fresh, technical), ingested as an
upload through the deployed service (`POST /add/upload` → V9 job; YouTube
acquisition needs a JS runtime absent from the image — documented, upload
path proven).

Path proven: **video → FFmpeg → Whisper HTTP (168 segs / 2840 words,
166.9 s for 920 s audio, RTF 0.18) → Halogen writing (6 sections, 143.3 s) →
Halogen image judging (215.5 s, 3 KEEPs) → published article → persistent
library.** Total stage wall ~550 s (~9 min), every second in
`stages.duration_s`.

Job history is the failure-visibility evidence: attempt 1 `text/torch`
(the §6.2 gap), attempt 2 `v9_write timed out` (self-inflicted contention
with the comparison harness — retried after isolating Halogen), attempt 3
`v9_select pytesseract` (the §6.4 gap), final `done v9_publish`
`art9-file-748583d573c627ee`. No silent fallback at any step; every failure
named its cause.

Article ("Building and Running a Hermes Agent", ~10.5 k chars, 6 sections):
coherent procedure following the video's arc (chat-vs-run → VPS → Docker
setup → model settings → skills/memory → WhatsApp → schedules); exact
values preserved verbatim (`KVM2`, two cores, 8 GB RAM, 10% off, `Zeno
Automates`, `Soul MD`/`memory.md`/`user.md`, allow-list + `ignore` + phone
format + Docker terminal-backend warnings); spot-checked timestamps match
narration spans (0:00/0:04/0:18/0:33/0:47 all correct). Three appropriate
images (catalog Select, model settings, plus one more per-run variance);
remaining sections correctly imageless (promo overlays, QR codes, title
cards — vetoes working as designed on a marketing-heavy video; the
transcribe record also carries 59 zero-duration-word warnings across 11
segments, all kept for text, none treated as precise timings).

Reader-visible warts (reported, not hidden): bare `m:ss` instead of
bracketed citations (values right); image captions leak rank internals
(`incumbent best or within margin…` — pre-existing caption policy, worth a
follow-up); headings are transcript-derived and occasionally awkward;
`Soul MD` vs `SO.md` inconsistency inherited from transcript ASR.

Verified live: full-text search finds the article; markdown/zip/standalone
exports byte-complete (zip: md + 2 JPEGs); section edit → version 4;
container restart → library, article, revisions intact.

## 8. Performance and failure measurements

- ASR HTTP: RTF 0.19 (23.0 s / 120 s) vs local 0.17; zero load seconds.
- Halogen: title 7.2 s; sections ~35 s isolated; rubric 17–26 s isolated
  (2/4/8 imgs), degraded under host contention (recorded per call).
- End-to-end wall: transcribe 166.9 s (RTF 0.18 on 920 s) + write 143.3 s
  + select 215.5 s + publish; stage-wall total ~550 s, all visible in
  `stages.duration_s`.
- Invalid outputs: Gemma 7/19 malformed rubric JSON (kept raw); Halogen
  0 invalid across 19 + qual probes; zero-duration-word warning fired once
  on sliced audio (accepted, recorded).
- Model identities: `model_loads` shows `remote-halogen` (text/vision) and
  `remote-asr`/`large-v3` (asr), all `ok=1`; inference snapshot frozen per
  job; no fallback rows.

## 9. CI, pytest, coverage, Sonar

- `pytest tests`: full suite green (1 pre-existing env failure:
  `test_text_model_class_is_importable`, transformers/tokenizers version
  mismatch, fails identically on the base commit).
- New: `tests/test_asr_http.py` (30 tests: contract, parse/validate incl.
  zero-duration + text-only hard-fail, mocked transport errors, stage
  wiring, registry default, probe routing) + loader mapping test +
  `_ensure_base` provenance test + `cite_rule` test.
- `ruff`: new/changed files clean; repo baseline (238 pre-existing) untouched.
- Sonar: PR gate to be reported on push (new-code coverage from the 30+
  new tests; no threshold changes).

## 10. Deliverables and recommendation

Shipped in one reviewable PR: HTTP ASR adapter + validation, `asr-local`
extra split, minimal provider changes (one `asr` role alongside
`writer`/`vision`; `text → writer` mapping), pinned compose example,
qualifying tests, deploy-Docs updates, this report. No custom Whisper
server, no new serving stack, no model downloads by yt2wiki, no UI
redesign, no leaderboard, no auto-merge.

**Use one Halogen provider for both roles by default.** Keep separate
writer/vision assignment available as the advanced option the code already
supports — the evidence gives no reason to use it.
