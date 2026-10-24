# yt2wiki deployment — pip, Docker, Portainer

This document qualifies the three supported deployment paths for the existing
FastAPI/Uvicorn application (`src/yt2wiki/web/app.py`). The base service is
the web library and article publisher; inference is via external OpenAI-compatible
providers by default, with an optional local `llama.cpp` sidecar for the
already-qualified Qwen3.5-4B visual judge.

No Redis, Celery, PostgreSQL or Kubernetes is required.

---

## 1. Pip — simplest deployment

**Supported:** Python 3.12 (also 3.11+), no GPU, no PyTorch, no Transformers, no GGUF.

System deps:

```bash
sudo apt update && sudo apt install ffmpeg tesseract-ocr tesseract-ocr-eng
```

Build and install from the wheel (no `PYTHONPATH`):

```bash
# from the checkout
uv build --wheel --out-dir /tmp/dist   # wheel contains templates/static via package-data
# outside the checkout, clean env
uv venv --python 3.12 /tmp/pip-test --seed
/tmp/pip-test/bin/pip install /tmp/dist/yt2wiki-0.1.0-py3-none-any.whl
# verify wheel contents
unzip -l /tmp/dist/yt2wiki-0.1.0-py3-none-any.whl | grep -E "templates|static"
```

The wheel is self-contained: `src/yt2wiki/web/templates/*.html` and `web/static/*`
are included via `pyproject.toml [tool.setuptools.package-data]`. No `PYTHONPATH`
modification is needed.

**Upgrade:**

```bash
/tmp/pip-test/bin/pip install --upgrade /tmp/dist/yt2wiki-0.1.0-py3-none-any.whl
```

**Startup with persistent data:**

```bash
export YT2WIKI_HOME=~/.local/share/yt2wiki   # your persistent library; keep this export for every command below
mkdir -p "$YT2WIKI_HOME"
/tmp/pip-test/bin/yt2wiki --help
/tmp/pip-test/bin/yt2wiki info     # shows profile, fingerprint, no CUDA
/tmp/pip-test/bin/yt2wiki serve --host 127.0.0.1 --port 8080
```

Verification (outside checkout, no GPU, `PYTHONPATH` unset):

- `yt2wiki --help` prints commands
- `yt2wiki info` shows `0.1.0`, profile `compact`/`standard`, `cuda available : False` without importing `torch`/`transformers`
- `pip list` shows `yt2wiki 0.1.0` and no `torch`/`transformers`
- `ffmpeg -version` and `tesseract --version` succeed
- `TestClient` against `create_app(Settings.load(data_dir=...))` exercises:
  `GET /library` (200, shows article card), `GET /articles/{id}` (reader),
  `GET /api/search?q=...` (FTS), `POST /api/articles/{id}` (edit),
  `GET /api/articles/{id}/revisions` and `POST /revert` (version history),
  `GET /api/articles/{id}/export.md` and `/standalone` (exports)
- Stop and restart (`Settings.load` with same `YT2WIKI_HOME`, new `Database`) — `SELECT * FROM articles` still returns the article

Evidence: `yt2wiki-0.1.0-py3-none-any.whl` (clean `uv venv` install, no `torch`/`transformers`,
`yt2wiki info`/`inference show` work with `YT2WIKI_HOME` set once via `export`), plus the full
pytest suite (253 passed) covering the same library/search/edit/revisions/export/restart flow.

Do not publish to PyPI without a separate licence decision — the project has no selected distribution licence.

---

## 2. Thin service container

**Base image:** `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70`
(pinned digest, `124 MB` base). The published image `ghcr.io/mitchins/yt2wiki:main`
is a multi-arch manifest (AMD64 + ARM64; see docs/releases.md) including
`ffmpeg`/`tesseract` and the Python deps (`fastapi`, `uvicorn`, `jinja2`, `pillow`,
`httpx`, `yt-dlp`, `pytesseract`, `numpy` — and **no**
`faster-whisper`/`ctranslate2`/`av`/`torch`: transcription and inference are
external HTTP services, see §3/§3.1).

Contents verified (replace the tag with the pinned `:sha-<commit>` you deploy):

```bash
docker run --rm ghcr.io/mitchins/yt2wiki:main pip list | grep -E "torch|transformers"  # no output
docker run --rm ghcr.io/mitchins/yt2wiki:main yt2wiki --help
docker exec <container> whoami  # yt2wiki (uid 1000, unprivileged)
docker exec <container> ffmpeg -version
docker exec <container> tesseract --version
```

