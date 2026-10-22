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
export YT2WIKI_HOME=/tmp/p2b-home   # or ~/.local/share/yt2wiki
mkdir -p "$YT2WIKI_HOME"
YT2WIKI_HOME=/tmp/p2b-home /tmp/pip-test/bin/yt2wiki --help
YT2WIKI_HOME=/tmp/p2b-home /tmp/pip-test/bin/yt2wiki info     # shows profile, fingerprint, no CUDA
YT2WIKI_HOME=/tmp/p2b-home /tmp/pip-test/bin/yt2wiki serve --host 127.0.0.1 --port 8080
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

Evidence: ` /tmp/p2b-dist/yt2wiki-0.1.0-py3-none-any.whl` (217 kB), `tests/test_upload_*` and the
` /tmp/test_pip_qualification.py` script (6/6 passed plus full suite 202 passed).

Do not publish to PyPI without a separate licence decision — the project has no selected distribution licence.

---

## 2. Thin service container

**Base image:** `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70`
(pinned digest, `124 MB` base). The `yt2wiki:0.1.0` image is `~1.16 GB` including
`ffmpeg`/`tesseract` and the Python deps (`fastapi`, `uvicorn`, `jinja2`, `pillow`,
`httpx`, `yt-dlp`, `onnxruntime`, `ctranslate2`, `av`, `numpy`, `faster-whisper` without `torch`).

Contents verified:

```bash
docker run --rm yt2wiki:0.1.0 pip list | grep -E "torch|transformers"  # no output
docker run --rm yt2wiki:0.1.0 yt2wiki --help
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
- `recover()` on startup rebuilds the job queue, so interrupted jobs are retried after `docker stop/start`
- Healthcheck: `curl -sf http://127.0.0.1:8000/health` (30s interval, 15s start period)
- Bounded logs: `json-file` with `max-size 10m` `max-file 3`
- Serves the library without loading any model (`GET /health`, `GET /library`, `GET /articles/{id}` all work with providers offline)

Build:

```bash
docker build -t yt2wiki:0.1.0 .
docker images yt2wiki:0.1.0  # ~1.16 GB
```

Run (bind to host loopback, data persists):

```bash
docker run -d --name yt2wiki -p 127.0.0.1:8080:8000 -v yt2wiki-data:/data yt2wiki:0.1.0
# or bind-mount an existing library without migration:
# -v /home/mitch/.local/share/yt2wiki:/data
curl -sf http://127.0.0.1:8080/health
curl -sf http://127.0.0.1:8080/library | grep "Library"
docker exec yt2wiki whoami
docker exec yt2wiki ls -lh /data/db
docker stop yt2wiki && docker start yt2wiki && curl -sf http://127.0.0.1:8080/health
```

Inside the container the app listens on `0.0.0.0:8000` so Docker can forward
host `127.0.0.1:8080` → container `8000`. Host-side binding to `127.0.0.1` is the
security boundary — do not publish the admin UI on `0.0.0.0` without an
authenticated proxy (see §4).

---

## 3. Service / model separation

Default inference is **external OpenAI-compatible providers** (`src/yt2wiki/web/inference.py`).

- Configuration lives in `/data/config/inference.json` (`providers: {name: {url, model, api_key_env}}`, `roles: {writer, vision}`) and status in `inference_status.json`. Secrets are never stored: `api_key_env` is the *name* of an env var (`^YT2WIKI_[A-Z0-9_]{1,56}$`), validated by `validate_api_key_env`, not the key itself. Compose files never hardcode keys.
- `yt2wiki serve` does not require `LLAMA_SERVER_DIR` when providers are remote. `LlamaServerVLM` is only instantiated when `config/models.yaml` assigns `engine: llamacpp-server` and the pipeline requests that role; the web app never silently loads a local Transformers model when `inference.json` points to a remote URL. `backends/loader.py` selects `RemoteOpenAIBackend` when the job's snapshotted `inference` config contains a provider URL.
- The complete web workflow honours the snapshotted provider endpoints: `submit_upload` → `enqueue` captures `_inference_snapshot()` (`writer.url/model`, `vision.url/model`, `reachable/qualified_for_*`) into the job's `config_json`; later `run_job` uses that snapshot, not live settings, so changing providers does not rewrite running jobs.
- A hosted model may qualify for writer only, vision only, or both — `test_endpoint` qualifies each role via real generation (JSON writer probe and 2-image rubric judge) and writes `qualified_for_writer`/`qualified_for_vision` to `inference_status.json`. Do not infer capability from a successful `/models` connection.
- The service starts and serves the library when all providers are offline. `yt2wiki info` prints `cuda available : False` without importing torch, and `GET /library`/`/articles` work. Submitting a job that needs an unavailable model yields an explicit `failed` state with `error` and `findings` (e.g. `model_loads` row with `ok=0`), not a silent fallback or a hanging queue. The job can be retried after the provider returns.

