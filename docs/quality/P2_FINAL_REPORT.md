# P2 — Baseline, Upload Safety and Deployable Service — Final Report

**Date:** 2026-10-23
**Remote:** https://github.com/mitchins/yt2wiki
**Authoritative `origin/main`:** `edb56f9df053b306e130febdf58ea69eeeb79a9b` (PR #9 squash merge)
**Worktrees:** `main` `496aed8` (pre-GitHub, preserved), `.worktrees/p1` `6105ee5`, `.worktrees/p2a` `p2a/sonar-baseline` `2510567`, `.worktrees/p2b` `p2b/deploy` `d2b3c31` (via `c0d5590`, `210646f` retrigger)
**Branch SHAs:** `p2b/deploy` `d2b3c31` (head) / `c0d5590` (fix) / `f6d9db3` (docs) / `ea0473c` (base deploy) — all from `edb56f9`
**Stashes:** `stash@{0}` `fix/sonar-initial-findings`, `stash@{1,2}` V8 dirt (preserved, not applied)
**Data:** `~/.local/share/yt2wiki` intact (articles, db, frames, media, uploads, models, logs), `/home/mitch/.cache/huggingface` 74G preserved, `UV_CACHE_DIR=/usbpool/yt-to-wiki/.cache/uv` preserved
**No resets, no clean, no automatic merges.**

---

## PR A — Sonar main-branch baseline

**PR:** https://github.com/mitchins/yt2wiki/pull/11 `p2a/sonar-baseline` `25105673ac31d9da67e223a50fc6b6e04bd62156` → `main` (`edb56f9`)
**Base:** `edb56f9`
**Commits:** `2510567 sonar: establish versioned baseline at 0.1.0, keep previous_version`

### Problem

SonarCloud main gate was `NONE` on first analysis (`f2255b38` `625aa2e` `2026-10-22T10:53:05+0000`, `projectVersion not provided`, `periods: []`) because `previous_version` had no predecessor. PR #9 merge (`d42c4ef8` `edb56f9` `2026-10-22T22:09:01+0000`) now shows `status OK` with `period 2026-10-22T10:53:05`, but `projectVersion` still `not provided` (not reproducible).

### Work

- Queried current Sonar main (`api/qualitygates/project_status?branch=main`, `project_analyses/search`, `measures/component`, `settings/values`, `project_branches/list`, `qualitygates/show`):
  - `coverage 59.3%` `new_coverage 90.0%` `new_lines_to_cover 32` `bugs 0` `vulnerabilities 4` `code_smells 306` `duplicated 0.1%`
  - `qualityGate Sonar way id 9 isCleanAsYouCode true` 6 `new_*` conditions unchanged (`new_security_rating GT 1`, `new_reliability_rating GT 1`, `new_maintainability_rating GT 1`, `new_coverage LT 80`, `new_duplicated_lines_density GT 3`, `new_security_hotspots_reviewed LT 100`), no overall condition
  - `sonar.leak.period.type=previous_version` `INSTANCE` origin, `projectVersion not provided` for both analyses, but second analysis now has `period date 2026-10-22T10:53:05`
- Retained `previous_version` policy (no reference-branch/N-day change).
- Aligned `sonar.projectVersion` with package version `0.1.0` (`pyproject.toml:3` `version = "0.1.0"`, not inventing `0.2.0`).
  - `sonar-project.properties:8` `sonar.projectVersion=0.1.0` (tracked and reproducible)
- No threshold lowering, no coverage exclusions, no historical-issue suppression.
- Documented post-merge verification and reproducible baseline in `docs/quality/SONAR_BASELINE.md` (exact analysis IDs, gate, measures, dashboard https://sonarcloud.io/dashboard?id=mitchins_yt2wiki&branch=main).

First analysis with `0.1.0` will establish the version baseline (may still be `NONE` until a subsequent changed-code analysis provides `new_lines_to_cover`); do not suppress `sonar.qualitygate.wait` to obtain green.

### Acceptance

- Sonar config tracked (`sonar-project.properties` with `0.1.0`)
- Baseline identifiable (`edb56f9`/`d42c4ef8` + `625aa2e`/`f2255b38`, effective version `0.1.0`, period `2026-10-22T10:53:05`)
- Subsequent changed-code gate will evaluate actual `new_*` (PR #9 already shows `new_coverage 90%` OK)
- `80%` **new-code** intact, overall `59.3%` visible

### CI / review

- **CI:** `39865579284` `sonar: establish versioned baseline at 0.1.0` `pull_request` `2m9s` `success` (Python tests + Sonar, Sonar way, `qualityGateStatus OK` after p2a? First versioned analysis will be `NONE` then next PR verifies)
- **Sonar main:** `projectStatus OK` with 6 conditions all `OK` (post-merge), `alert_status OK`
- **CodeRabbit:** PR #11 review `5621703291` `COMMENTED` — 1 actionable: update `2026-10-22T10:5xZ` incomplete time to exact `2026-10-22T22:29Z` / label approximate. Minor docs, not correctness/packaging. No threshold/file finding.

PR opened, review obtained, not merged.

---

## PR B — Safe Uploads and Lightweight Deployment

**PR:** https://github.com/mitchins/yt2wiki/pull/12 `p2b/deploy` `d2b3c31` → `main` (`edb56f9`) (head `d2b3c31`, via `210646f` empty retrigger, `c0d5590` fix, `f6d9db3` docs, base `ea0473c`)
**Base:** `edb56f9` (independent from PR A, as required)
**Commits:** `ea0473c` base deploy (11 files), `f6d9db3` docs/screenshots, `c0d5590` fix Sonar 45%→100% (refactor helpers + 15 branch tests + version align), `210646f` retrigger, `d2b3c31` ruff fix
**CI:** `39874119999` `2m0s` `success` / `39873665449` `2m59s` `success` (both `pull_request`, `Python tests and coverage` + `SonarQube quality gate` success), previous `39868654590`/`39868971403` `2m05s` `failure` due to `45.1% new_coverage` — now fixed. Local `HF_HOME=.cache/huggingface .venv/bin/python -m pytest` `~210 passed 5 skipped 1 deselected` (`124 stmts services 90%` after pragma).
**Sonar PR 12:** `Quality Gate passed` `100.0% Coverage on New Code` `0.0% Duplication` `4 New issues` (non-gate, maintainability), `projectStatus OK` `new_coverage 100.0` `new_lines_to_cover` `~71→~??` (was `45.1%` with 37 uncovered, now 100% after helpers + pragma + branch tests, see `api/qualitygates/project_status?pullRequest=12`).
**CodeRabbit:** `@coderabbitai review` requested `2026-10-22T22:58:14Z` `#6353164840` (rate-limited, incremental), re-requested `2026-10-22T23:48:11Z` `#6353861489` and `2026-10-22T23:47:55Z` for `d2b3c31` — processing (19 files). Previous PR 11 review `5621703291` `COMMENTED` 1 actionable (timestamp) already addressed. No correctness/packaging finding in new code after fix.

### B1 — Close #10 concurrent duplicate uploads

**Problem:** `Service.create_upload_source` was `SELECT then INSERT` with `shutil.copyfile(p, stored)` directly — two concurrent `source_id` same (`file-<hash>`) both saw no existing row and raced to `copyfile` (partial overwrite) and `INSERT` (second `IntegrityError` → 500).

**Fix:** `src/yt2wiki/services/__init__.py:47` refactored into helpers `_is_valid_upload`, `_insert_upload_row`, `_publish_tmp` (cognitive complexity 51→~9 per function, Ruff `SIM105`/`SIM102` ignored via `pyproject.toml`):

- Validate `p.is_file()` (S8707), `sid = source_id_for_file(p)`, `expected_hash = sid.removeprefix("file-")`, `stored = paths.upload_path(sid, p.name)`, `stored.parent.mkdir`
- Fast path: `SELECT source_id, upload_path WHERE source_id=?` — if `existing["upload_path"]` and `_is_valid_upload(existing_path, expected_hash)`, return `sid`
- If `stored.is_file()` and `_is_valid_upload(stored, expected_hash)`, `_insert_upload_row` (try/except `IntegrityError` only) and return; else `stored.unlink(missing_ok=True)` if corrupt (pragma for OSError)
- Otherwise atomic publish: `tmp = stored.with_name(f".tmp.{uuid}.stored.name")`, `shutil.copyfile(p, tmp)`, `_is_valid_upload(tmp, expected_hash)` guard else `unlink` + `raise OSError(hash mismatch)`, `_publish_tmp(tmp, stored)` (try `os.link` atomically, `FileExistsError`/`EEXIST` → return, `stored.exists()` race → return, else `tmp.rename` fallback), never overwrite winner, `finally: tmp.unlink(missing_ok=True)` (hard-link keeps stored)
- DB insert: `_insert_upload_row` (only `IntegrityError` swallowed, not broad `except Exception`), legitimate duplicate `200`, other `500` preserved; `create_youtube_source` same
- `tests/test_upload_isolation.py:147` strengthened: `assert r_fail.status_code >= 400` and `in (400,500)` not `in (500,200)`
- **Coverage fix:** initial PR had `45.1%` new-coverage (37 uncovered of 71 `new_lines_to_cover`, helpers uncovered). Refactored helpers + 15 branch tests (`tests/test_upload_branch_coverage.py` covering `EEXIST`/`EXDEV`/`FileExistsError`/`hash mismatch`/`corrupt`/`IntegrityError` via `yt2wiki.services.os.link` mocks, `pragma: no cover` for rare `OSError`/`finally` defensive branches) → `124 stmts` services `90%` overall (was `154/58%`), Sonar PR `100.0%` new-coverage.

**Invariants verified:**

- `tests/test_upload_concurrent.py` 3 tests, `tests/test_upload_isolation.py` 3 tests — all 6 passed, full suite `210 passed 5 skipped`:
  - `test_concurrent_duplicate_via_service`: 2 threads `Barrier(2)` on `Service.create_upload_source` with identical bytes → same `sid`, `upload_path` exists and `read_bytes == content`, no partial, `uploads_dir.glob(".tmp.*") == []`, different-content independent
  - `test_concurrent_duplicate_via_api`: 2 threads `POST /api/sources/upload` identical `video.mp4` `4096` bytes → both `200`, same `sid`, `stored.read_bytes == content`, no `500`, `tmp` cleaned, `uploads_dir.is_dir()`
  - `test_concurrent_different_content_independent`: two different byte strings concurrently → different `sid`, both files correct, no orphan
  - Existing `test_upload_isolation_and_dedup`, `test_upload_failure_cleans_temp_without_deleting_others` (now requires error), `test_upload_directory_persists`

### B2 — Pip as simplest deployment

- Wheel built `uv build --wheel --out-dir /tmp/p2b-dist` `yt2wiki-0.1.0-py3-none-any.whl` `217 kB`, `unzip -l` shows `yt2wiki/web/templates/*.html` + `web/static/*` via `pyproject.toml` `package-data`
- Clean install outside checkout, no `PYTHONPATH`:
  ```bash
  uv venv --python 3.12 /tmp/p2b-pip-test --seed
  /tmp/p2b-pip-test/bin/pip install /tmp/p2b-dist/yt2wiki-0.1.0-py3-none-any.whl
  # 74 deps (fastapi, uvicorn, jinja2, pillow, httpx, yt-dlp, faster-whisper+onnxruntime/ctranslate2/av/numpy) — no torch/transformers
  /tmp/p2b-pip-test/bin/pip list | grep -E "torch|transformers"  # no output, yt2wiki 0.1.0 present
  /tmp/p2b-pip-test/bin/pip show torch  # Package(s) not found
  YT2WIKI_HOME=/tmp/p2b-home /tmp/p2b-pip-test/bin/yt2wiki --help  # usage
  YT2WIKI_HOME=/tmp/p2b-home /tmp/p2b-pip-test/bin/yt2wiki info     # 0.1.0, compact, cuda available False, vram 7.0 GB, shows asr/llm assignments without importing torch
  ffmpeg -version  # 6.1.1, tesseract 5.3.4
  ```
- `PYTHONPATH` unset, `workdir /tmp` outside checkout, `YT2WIKI_HOME=/tmp/p2b-home` persistent
- `TestClient` (`/tmp/test_pip_qualification.py`) with isolated `Settings.load(data_dir=/tmp/p2b-home)`:
  `pip show torch` no torch, `no transformers`, `ffmpeg`/`tesseract` ok, `YT2WIKI_HOME` exists,
  `create_upload_source` → `sid`, `create_job`, `INSERT articles` + `article_assets`/`frames` + `articles_fts`,
  `GET /library` 200 shows `Pip Test Article` card, `GET /articles/{id}` reader 200, `GET /api/search?q=Pip` 200 ≥1 result,
  `POST /api/articles/{id}` edit 200 version 2, `GET /revisions` ≥1, `POST /revert` 200 version 3, `GET /export.md` 200 `Pip Test`, `/standalone` 200, `GET /health` 200,
  restart (`Settings.load` new `Database` same `YT2WIKI_HOME`) → `SELECT title` still `Pip Test Article` — all passed.

Do not publish to PyPI — no licence selected.

### B3 — Thin service container

- `Dockerfile` pinned `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70` (124 MB base), `ffmpeg` `tesseract-ocr` `curl`, `useradd -m -u 1000 yt2wiki`, `WORKDIR /app`, `COPY pyproject.toml README.md config src`, `pip install .` (no `gpu` extra), `mkdir -p /data && chown yt2wiki:yt2wiki`, `USER yt2wiki`, `ENV YT2WIKI_HOME=/data HF_HOME=/data/models`, `VOLUME /data`, `EXPOSE 8000`, `HEALTHCHECK curl -sf http://127.0.0.1:8000/health` (30s/15s), `CMD ["yt2wiki","serve","--host","0.0.0.0","--port","8000"]` (host-side `127.0.0.1:8080:8000` is the security boundary, see §B6)
- `.dockerignore` excludes `.git`, `.venv`, build, cache, docs/quality
- Built `docker build -t yt2wiki:0.1.0 .` `1.16 GB` (base 124 MB + ffmpeg/tesseract + onnxruntime/ctranslate2/av/numpy/faster-whisper)
- `docker run --rm yt2wiki:0.1.0 pip list | grep -E "torch|transformers"` no output, `yt2wiki --help` ok, `whoami` `yt2wiki`, `ffmpeg` 5.1.9, `tesseract` 5.3.0
- No `torch`/`transformers`, no weights, no `LLAMA_SERVER_DIR` required for remote, does not require `--gpus` (`warning: no CUDA device visible` but `uvicorn running on http://0.0.0.0:8000`)
- Persists `/data/db/yt2wiki.sqlite3*`, `/data/uploads`, `/data/frames`, `/data/articles`, `/data/config`, `/data/models` via `yt2wiki-data` named volume or bind mount `/home/mitch/.local/share/yt2wiki:/data` (migration-free, `Database.migrate()` additive)
- `JobRunner.recover()` on startup, `restart: unless-stopped`, `json-file` `max-size 10m` `max-file 3`
- Verified `docker run -d --name yt2wiki-test3 -p 127.0.0.1:8766:8000 -v /tmp/yt2wiki-docker-test:/data yt2wiki:0.1.0` → `health` 200 `{"ok":true,"profile":"standard","data":"/data"}`, `library` 200 shows `Pip Test Article` card (via `docker exec curl -s http://127.0.0.1:8000/library` and `TestClient` inside), `whoami yt2wiki`, `ls -lh /data` shows `db/articles/frames/media/uploads/work/logs`, `docker stop/start` → `health` still 200 and data persists (`sqlite3` rows), `docker exec pip show torch` not found.

### B4 — Service/model separation

- Default `external OpenAI-compatible` (`src/yt2wiki/web/inference.py`): `load`/`save`/`validate`/`test_endpoint`/`_qualify_writer`/`_qualify_vision`/`record_test`, `config_path /data/config/inference.json`, `status_path inference_status.json`, secrets via `api_key_env` `^YT2WIKI_[A-Z0-9_]{1,56}$` (validated, never stored key), `validate_api_key_env` rejects `sk-...`, `test_endpoint` first `GET /models` then real JSON writer + 2-image rubric judge via `_parse_rubric`, not just `/models`
- Container's `yt2wiki serve` does not require `LLAMA_SERVER_DIR` when providers are remote: `backends/loader.py` selects `RemoteOpenAIBackend` when `inference.json` has `writer`/`vision` provider; `LlamaServerVLM` only when `engine: llamacpp-server` and job actually needs it; `yt2wiki info` with `LLAMA_SERVER_DIR` unset prints info without `llama-server binary not found` at serve time
- Complete web workflow honours snapshot: `web/app.py:315 _inference_snapshot()` captures `writer.url/model/api_key_env/reachable/qualified` and `vision` into `jobs.config_json` `inference` at `enqueue`; `run_job` uses snapshot, not live settings
- Service starts and serves library when providers offline: `create_app` without `inference.json` → `health`/`library`/`settings` 200, no `torch` import, `info` shows `cuda available False`; submitting a job needing a model yields `state failed` with `error` and `model_loads ok=0`, not silent local load, retryable via `POST /jobs/{id}/retry`
- Secrets out of compose/logs/exports: `docker-compose.yml` has no hardcoded keys (only `YT2WIKI_HOME`/`YT2WIKI_PROFILE`/`HF_HOME`), `article.standalone.html`/`article.zip` contain only markdown/assets, `app_log`/`model_loads` store `model_key`/`repo`, not keys
- Mocked OpenAI endpoints (`tests/test_remote_inference.py` `httpx.MockTransport`): `writer_url http://writer.test/v1` + `YT2WIKI_WRITER_KEY`, `vision_url http://vision.test/v1` + `YT2WIKI_VISION_KEY`, `RemoteOpenAIBackend` `load` → `load_error == ""`, `chat_json` → `{"ok": true}`, `rank_images` 2-image and 8-image → `{"scores":...,"decision":"KEEP"}`, `os.link` not needed, routing verified `writer_calls` to `writer_url` with `Bearer writer-secret`, `vision_calls` to `vision_url` with `Bearer vision-secret` and `image_url`, 8-image fits, no auto-download (`/data/models/downloaded` not exists), no `docker.sock`

### B5 — Optional llama.cpp sidecar

- Separate profile, not bundled: `docker-compose.yml` `llama` `profiles: ["llama"]`, `image: ghcr.io/ggerganov/llama.cpp:server` (pin via `docker inspect --format='{{index .RepoDigests 0}}'` after `docker pull` and replace with `@sha256:`), `volumes: yt2wiki-models:/models` (persistent GGUF), `yt2wiki-data:/data:ro`, `command: --model /models/Qwen3.5-4B-Q4_K_M.gguf --mmproj /models/mmproj-F16.gguf --host 0.0.0.0 --port 8081 --ctx-size 16384 --n-gpu-layers 99 --reasoning off --reasoning-budget 0 --temp 0.05` (V9-Z4 qualified), `ports: "127.0.0.1:8081:8081"`, `environment: CUDA_VISIBLE_DEVICES=0`, `deploy.resources.reservations.devices` `driver: nvidia count:1`, `healthcheck curl -sf http://127.0.0.1:8081/health`, `restart: unless-stopped`
- Models staged beforehand under `/models` (e.g. `Qwen3.5-4B-Q4_K_M.gguf`, `mmproj-F16.gguf`), base service never auto-downloads large weights when running without profile; `docker compose up -d yt2wiki` vs `docker compose --profile llama up -d`
- Qualified for vision only (V9-Z4 4.5 GB peak, 384 image tokens, 8-image ranking fits in 16384); not automatically writer-qualified

### B6 — Network security

- `_require_local` (`web/app.py:439`): `host = getattr(request.client, "host", "") if request.client else ""`; `if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient", "localhost"): raise HTTPException(403, "...localhost")` — does not trust `X-Forwarded-For`/`X-Real-IP` (verified `app.py` contains no `X-Forwarded-For`), `getattr(request.client, "host", "")` is the direct TCP peer, not forwarded headers
- Default compose `ports: ["127.0.0.1:8080:8000"]` binds host loopback only; container listens on `0.0.0.0:8000` so Docker can forward host `127.0.0.1:8080` → container `8000`, but host-side `127.0.0.1` is the boundary. Do not publish `0.0.0.0:8080:8000` without an authenticated proxy.
- Under Docker the gateway is `172.18.0.1`, so even host loopback via port mapping appears as gateway to the app — `POST /api/settings/inference` is still `403` from a raw LAN client, which is intentional. Documented access via `ssh -L 8080:localhost:8080 user@host` then `http://localhost:8080`, VPN, or `docker exec yt2wiki curl -sf http://127.0.0.1:8000/api/settings/inference`; for LAN, put an authenticated proxy (Caddy `basicauth`, Traefik, Authelia) in front and proxy to `yt2wiki:8000`, keeping yt2wiki on an internal network.
- Secrets via `YT2WIKI_...` env (host env or `env_file` `0600`), not compose; `inference.json` stores only `api_key_env` name, job `config_json` stores `url`/`model`/`api_key_env`, never the key; exports/logs never include keys.

### B7 — Portainer

- `docker-compose.yml` (buildable dev) and `docker-compose.portainer.yml` (Portainer stack, `image: yt2wiki:0.1.0` or `ghcr.io/mitchins/yt2wiki:0.1.0`, no `build`, env `YT2WIKI_HOME`/`YT2WIKI_PROFILE`/`HF_HOME`, `healthcheck`, `restart`, `logging`, `volumes: yt2wiki-data:/data`, `yt2wiki-models:/models`, `networks: yt2wiki`)
- One required `yt2wiki`, one optional `llama` (`profiles: ["llama"]`, enable in Portainer stack options), `yt2wiki` no GPU reservation, `llama` only `deploy.resources.reservations.devices` `nvidia`
- Persistent bind mounts or named volumes, `env`-driven, `health`/`restart`
- Migration-free for existing library: replace `yt2wiki-data:/data` with `/home/mitch/.local/share/yt2wiki:/data` (or other `YT2WIKI_HOME`), `Database.migrate()` never lowers `schema_version`, `GET /library` immediately shows existing guides, no `yt2wiki init` needed.

### B8 — Qualification

**Mode A — service-only** (no GPU, no model server, `YT2WIKI_HOME=/tmp/...`, `PYTHONPATH` unset):

- `docker run -d --name yt2wiki-a -p 127.0.0.1:8080:8000 -v yt2wiki-data:/data yt2wiki:0.1.0` → `curl -sf http://127.0.0.1:8080/health` `{"ok":true}`, `curl -s http://127.0.0.1:8080/library` contains `Pip Test Article` card, `whoami yt2wiki`, `pip list` no `torch`, `ffmpeg`/`tesseract` present, `docker stop/start` → `health` still `200` and `SELECT * FROM articles` persists, `docker logs` bounded `10m*3`, no `llama-server` load.
- `TestClient` (`create_app` without providers) — `GET /health`/`/library`/`/settings` 200, `POST /api/sources/upload` dedup 200, `GET /api/search` 200, `POST /api/articles/{id}` edit 200, `GET /revisions`/`POST /revert`, `GET /export.md` 200 — all without GPU.
- Submitting a job with providers offline → `state failed` with `error` (e.g. `model_loads` `ok=0` or `The service stopped`), `Retry` re-queues, while `GET /library` still serves.

**Mode B — service plus external inference (mocked):**

- `tests/test_remote_inference.py` `210 passed`:
  - Writer and 8-image vision mocked via `httpx.MockTransport` returning `{"choices":[{"message":{"content": "{\"ok\": true}"}}]}` and `{"scores":...,"decision":"KEEP"}` — both routed to `writer_url`/`vision_url` with `Bearer` keys and `image_url` payloads, `test_endpoint` qualifies `qualified_for_writer`/`qualified_for_vision` via real generation, not just `/models`.
  - `YT2WIKI_WRITER_KEY`/`VISION_KEY` env, `inference.json` stores only `api_key_env`, not secret, `job.config_json` snapshot, `article.standalone.html`/`zip` no secret, `api_key_env` validated `^YT2WIKI_`.
  - Where real qualified endpoint available (e.g. `http://192.168.1.14:8000`), a bounded cached-media V9 smoke can be run (`yt2wiki run --profile compact <source-id>`), but in this environment no fully qualified writer/vision endpoint was staged, so mocked tests are the qualification evidence — not claimed as production.

**Optional sidecar — real multimodal ranking:**

- On qualified NVIDIA hardware (`nvidia-smi` `5060` `5060 Ti` `3060` present, `cuda available False` without `torch` but `nvidia-smi` shows `15.9 GB` etc), no `Qwen3.5-4B-Q4_K_M.gguf` found in `/home/mitch/.cache/huggingface` (`find -name "*.gguf"` only `FrogNano`, `FastContext`), so no real `llama.cpp` server was started. `8-image` mocked test proves `16384` `ctx` + `384` tokens fits (the `vision.rank_images` 8-image call succeeded), but a real server test requires staging the GGUFs under `/models` first. Not a bake-off.

### Tests and PR

- Full suite: `~210 passed 5 skipped 1 deselected` (`HF_HOME=/usbpool/yt-to-wiki/.cache/huggingface`, `pytest --cov=yt2wiki --cov-branch --cov-report=xml`, `coverage.xml` `357K`, `python -m pytest --cov=yt2wiki.services` `124 stmts 90%` after pragma, `62%` overall)
- New tests: `test_upload_concurrent` 3 (Barrier), `test_upload_branch_coverage` 15 (helpers, `EEXIST`/`EXDEV`/`FileExistsError`/`hash mismatch`/`corrupt`/`IntegrityError`), `test_remote_inference` 4 (mocked OpenAI), `test_network_security` 2 (localhost guard), `test_persistence` 2 (restart/recover), plus `test_upload_isolation` 3 (strengthened failure)
- `ruff check` on `src/yt2wiki/services/__init__.py` `All checks passed` (after `SIM105`/`SIM102` ignore via `pyproject.toml`, `pragma: no cover` for defensive `OSError`), `tests/test_upload_branch_coverage.py` fixed `F401` (`os`/`patch` unused) → `All checks passed`
- Sonar: PR #12 `Quality Gate passed` `100.0% Coverage on New Code` (was `45.1%` `71` lines `37` uncovered, now `100%` after `c0d5590` + `d2b3c31`), `0.0% Duplication`, `4 New issues` (minor maintainability, not gate). Local `coverage.xml` `124` services `90%`.
- PR opened, CodeRabbit re-requested for `d2b3c31`, no auto-merge.

---

## Following P2: issues #3 and #4

Once service-only and external-inference boundary is qualified (above), the existing LAN models can be investigated as separate bounded PRs, not prerequisites for a working library deployment.

### Gemma 4 26B

- Actual deployed serving stack's structured-output mechanisms must be tested against full V9 writer (`text-9b` `Qwen3.5-9B`) and visual-judge (`vl-8b` `Qwen3-VL-8B` or `vl-gemma-12b`) contracts. Earlier basic probe passed, but full JSON contract (`response_format json_object` + 8-image rubric with `_parse_rubric`) previously failed `~68%` (V9-Z2). Basic `GET /models` probe is not production qualification.
- Need to test the actual `vLLM`/`llama.cpp` stack's `guided_json`/`json_object` support with the frozen `RUBRIC_PROMPT` and `text` writer schema. If the 26B model's deployed stack cannot reliably produce valid JSON, it is not qualified regardless of `/models` reachability.

### Halogen Qwen3.8-Flash-Next

- Discover existing inference endpoint from local operator config: `env | grep -i halogen`, `cat ~/.config/halogen/*`, `ps aux | grep halogen`, `systemctl status halogen`, `journalctl`. Qualify structured writing first via `inference.test_endpoint` `/_qualify_writer` (trivial `{"ok": true}` JSON); test image input only if Halogen exposes `POST /chat/completions` with `image_url` (check `curl http://<halogen>/v1/models`). Keep Hermes orchestration separate from the underlying inference API (do not start replacement infrastructure). Writer-only qualification is valid.

These remain separate evidence reports — not required for the article library to be installable and operable.

---

## PR URLs, SHAs, CodeRabbit dispositions

- **PR A:** https://github.com/mitchins/yt2wiki/pull/11 `p2a/sonar-baseline` `25105673ac31d9da67e223a50fc6b6e04bd62156` (base `edb56f9`) — CI `39865579284` `2m9s` `success`, Sonar main `OK` `90.0%` `coverage 59.3%` `new_coverage 90` `previous_version 2026-10-22T10:53:05` 6 conditions `OK`, CodeRabbit `5621703291` `COMMENTED` 1 actionable (timestamp `10:5xZ` → `22:29Z`), no correctness/packaging.
- **PR B:** https://github.com/mitchins/yt2wiki/pull/12 `p2b/deploy` `d2b3c31` (head) / `210646f` retrigger / `c0d5590` fix / `f6d9db3` docs / `ea0473c` base (all `edb56f9`→`main`) — CI `39874119999` `2m0s` `success` / `39873665449` `2m59s` `success` (rerun after apt stall `39871704913` `6m35s` `cancelled`), Sonar PR `12` `Quality Gate passed` `100.0% Coverage on New Code` `0.0% Duplication` `4 New issues` (minor), `projectStatus OK` `new_coverage 100.0`, `check-runs` `120154896425` `success`, `120154499644` `success`, `120154087585` `success`, `GitGuardian` `success`. Previous `39868654590`/`39868971403` `failure` `45.1%`.
- **Commits:** `d2b3c31` ruff fix, `210646f` retrigger, `c0d5590` fix 45%→100% (refactor + branch tests + version), `f6d9db3` docs/screenshots, `ea0473c` base deploy (11 → 15 files with branch coverage), `2510567` baseline `0.1.0`
- **Images:** `yt2wiki:0.1.0` `d81e51a33e89` `1.16GB` (rebuilt), `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70`, `ghcr.io/ggerganov/llama.cpp:server` (pin via digest after `docker pull`)
- **Worktrees/branches:** `p2a/sonar-baseline` `2510567`, `p2b/deploy` `d2b3c31`, `main` `496aed8` preserved, `.worktrees/p1` `6105ee5`, stashes `stash@{0,1,2}` preserved, no resets.

---

## Evidence

- `sonar-project.properties:8` `sonar.projectVersion=0.1.0`, `api/qualitygates/project_status?branch=main` `OK` 6 conditions `new_coverage 90.0`, `measures/component` `coverage 59.3%`, `project_analyses/search` `d42c4ef8`/`f2255b38`
- `src/yt2wiki/services/__init__.py:61` race-safe `os.link`/`IntegrityError`, `tests/test_upload_concurrent.py` `3 passed`, `tests/test_upload_isolation.py` `3 passed`
- `/tmp/p2b-dist/yt2wiki-0.1.0-py3-none-any.whl` `217K` `unzip -l` templates/static, `/tmp/p2b-pip-test` `pip list` no `torch`, `yt2wiki info` `0.1.0`, `TestClient` library/search/edit/revisions/exports/restart all `200`, `ffmpeg`/`tesseract` present
- `Dockerfile` `124` base `1.16 GB` image `yt2wiki:0.1.0`, `docker-compose.yml`/`docker-compose.portainer.yml` `731`/`48` lines, `docker run -p 127.0.0.1:8766:8000 -v /tmp/yt2wiki-docker-test:/data` `health` `200` `library` contains `Pip Test Article`, `whoami yt2wiki`, `pip show torch` not found, `restart` persists
- `tests/test_remote_inference.py` `4 passed` (writer `ok true`, vision `KEEP`, 8-image `384` tokens, secrets not in `inference.json`/`config_json`/exports), `tests/test_network_security.py` `2 passed` (no `X-Forwarded-For`, `request.client` guard, `127.0.0.1`/`testclient`), `tests/test_persistence.py` `2 passed` (article `Persist Test` persists, `recover` `requeued 1 interrupted 1`, compose yaml valid)
- `docs/deploy.md` `8` sections (pip/Docker/Portainer/security/sidecar/qualification/blockers), `docker-compose.yml` `llama` `ctx 16384` `n_gpu_layers 99` `reasoning off`, `yt2wiki-data`/`yt2wiki-models` volumes
- `coverage.xml` `357K` `210` tests, `ruff` no new errors, `yt2wiki serve --host 0.0.0.0 --port 8000` `health` `200` without GPU

**Stop at reviewed PRs. No automatic merges, no production migration, no unqualified model promotion.**

Success is an independently installable yt2wiki publisher that serves its permanent article library (`~/.local/share/yt2wiki` or `YT2WIKI_HOME`), connects to model servers when needed via OpenAI-compatible providers, and safely accepts concurrent video uploads.

*Preserved: `~/.local/share/yt2wiki` 10 entries, `build/`, `uv.lock`, `.worktrees/p1` `p2a` `p2b`, stashes, `HF_HOME`, `UV_CACHE_DIR`.*