The image contains:
- Python 3.12, `yt2wiki` (wheel), `ffmpeg`, `tesseract-ocr`, runtime deps
- No `torch`/`transformers`, no LLM/VLM weights
- Does not require `--gpus` or NVIDIA runtime to start (`warning: no CUDA device visible` but `uvicorn running on http://0.0.0.0:8000`)
- Runs as user `yt2wiki` (uid 1000), `WORKDIR /app`, `VOLUME /data`, `YT2WIKI_HOME=/data`, `HF_HOME=/data/models`
- Persists `SQLite` (`/data/db/yt2wiki.sqlite3*`), `uploads` (`/data/uploads`), `frames` (`/data/frames`), `articles` (`/data/articles`), `assets`, `revisions`, `config` (`/data/config/inference.json`)
- `recover()` on startup rebuilds the job queue: still-queued jobs resume automatically, while a job that was running is marked `interrupted` (finished stages are kept and reused) — it is **not** automatically retried. Retry it explicitly from the UI (`Retry`) or `POST /api/jobs/{id}/reprocess` after the provider returns.
- Healthcheck: `curl -sf http://127.0.0.1:8000/health` (30s interval, 15s start period)
- Bounded logs: `json-file` with `max-size 10m` `max-file 3`
- Serves the library without loading any model (`GET /health`, `GET /library`, `GET /articles/{id}` all work with providers offline)

Install and upgrade through Compose only — no local build step:

```bash
docker compose pull
docker compose up -d
```

Run (bind to host loopback, data persists) — equivalent to `up -d` above:

```bash
docker run -d --name yt2wiki -p 127.0.0.1:8080:8000 -v yt2wiki-data:/data ghcr.io/mitchins/yt2wiki:main
# or bind-mount an existing library without migration:
# -v /home/mitch/.local/share/yt2wiki:/data
curl -sf http://127.0.0.1:8080/health
curl -sf http://127.0.0.1:8080/library | grep "Library"
docker exec yt2wiki whoami
docker exec yt2wiki ls -lh /data/db
docker stop yt2wiki && docker start yt2wiki && curl -sf http://127.0.0.1:8080/health
```

Developers build locally with the override file (never for ordinary installs;
see docs/releases.md for tags and digests):

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml build
```

Inside the container the app listens on `0.0.0.0:8000` so Docker can forward
host `127.0.0.1:8080` → container `8000`. Host-side binding to `127.0.0.1` is the
security boundary — do not publish the admin UI on `0.0.0.0` without an
authenticated proxy (see §4).

---

## 3. Service / model separation

Default inference is **external providers** (`src/yt2wiki/web/inference.py`):
LLM/vision roles speak OpenAI-compatible `/chat/completions`; transcription
speaks the Whisper service `/asr` contract (see §3.1). yt2wiki owns no
weights and performs no local inference on the default path.

- Configuration lives in `/data/config/inference.json` (`providers: {name: {url, model, api_key_env}}`, `roles: {writer, vision, asr}`) and status in `inference_status.json`. Secrets are never stored: `api_key_env` is the *name* of an env var (`^YT2WIKI_[A-Z0-9_]{1,56}$`), validated by `validate_api_key_env`, not the key itself. Compose files never hardcode keys.
- Role mapping: the pipeline asks for `text` (writing), `vision` (image
  judging) and `asr` (transcription); the operator console names these
  assignments `writer`, `vision` and `asr` (`loader._remote_spec` maps
  `text → writer`). One provider entry may serve both writing and vision —
  assign the same provider to both roles.
- `yt2wiki serve` does not require `LLAMA_SERVER_DIR` when providers are remote. `LlamaServerVLM` is only instantiated when `config/models.yaml` assigns `engine: llamacpp-server` and the pipeline requests that role; the web app never silently loads a local Transformers model when `inference.json` points to a remote URL. `backends/loader.py` selects `RemoteOpenAIBackend` when the job's snapshotted `inference` config contains a provider URL.
- The complete web workflow honours the snapshotted provider endpoints: `submit_upload` → `enqueue` captures `_inference_snapshot()` (`writer/vision/asr url/model`, `reachable/qualified_for_*`) into the job's `config_json`; later `run_job` uses that snapshot, not live settings, so changing providers does not rewrite running jobs.
- A hosted model may qualify for writer only, vision only, or both — `test_endpoint` qualifies each role via real generation (JSON writer probe and 2-image rubric judge) and writes `qualified_for_writer`/`qualified_for_vision` to `inference_status.json`. A transcription server is recognised by the absence of `/models` and probed against `/asr` (`qualified_for_asr`); per-job validation still hard-fails a text-only server when words are required. Do not infer capability from a successful `/models` connection.
- The service starts and serves the library when all providers are offline. `yt2wiki info` prints `cuda available : False` without importing torch, and `GET /library`/`/articles` work. Submitting a job that needs an unavailable model yields an explicit `failed` state with `error` and `findings` (e.g. `model_loads` row with `ok=0`), not a silent fallback or a hanging queue. The job can be retried after the provider returns.

Verification:

- `yt2wiki info` with no `LLAMA_SERVER_DIR` and no provider still prints info (no `llama-server binary not found` error at serve time unless a job actually needs the llamacpp engine).
- Mocked OpenAI endpoints (`httpx` `MockTransport` returning `{"choices":[{"message":{"content":"{\"ok\": true}"}}]}` and valid `RUBRIC_PROMPT` JSON) prove `writer` and 8-image `vision` requests are routed to the configured URLs (`tests/test_remote_inference.py`).
- Stopping the mock servers and submitting a job shows `state failed` with recoverable error, while `GET /library` still serves.

Keep secrets out of exported articles and logs: `article.standalone.html` and `article.zip` contain only markdown/assets, never `inference.json` or env values; `app_log` and `model_loads` store `model_key`/`repo`, not API keys.

### Secrets: env_file (Compose) vs stack variables (Portainer)

Provider keys enter the container as environment, never as committed text:

```bash
cp .env.example .env && chmod 600 .env   # on the Docker endpoint; .env is git-ignored
# edit .env: YT2WIKI_OPENAI_API_KEY=... (real key lives only here, mode 600)
docker compose up -d yt2wiki
```

Two mechanisms, do not confuse them:

- **Compose interpolation** (`${VAR}` in YAML) is resolved **on the host** when
  Compose renders the file — values come from the host shell or the `.env` file.
- **`env_file`** entries are injected **into the container** as environment at
  container start. This is how provider keys reach the app.
- **`inference.json` stores only the variable NAME** (`api_key_env`); the app
  reads the value from the container environment at request time. Verify:
  `docker exec yt2wiki cat /data/config/inference.json` must show the name,
  never the key.

### Operator console: `yt2wiki inference` (required in Docker)

The settings web API refuses non-localhost peers (see §5), so browser sessions
through the published Docker port — including SSH tunnels — cannot save or test
endpoints. Configure inference from the operator console instead:

```bash
docker exec yt2wiki yt2wiki inference show
docker exec yt2wiki yt2wiki inference save --file /data/config/inference-payload.json
docker exec yt2wiki yt2wiki inference test --name lan
```

Create the payload file inside `/data/config/` first — `save` only reads regular
files that resolve inside the config directory (symlinks resolved, `../` escapes
refused). Minimal valid example (no real credentials or private addresses — use
your own endpoint and a `YT2WIKI_…` variable name whose value you inject via
`env_file` or Portainer stack variables):

```bash
cat > /tmp/inference-payload.json <<'EOF'
{"providers": [{"name": "lan", "url": "http://192.168.1.20:8000/v1",
               "model": "qwen3", "api_key_env": "YT2WIKI_LAN_KEY"}],
 "roles": {"writer": "lan", "vision": "lan"}}