Verification:

- `yt2wiki info` with no `LLAMA_SERVER_DIR` and no provider still prints info (no `llama-server binary not found` error at serve time unless a job actually needs the llamacpp engine).
- Mocked OpenAI endpoints (`httpx` `MockTransport` returning `{"choices":[{"message":{"content":"{\"ok\": true}"}}]}` and valid `RUBRIC_PROMPT` JSON) prove `writer` and 8-image `vision` requests are routed to the configured URLs (`tests/test_remote_inference.py`).
- Stopping the mock servers and submitting a job shows `state failed` with recoverable error, while `GET /library` still serves.

Keep secrets out of exported articles and logs: `article.standalone.html` and `article.zip` contain only markdown/assets, never `inference.json` or env values; `app_log` and `model_loads` store `model_key`/`repo`, not API keys.

---

## 4. Optional llama.cpp sidecar

Do not bundle inference into the service image. Use a separate Compose profile:

```bash
# Stage models once (persistent, not auto-downloaded at service start)
mkdir -p /data/models
# Place the already-qualified Qwen3.5-4B files:
#   /data/models/Qwen3.5-4B-Q4_K_M.gguf
#   /data/models/mmproj-F16.gguf
# Verify sha256 if you have a reference.

# Start the base service only:
docker compose up -d yt2wiki

# Start the visual sidecar (requires NVIDIA):
docker compose --profile llama up -d
# or Portainer: enable the "llama" profile when deploying the stack
```

`docker-compose.yml` `llama` service:

- Image: `ghcr.io/ggerganov/llama.cpp:server` — pin after pull via `docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/ggerganov/llama.cpp:server` and replace `image:` with the `@sha256:` form for reproducibility.
- Volumes: `yt2wiki-models:/models` (read-write, also `YT2WIKI_HOME/models`), `yt2wiki-data:/data:ro` (no Docker socket inside yt2wiki)
- Command uses the V9-Z4 qualified settings: `--ctx-size 16384 --n-gpu-layers 99 --reasoning off --reasoning-budget 0 --temp 0.05` plus `--mmproj /models/mmproj-F16.gguf` and production `--vision_image_tokens 384` equivalent (1280×720 → ~384 tokens). 8-image ranking fits in 16384.
- `deploy.resources.reservations.devices` reserves 1 NVIDIA GPU for the sidecar only; the `yt2wiki` service has no GPU reservation and runs without `--gpus`.
- Healthcheck `curl -sf http://127.0.0.1:8081/health`, `restart: unless-stopped`
- No auto-download of large weights when running the base service; the sidecar only starts when the `llama` profile is selected. No sophisticated JIT orchestration — if the llama.cpp build has reliable idle sleep, it can be used; otherwise leave the server running normally.

The sidecar is qualified for vision only. Its visual qualification does not prove it is a qualified article writer — writer and vision are separate qualifications (see §3).

---

## 5. Network security

The web app has editing (`POST /api/articles/{id}`), uploads (`POST /api/sources/upload`), job execution (`POST /api/jobs/{id}/reprocess`) and inference config (`POST /api/settings/inference`) — all unauthenticated.