EOF
docker cp /tmp/inference-payload.json yt2wiki:/data/config/inference-payload.json
docker exec yt2wiki yt2wiki inference save --file /data/config/inference-payload.json
```

Transcription uses a separate provider entry pointing at the Whisper
service (no `/v1` suffix — the adapter drives its `/asr` contract) and the
`asr` role. Do not reuse the LLM `/v1` example for ASR:

```bash
cat > /tmp/inference-asr-payload.json <<'EOF'
{"providers": [{"name": "whisper-svc", "url": "http://asr:9000",
               "model": "large-v3", "api_key_env": ""}],
 "roles": {"asr": "whisper-svc"}}
EOF
docker cp /tmp/inference-asr-payload.json yt2wiki:/data/config/inference-asr-payload.json
docker exec yt2wiki yt2wiki inference save --file /data/config/inference-asr-payload.json
docker exec yt2wiki yt2wiki inference test --name whisper-svc
```

`test` reports `qualified_for_asr` only when the server speaks the
transcription protocol; per-transcription validation still hard-fails a
text-only server when words are required. `model` is the operator-declared
server-side model (recorded for provenance — `/asr` echoes no model id).

- `save` accepts the same `{providers, roles}` JSON schema as the web API and
  validates it before writing (`YT2WIKI_…` names only, `http(s)://` URLs;
  malformed shapes fail with a controlled error, never a traceback).
- `test` runs the full qualification (writer JSON probe + 2-image rubric judge),
  records the result in `inference_status.json`, prints it, and exits non-zero
  when the endpoint is not reachable/qualified.
- The `/settings` page renders an explicit **Operator only** notice — with these
  exact commands — for every non-localhost viewer, and disables its form.

Verified against a locally built image of the same Dockerfile (tag `yt2wiki:0.1.0`,
pre-GHCR) running with an authenticated mock
provider (`http://172.17.0.1:8931/v1`, key `YT2WIKI_MOCK_KEY=test-secret-xyz` via
`--env-file`): the mock logged `Authorization: Bearer test-secret-xyz` on
`/models` and `/chat/completions`, `inference test` reported
`qualified_for_writer/…_vision: true`, and `/data/config/inference.json`
contained only the variable name (see §7). Re-verify against the published
`ghcr.io/mitchins/yt2wiki:main` image after the first GHCR publish.

### 3.1 Default transcription backend (HTTP, no bundled ASR runtime)

The default install ships **no** ASR inference runtime: `faster-whisper` moved
to the `asr-local` extra (`pip install yt2wiki[asr-local]`, explicitly
selected per override, never an implicit fallback). Transcription goes over
HTTP to a separately operated Whisper service
(`src/yt2wiki/backends/asr_http.py`, engine `asr-http`, registry default for
every profile).

- Selected service: **ahmetoner/whisper-asr-webservice** (community go-to:
  3.3k stars, maintained — v1.10.0, faster-whisper v1.2.1 engine). The adapter
  speaks its `/asr?output=json` contract (multipart `audio_file`, query
  `task/language/word_timestamps/vad_filter`) and validates the response into
  the existing transcript schema (segments, word timestamps, language,
  duration, per-segment confidence metadata, model provenance).
- **Protocol note (explicitly non-OAI):** this service does NOT implement
  OpenAI `POST /v1/audio/transcriptions` (verified 404). An OAI-compatible
  server (e.g. speaches-ai/speaches) could attach via its `verbose_json`
  shape, but ghcr.io was unreachable from the qualification network, so no
  OAI-protocol server was run here. Users who BYO an OAI server get the same
  per-job validation; the pinned example below is the qualified path.
- What is preserved: timestamped segments, word-level timestamps
  (**required** — a wordless response while `word_timestamps` is requested is
  a hard `AsrHttpError`, never a silent downgrade), language (confidence
  from `/detect-language`, `None` when unavailable — never invented),
  duration (WAV header), server identity (`YT2WIKI_ASR_MODEL` declares the
  server-side model for provenance; `/asr` echoes no model id), HTTP
  failures/timeouts/malformed responses with server + status + body excerpt.
- Explicitly unsupported by `/asr` and **recorded per job**
  (`asr_unsupported_options`), never emulated: vocabulary hints / initial
  prompt, beam size, condition-on-previous-text, temperature, translation.
  Structural validation is bidirectional (monotonic finite timestamps,
  bounds, text, word/segment containment and ordering); zero-duration words
  the server emits at audio boundaries are accepted with an `asr_warnings`
  entry, not treated as precise timings.
- Compose example (`docker-compose.yml`, profile `asr`, digest-pinned GPU
  image, persistent `asr-models` cache, no published ports — reachable only
  as `http://asr:9000`): `docker compose --profile asr up -d`. The service
  points at it via `YT2WIKI_ASR_URL=http://asr:9000`, or assign the
  transcription role to a provider and test it. In Docker, browser sessions
  cannot reach the operator console, so configure via the console instead —
  from the host against the loopback UI, or headless:
  `docker exec yt2wiki yt2wiki inference save --file /data/config/<payload>.json`
  then `docker exec yt2wiki yt2wiki inference test --name <asr-provider>`
  (`qualified_for_asr`). `test_endpoint` auto-detects transcription servers
  (no `/models` → `/asr` probe); LLM-only servers stay unqualified for `asr`.
  On multi-GPU hosts pin the card (`device_ids`, not `count: 1`): large-v3
  OOMs on a bare 8 GB card.
- Local faster-whisper remains for explicit use (`--model whisper-large-v3`
  override); without the `asr-local` extra its `load()` fails with an
  actionable error (install the extra or configure the HTTP provider).

---

## 4. Optional llama.cpp sidecar — EXPERIMENTAL, not qualified

Do not bundle inference into the service image. A Compose profile exists for a
local sidecar, but it is **experimental**: the referenced weights are absent
from this machine, no digest was verified here (`docker pull` of the CUDA image
was denied from this network), no 8-image ranking, CUDA-offload check or V9
rubric validation has been performed against a running server, and the flags
below are transcribed from upstream docs, not validated here. Do not represent
this profile as production-ready. The `yt2wiki` service above is independently
usable without it.

```bash
# Start the base service only (the supported deployment):
docker compose up -d yt2wiki

# Only to attempt your own qualification (requires NVIDIA + staged weights).
# Copy your already-available GGUFs into the named volume (NOT the host's
# /data/models — that path is inside the yt2wiki container, not the sidecar):
#   docker run --rm -v yt2wiki-models:/models -v /host/gguf:/src:ro alpine \
#     cp /src/Qwen3.5-4B-Q4_K_M.gguf /src/mmproj-F16.gguf /models/
# Do not download another model merely to finish this PR; verify sha256
# against your own reference, then:
docker compose --profile llama up -d
```

`docker-compose.yml` `llama` service:

- Image: `ghcr.io/ggml-org/llama.cpp:server-cuda` (the upstream CUDA 12 server
  build; the old `ggerganov` account is deprecated). **No digest is pinned here
  because none was verified on this machine.** Pin one on your endpoint after a
  successful pull, then replace `image:` with the digest form:
  ```bash
  docker pull ghcr.io/ggml-org/llama.cpp:server-cuda
  docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/ggml-org/llama.cpp:server-cuda
  # image: ghcr.io/ggml-org/llama.cpp@sha256:<verified digest>
  ```
- Volumes: `yt2wiki-models:/models` (read-write, also `YT2WIKI_HOME/models`), `yt2wiki-data:/data:ro`
- Command proposes `--ctx-size 16384 --n-gpu-layers 99 --reasoning off --temp 0.05`
  plus `--mmproj /models/mmproj-F16.gguf`. These flag meanings follow upstream
  llama.cpp docs; they were **not** validated against a running server here.