- Default `docker-compose.yml` binds `127.0.0.1:8080:8000` so the admin UI is only reachable from the host's loopback. Access it via `ssh -L 8080:localhost:8080 user@host` then `http://localhost:8080`, or via a VPN. Do not change the host binding to `0.0.0.0:8080:8000` without an authenticated proxy.
- `src/yt2wiki/web/app.py:_require_local` checks `request.client.host` is `127.0.0.1`, `::1`, `::ffff:127.0.0.1`, `testclient` or `localhost`. It does **not** trust `X-Forwarded-For` or `X-Real-IP` — arbitrary client-supplied headers cannot grant admin access. Under Docker the forwarded host is the gateway (e.g. `172.18.0.1`), so a direct `curl` from the host's network to `0.0.0.0:8080` would still be blocked for `/api/settings/inference` — this is intentional. Use `docker exec yt2wiki curl -sf http://127.0.0.1:8000/api/settings/inference` or SSH tunnel to the host's loopback, not a raw LAN exposure.
- For broader LAN operation, put an authenticated boundary in front, e.g. Caddy/Traefik with `basic_auth` or Authelia, then proxy to `yt2wiki:8000`. Example `Caddyfile`:
  ```
  yt2wiki.lan {
      basicauth * {
          admin $2a$14$...
      }
      reverse_proxy yt2wiki:8000
  }
  ```
  Test that unauthenticated `curl http://yt2wiki.lan/api/settings/inference` returns `401` from the proxy, while authenticated requests succeed and are still subject to yt2wiki's own localhost check only when the proxy is on the same host network (configure the proxy to connect via `127.0.0.1` inside the host network, or run the proxy as the trusted entrypoint and keep yt2wiki on an internal network).
- Secrets: `api_key_env` names an env var (`YT2WIKI_GEMMA_API_KEY`) that is set via host env or `env_file` with `0600` permissions, not baked into the image or compose. `inference.json` stores only the name, and job `config_json` stores `url`/`model`/`api_key_env` name, not the key. Logs and exports never include keys.

Do not remove the localhost guard merely because requests arrive from a container network address.

---

## 6. Portainer

Import `docker-compose.portainer.yml` as a Portainer Stack:

- One required service `yt2wiki`, one optional `llama` (enable the `llama` profile in Portainer's stack options).
- Persistent named volumes `yt2wiki-data` and `yt2wiki-models` (or replace with bind mounts for an existing library — see below).
- Environment-variable-based settings: `YT2WIKI_HOME`, `YT2WIKI_PROFILE`, `HF_HOME`; provider keys via env names.
- Health and `restart: unless-stopped` policies.
- No mandatory GPU for `yt2wiki`; GPU reservation only for `llama`.

Connecting existing LAN inference providers instead of the sidecar: in Portainer set
`YT2WIKI_PROFILE` and create `/data/config/inference.json` (via the Settings UI or `docker exec`) pointing `writer`/`vision` to your `http://192.168.1.x:8000` providers, then `POST /api/settings/inference/test` to qualify.

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

---

## 7. Qualification

### Mode A — service-only (no GPU, no model server)

```bash
docker run -d --name yt2wiki-a -p 127.0.0.1:8080:8000 -v yt2wiki-data:/data yt2wiki:0.1.0
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

### Optional sidecar — real multimodal ranking

On the qualified NVIDIA hardware (`nvidia-smi` shows 5060/5060 Ti/3060), with the sidecar running:

```bash
docker compose --profile llama up -d
curl -sf http://127.0.0.1:8081/health
# Run the 2-image rubric probe used for qualification (see web/inference.py:_qualify_vision)
python -m tests.test_llama_sidecar  # 8-image ranking fits in 16384, no OOM
docker logs yt2wiki-llama | grep "llamacpp.*resident"
```

No model bake-off is performed here — the sidecar's visual qualification is the already-recorded
V9-Z4 result (Qwen3.5-4B Q4_K_M, ctx 16384, n_gpu_layers 99, reasoning off, 384 tokens). Its
visual qualification does not automatically prove writer qualification.

---

## 8. Remaining blockers for Gemma 4 26B and Halogen

These are separate bounded follow-ups, not prerequisites for a working library deployment.

- **Gemma 4 26B:** The earlier basic probe passed, but the full V9 JSON contract (strict `response_format: json_object` plus 8-image rubric) previously failed 68% of the time on the deployed serving stack. Need to test the actual deployed stack's structured-output mechanisms (guided_json, json_object, plain) against the full `text-9b` writer and `vl-8b` judge contracts. Basic probe is not production qualification.
- **Halogen Qwen3.8-Flash-Next:** Discover the existing inference endpoint from local operator configuration (check `~/.config/halogen`, `env | grep HALOGEN`, `ps aux | grep halogen`). Qualify structured writing first via `_qualify_writer`; test image input only if the Halogen deployment exposes `/chat/completions` with `image_url`. Keep Hermes orchestration separate from the inference API. Writer-only qualification is valid.

No replacement infrastructure is started for these.

---

*Generated for P2 — pip, Docker, Portainer, and mode A/B qualification. See `docker-compose.yml`, `Dockerfile`, and `tests/test_remote_inference.py` / `tests/test_upload_concurrent.py`.*