- `deploy.resources.reservations.devices` reserves 1 NVIDIA GPU for the sidecar only.
- Healthcheck `curl -sf http://127.0.0.1:8081/health`, `restart: unless-stopped`

A real sidecar qualification requires **all** of: actual Qwen3.5-4B Q4_K_M weights
with matching F16 projector, confirmed CUDA offload (no unintended CPU offload),
an actual 8-image ranking inside the V9 context budget with valid rubric JSON, a
successful service-to-sidecar request over the Compose network, and measured peak
VRAM + latency. A mocked HTTP 200 from `httpx.MockTransport` proves routing, not
fit. Until that qualification exists, writer and vision for production use come
from external OpenAI-compatible endpoints (§3).

---

## 5. Network security

The web app has editing (`POST /api/articles/{id}`), uploads (`POST /api/sources/upload`), job execution (`POST /api/jobs/{id}/reprocess`) and inference config (`POST /api/settings/inference`) — all unauthenticated.

- Default `docker-compose.yml` binds `127.0.0.1:8080:8000` so the admin UI is only reachable from the host's loopback. Reach it from your laptop with `ssh -L 8080:localhost:8080 user@host` then `http://localhost:8080`, or via a VPN — but understand what that does **not** do: an SSH tunnel does **not** make the settings API accept your browser. The app sees the Docker gateway address (e.g. `172.18.0.1`), not your browser's loopback, so `/api/settings/inference` still returns `403` through a tunnel. Never change the host binding to `0.0.0.0:8080:8000` — that publishes the unauthenticated UI directly and bypasses any proxy in front of it — and do not tell users a tunnel fixes the guard — it does not.
- `src/yt2wiki/web/app.py` (`LOCAL_PEERS`, `peer_host`, `is_local_peer`) allows inference configuration only from a localhost peer (`127.0.0.1`, `::1`, `::ffff:127.0.0.1`, `testclient`, `localhost`). It does **not** trust `X-Forwarded-For` or `X-Real-IP` — arbitrary client-supplied headers cannot grant admin access, and whitelisting the Docker gateway is explicitly not done. The 403 message points at the operator console instead. For Docker deployments the working management path is `docker exec yt2wiki yt2wiki inference …` (see §3), which the `/settings` page also states in its **Operator only** notice for every non-localhost viewer (with its mutation form disabled).
- For broader LAN operation, put an authenticated boundary in front, e.g. Caddy/Traefik with `basic_auth` or Authelia. Keep the yt2wiki container itself on host loopback or an isolated Docker network — never publish the unauthenticated app port broadly. Example `Caddyfile` (proxy on the host network, upstream via loopback):
  ```
  yt2wiki.lan {
      basicauth * {
          admin $2a$14$...
      }
      reverse_proxy 127.0.0.1:8080
  }
  ```
  Or give the proxy and yt2wiki an isolated network (`docker network create yt2wiki-net`, attach both, no published `ports:` on yt2wiki) and proxy to `http://yt2wiki:8000` inside that network. Test that unauthenticated `curl http://yt2wiki.lan/api/settings/inference` returns `401` from the proxy. Note the proxy does **not** make browser sessions localhost peers: even behind an authenticated proxy, settings save/test from a browser still requires the operator console unless authenticated management is implemented (the localhost guard intentionally stays).
- Secrets: `api_key_env` names an env var (`YT2WIKI_GEMMA_API_KEY`) that is set via host env or `env_file` with `0600` permissions, not baked into the image or compose. `inference.json` stores only the name, and job `config_json` stores `url`/`model`/`api_key_env` name, not the key. Logs and exports never include keys.

Do not remove the localhost guard merely because requests arrive from a container network address.

---

## 6. Portainer

Import `docker-compose.portainer.yml` as a Portainer Stack (or paste
`docker-compose.yml` itself — both pull the same GHCR image, no build step):

- **Prebuilt image.** The stack pulls `ghcr.io/mitchins/yt2wiki:main`
  automatically; pin releases with the `YT2WIKI_IMAGE_TAG` stack variable
  (`:vX.Y.Z` or `:sha-<commit>`, see docs/releases.md).
- One required service `yt2wiki`, one **experimental** `llama` (enable the
  `llama` profile in Portainer's stack options only to attempt your own
  qualification — §4).
- Persistent named volumes `yt2wiki-data` and `yt2wiki-models` (or replace with bind mounts for an existing library — see below).
- Environment-variable-based settings: `YT2WIKI_HOME`, `YT2WIKI_PROFILE`, `HF_HOME`.
- Provider keys via Portainer stack variables (Stack → Environment variables →
  Advanced mode): define `YT2WIKI_OPENAI_API_KEY`, `YT2WIKI_GEMMA_API_KEY`, …,
  and the compose mapping `YT2WIKI_OPENAI_API_KEY=${YT2WIKI_OPENAI_API_KEY:-}`
  interpolates your value into the container environment at deploy time. The
  container receives the resolved value; `inference.json` stores only the name.
- Health and `restart: unless-stopped` policies.
- No mandatory GPU for `yt2wiki`; GPU reservation only for `llama`.

Connecting existing LAN inference providers instead of the sidecar: in Portainer set
`YT2WIKI_PROFILE` and the provider key stack variables above, then from the
endpoint shell run `docker exec yt2wiki yt2wiki inference save --file …` pointing
`writer`/`vision` at your `http://192.168.1.x:8000` providers, then
`docker exec yt2wiki yt2wiki inference test --name …` to qualify. The Settings UI
cannot save/test from a browser session (see §3/§5) — use the operator console.

Migration-free installation for an already existing article library:

```bash
# On the host, the library lives at ~/.local/share/yt2wiki (articles/db/frames/uploads)
# In Portainer, when creating the stack, change the volume for yt2wiki from
#   yt2wiki-data:/data
# to a bind mount:
#   /home/mitch/.local/share/yt2wiki:/data
# No database migration is needed — `Database.migrate()` is additive and never lowers `schema_version`.
```

After switching the mount, `GET /library` shows the existing guides, and `GET /articles/{id}` renders them without loading a model.

WAL-safe backup — one tested procedure. Never copy the bare `.sqlite3` file while
`-wal`/`-shm` sidecars may exist; use SQLite's online `.backup`, which reads
through the WAL into a consistent snapshot. `docker exec`/`docker cp` need a
container (running or stopped) — they do not operate on host paths directly.
Stop writers first so the database snapshot and the article files pair exactly;
copying either store while jobs or edits continue cannot be point-in-time
consistent.

The database references files beyond `articles/`: the rows' `upload_path` /
`video_path` live under `uploads/` and `media/`, frames under `frames/`, and the
endpoint configuration under `config/`. Back all of them up together (`work/`
job scratch and `logs/` are ephemeral; `models/` is a re-downloadable cache and
is excluded deliberately):

```bash
TAG=$(date +%F)
mkdir -p backup-$TAG && cd backup-$TAG   # host dir must be writable by UID 1000:
                                         # the container runs as `yt2wiki` (1000), and bind
                                         # mounts carry host ownership — a root-owned output
                                         # dir fails the backup with "Permission denied"
docker stop yt2wiki   # quiesce writers; on next start queued jobs resume and
                      # running jobs are marked `interrupted` for explicit retry (not auto-retried)
# Online backup of the stopped volume (writers quiet, WAL read handled by .backup):
docker run --rm -v yt2wiki-data:/data -v "$PWD":/out ghcr.io/mitchins/yt2wiki:main \
  sqlite3 /data/db/yt2wiki.sqlite3 ".backup '/out/yt2wiki-backup-$TAG.sqlite3'"
# All persistent media, frames, article files and config from the stopped container:
for d in articles uploads frames media config; do
  docker cp "yt2wiki:/data/$d" "./yt2wiki-backup-$TAG-$d"
done
docker start yt2wiki
# Verify the snapshot (host sqlite3 is fine — the backup is a plain file),
# including that every referenced media file is actually present:
sqlite3 "yt2wiki-backup-$TAG.sqlite3" "PRAGMA integrity_check;"
# → ok
python3 - <<'EOF'
import sqlite3
import sys
from pathlib import Path
TAG = "REPLACE_WITH_DATE_TAG"  # e.g. 2026-10-23
root = Path(".")
conn = sqlite3.connect(f"yt2wiki-backup-{TAG}.sqlite3")
missing, superseded = [], []
for article_id, in conn.execute("SELECT article_id FROM articles"):
    for name in ("article.md", "article.html"):
        p = root / f"yt2wiki-backup-{TAG}-articles" / article_id / name
        if not p.is_file():
            missing.append(str(p))
for sid, up, vp in conn.execute("SELECT source_id, upload_path, video_path FROM sources"):
    if vp:
        # Ingested: media/video.mp4 is the live file; the old upload path is
        # superseded staging and is only excused when this video exists.
        p = root / f"yt2wiki-backup-{TAG}-media" / Path(vp).relative_to("/data/media")
        if not p.is_file():
            missing.append(str(p) + f" (live video for {sid})")
        elif up and not (root / f"yt2wiki-backup-{TAG}-uploads" / Path(up).name).is_file():
            superseded.append(f"{up} (moved to {vp})")
    elif up:
        p = root / f"yt2wiki-backup-{TAG}-uploads" / Path(up).name
        if not p.is_file():
            missing.append(str(p) + f" (no replacement video for {sid})")
if missing:
    print("BACKUP VALIDATION FAILED — missing live files:")
    print("\n".join(f"  - {m}" for m in missing))
    sys.exit(1)
print("backup validation ok: all", conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
      "sources and", conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
      "articles resolve to present files")
for s in superseded:
    print("  superseded staging (ok):", s)
EOF
# → "backup validation ok: …" or a FAILED list with a non-zero exit
```

The check inspects `upload_path` and `video_path` together: a missing upload is
accepted as superseded staging **only** when its row carries an existing
`video_path`. Anything else absent — an article file, an un-ingested upload, or
a video — fails the validation outright (non-zero exit), never a reassuring
success message.

Restore into a fresh volume (same steps work for the same volume):

```bash
docker volume create yt2wiki-restored
docker run --rm -v yt2wiki-restored:/data ghcr.io/mitchins/yt2wiki:main mkdir -p /data/db
docker stop yt2wiki
docker run --rm -v yt2wiki-restored:/data -v "$PWD":/out ghcr.io/mitchins/yt2wiki:main \
  sh -c "cp '/out/yt2wiki-backup-$TAG.sqlite3' /data/db/yt2wiki.sqlite3 \
         && rm -f /data/db/yt2wiki.sqlite3-wal /data/db/yt2wiki.sqlite3-shm \
         && sqlite3 /data/db/yt2wiki.sqlite3 'PRAGMA integrity_check;'"
# → ok
for d in articles uploads frames media config; do
  docker run --rm -v yt2wiki-restored:/data -v "$PWD":/out ghcr.io/mitchins/yt2wiki:main \
    sh -c "mkdir -p /data/$d && cp -a '/out/yt2wiki-backup-$TAG-$d/.' /data/$d/"
done
# Restored files must be writable by the service UID (1000 `yt2wiki`):
docker run --rm --user root -v yt2wiki-restored:/data ghcr.io/mitchins/yt2wiki:main \
  chown -R 1000:1000 /data/db /data/articles /data/uploads /data/frames /data/media /data/config
docker run -d --name yt2wiki-restored -p 127.0.0.1:18081:8000 -v yt2wiki-restored:/data ghcr.io/mitchins/yt2wiki:main
curl -sf http://127.0.0.1:18081/health && curl -sf http://127.0.0.1:18081/library | grep Library
docker start yt2wiki   # original service back up
```

Host-native equivalent (pip install — stop the service first, same WAL rule;
`chown` only matters when UIDs differ, e.g. restoring as root):

```bash
# stop yt2wiki serve (Ctrl-C / systemctl), then:
sqlite3 "$YT2WIKI_HOME/db/yt2wiki.sqlite3" ".backup 'yt2wiki-backup-$(date +%F).sqlite3'"
for d in articles uploads frames media config; do
  cp -a "$YT2WIKI_HOME/$d" "yt2wiki-backup-$(date +%F)-$d"
done
sqlite3 "yt2wiki-backup-$(date +%F).sqlite3" "PRAGMA integrity_check;"
# → ok
```

(The Settings → Library panel shows the data folder, DB path/size and revision
count for the same purpose. Article bodies are also re-exportable from the DB,
so a DB-only backup already preserves the text; the articles copy adds the
rendered files and assets. This exact container procedure — stop, backup,
articles copy, integrity check, restart, library still serving — is exercised in
§7 item 10.)

---

## 7. Qualification

### Mode A — service-only (no GPU, no model server)

```bash
docker run -d --name yt2wiki-a -p 127.0.0.1:8080:8000 -v yt2wiki-data:/data ghcr.io/mitchins/yt2wiki:main
curl -sf http://127.0.0.1:8080/health
curl -sf http://127.0.0.1:8080/library | grep Library
# inside container:
docker exec yt2wiki-a pip list | grep -v torch  # no torch
docker exec yt2wiki-a python -c "import yt2wiki; print(yt2wiki.__version__)"
# Browser: http://localhost:8080 (via SSH tunnel) — library, search, read, edit, revisions, exports
# Stop/start:
docker stop yt2wiki-a && docker start yt2wiki-a && curl -sf http://127.0.0.1:8080/health
docker logs yt2wiki-a  # no model load, bounded 10m*3
```

- Browse/search/read/edit/export/restart all pass (pip TestClient and container `curl` both show `Pip Test Article`, `200` on `/api/search`, `/api/articles/{id}/export.md`, `/health`).
- Submitting a job with providers offline yields `state failed` with `error` (explicit, recoverable via Retry), not a silent local model load.

### Mode B — service plus external inference (mocked)

Mocked OpenAI-compatible endpoints prove routing:

```bash
pytest tests/test_remote_inference.py -v  # writer=True vision=True via httpx MockTransport
```

The test captures the job's snapshotted `inference` (`writer.url`, `vision.url`) and asserts
`httpx.post` was called to the configured provider with the correct `model` and 8-image payload.

Where qualified real endpoints are available (e.g. a vLLM at `http://192.168.1.14:8000`), a bounded
cached-media V9 smoke can be run:

```bash
HF_HOME=/data/models YT2WIKI_HOME=/data yt2wiki run --profile compact <source-id>
```

If no fully qualified writer/vision combination is available, report that limitation — do not claim
full production inference qualification based on mocked tests.

### Management path and credential routing — demonstrated in the running container

Locally built image of the same Dockerfile (`1.16 GB`, `1a79ea27c4c6`), container `yt2wiki-demo`,
host port `18080 → 8000` (host `8080` was occupied; same loopback binding),
`--env-file` with `YT2WIKI_MOCK_KEY=test-secret-xyz`, volume `yt2wiki-demo-data`:

- `GET /health` → `{"ok":true}`; `docker exec … whoami` → `yt2wiki` (uid 1000);
  `pip list` shows no `torch`/`transformers`; `import torch` → `ModuleNotFoundError`.
- `GET /settings` from the host (gateway peer `172.17.0.1`) renders the
  **Operator only** notice with `data-remote` and the `docker exec` commands;
  `POST /api/settings/inference` from the host → `403` with the operator-console
  message (also with `X-Forwarded-For: 127.0.0.1` — headers never grant access).
- `docker exec yt2wiki-demo curl … http://127.0.0.1:8000/api/settings/inference`
  (localhost peer) → `200`.
- `docker exec yt2wiki-demo yt2wiki inference save --file /tmp/payload.json`
  (provider `mock` → `http://172.17.0.1:8931/v1`, `api_key_env: YT2WIKI_MOCK_KEY`)
  then `yt2wiki inference test --name mock` → `ok: true`,
  `qualified_for_writer/…_vision: true`. The mock logged
  `Authorization: Bearer test-secret-xyz` on `/models` and `/chat/completions`;
  `/data/config/inference.json` contains only the variable name (`grep -c` for the
  secret → `0`).
- CPU ASR inside the same container (no GPU, no torch): faster-whisper
  `whisper-small` cold load `11.7 s` (first download to `/data/models`),
  transcribe of the 11 s 16 kHz fixture `2.3 s` (RTF `4.7×`), peak RSS `0.8 GB`,
  correct transcript with word-level timestamps — proving new-video ASR works
  on the GPU-free service.

### CPU ASR — qualified on real audio (host, CUDA hidden)

Fixture: `jfk.flac` (OpenAI whisper sample, 11 s, sha256
`63a4b1e4…9a4a`) → `ffmpeg -ar 16000 -ac 1` → 16 kHz mono PCM16 (extraction proven),
`CUDA_VISIBLE_DEVICES=""` throughout:

| model | compute | cold load | transcribe 11 s | RTF | peak RSS | transcript |
|---|---|---|---|---|---|---|
| faster-whisper-small | `int8` (registry `cpu_compute_type`) | 1.2 s | 2.0 s | 5.5× | 1.18 GB | correct + word timestamps |
| faster-whisper-large-v3 (default) | `int8` (registry `cpu_compute_type`) | 4.8 s (68 s incl. first download) | 9.3 s | 1.18× | 4.3 GB | correct + word timestamps |

- The previous GPU default (`int8_float16`, also `float16` for enhanced) fails
  hard on CPU: `ValueError: Requested int8_float16 compute type, but the target
  device or backend do not support efficient int8_float16 computation.` The
  backend now substitutes the registry's explicit `cpu_compute_type: int8` on
  `device == "cpu"` and records the effective type in the load report; GPU
  defaults are untouched.
- No hidden CUDA requirement: `import faster-whisper` leaves `torch` unimported;
  `torch` in this dev venv arrives only via the optional guarded import in
  `yt2wiki/gpu.py`, and the container image has no `torch` at all.
- Speed/quality: `int8` is CTranslate2's recommended CPU quantization; large-v3
  stays the default for accuracy, at ~1× realtime on CPU. A registry fingerprint
  change invalidates cached `transcribe` outputs once (by design — tuning keys);
  rerunning jobs retranscribes.
- Consequence for the service: new-video ASR **works** GPU-free (small or
  large-v3 on CPU). No external ASR capability is required.

### Optional sidecar — NOT qualified (do not use for production claims)

The `llama` profile remains in the Compose files as an experimental staging for
your own qualification (§4). Not performed here: no Qwen3.5-4B weights (none
downloaded, per scope), no digest pinned (`docker pull …:server-cuda` denied
from this network), no validated flags, no 8-image ranking, no VRAM/latency
numbers. The gateway/mock qualification above covers routing only.

---

## 8. Remaining blockers for Gemma 4 26B and Halogen

These are separate bounded follow-ups, not prerequisites for a working library deployment.

- **Gemma 4 26B:** The earlier basic probe passed, but the full V9 JSON contract (strict `response_format: json_object` plus 8-image rubric) previously failed 68% of the time on the deployed serving stack. Need to test the actual deployed stack's structured-output mechanisms (guided_json, json_object, plain) against the full `text-9b` writer and `vl-8b` judge contracts. Basic probe is not production qualification.
- **Halogen Qwen3.8-Flash-Next:** Discover the existing inference endpoint from local operator configuration (check `~/.config/halogen`, `env | grep HALOGEN`, `ps aux | grep halogen`). Qualify structured writing first via `_qualify_writer`; test image input only if the Halogen deployment exposes `/chat/completions` with `image_url`. Keep Hermes orchestration separate from the inference API. Writer-only qualification is valid.

No replacement infrastructure is started for these.

---

*Generated for P2 — pip, Docker, Portainer, and mode A/B qualification. See `docker-compose.yml`, `Dockerfile`, and `tests/test_remote_inference.py` / `tests/test_upload_concurrent.py`.*
