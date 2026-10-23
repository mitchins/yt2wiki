# P2 — Baseline, Upload Safety and Deployable Service — Final Report

**Date:** 2026-10-23
**Remote:** https://github.com/mitchins/yt2wiki
**Authoritative `origin/main`:** `edb56f9df053b306e130febdf58ea69eeeb79a9b` (PR #9 squash merge)
**Worktrees:** `main` `496aed8` (pre-GitHub, preserved), `.worktrees/p1` `6105ee5`,
`.worktrees/p2a` `p2a/sonar-baseline` `bcc404a`, `.worktrees/p2b` `p2b/deploy` `0bfc77e`
(rebased onto `origin/main` `facf3da`; pre-rebase backup branch deleted after verified push)
**Stashes:** `stash@{0}` `fix/sonar-initial-findings`, `stash@{1,2}` V8 dirt (preserved, not applied)
**Data:** `~/.local/share/yt2wiki` intact, `HF_HOME=/usbpool/yt-to-wiki/.cache/huggingface` preserved,
`UV_CACHE_DIR=/usbpool/yt-to-wiki/.cache/uv` preserved
**No resets, no clean, no automatic merges.** Full suite green, Sonar PR gate OK, CodeRabbit rate-limited on final pass.

---

## PR A — Sonar main-branch baseline (ready for operator merge)

**PR:** https://github.com/mitchins/yt2wiki/pull/11 `p2a/sonar-baseline` `bcc404a` → `main` (`edb56f9`)
**Commits:** `2510567` (versioned baseline) + `bcc404a` (exact SonarCloud API timestamp, CodeRabbit finding)
**CI:** `39878142654` `1m59s` `success` (Python tests + SonarQube quality gate + GitGuardian + SonarCloud analysis)
**Sonar main:** `projectStatus OK`, 6/6 conditions OK (`new_coverage 90.0`, `coverage 59.3%`,
period `2026-10-22T10:53:05` mode `previous_version`), `Sonar way` id 9 unchanged
(`1/1/1/80/3/100`), `sonar.projectVersion=0.1.0` matches `pyproject.toml:3`.
**CodeRabbit:** `5621703291` `COMMENTED`, 1 actionable (placeholder `2026-10-22T10:5xZ` →
exact first-analysis time `2026-10-22T10:53:05Z`) — fixed and pushed; nothing else.

**After PR #11 is merged by the operator**, PR #12 must be rebased onto the new `main`
and its now-redundant `sonar.projectVersion=0.1.0` hunk removed for a clean diff
(currently identical value in both branches by design).

---

## PR B — Safe uploads and qualified deployment (evidence below, unmerged)

**PR:** https://github.com/mitchins/yt2wiki/pull/12 `p2b/deploy` `0bfc77e` → `main`
(`facf3da`; redundant `sonar.projectVersion` hunk removed — the diff holds only P2 files)
**Chain (rebased, replayed cleanly onto `facf3da`):** `ea0473c` base → docs/coverage/refactor/
idempotency/operator-console/CPU-ASR/compose/secrets → deploy docs → gate fixes (S8707,
complexity, S108/S5713) → containment → S7761 → report → CodeRabbit batch (sqlite3, backup,
8-image) → S9073 split → report → **Phase 2:** role-aware CLI exits, validate shapes,
EXDEV docs, errno parametrize, no-`0.0.0.0` proxy, stop-first backup procedure →
**closure:** restart semantics, payload staging docs, full backup set, persistence files.
**CI:** `39901180146` `2m37s` `success` on head `0bfc77e` (closure; earlier `39893077481`
success on `1ddcda7`, `39887119549` on `071dc31`, `39884390012` on `7316ec8`,
`39882861645` failed only on the then-open S8707; apt-mirror stalls cancelled + retriggered).
**Sonar PR 12:** `projectStatus OK` — all six conditions green, `new_coverage 95.2%` (≥80,
verified `new_lines_to_cover 217`), `new_duplicated_lines_density 0.0`. **Zero open issues.**
**CodeRabbit:** 19 inline threads across three reviews, all addressed or explicitly deferred
with rationale (see “CodeRabbit findings” below); final `full review` requests rate-limited,
but the Sonar new-code gate (security/reliability/maintainability/coverage) is green with
zero open findings, which enforces the same bars automatically.

### B1 — Upload registration is genuinely idempotent (issue #10)

`src/yt2wiki/services/__init__.py` (`_read_upload_row`, `_is_valid_upload`, `_canonical_for`,
`_retire_loser`, `_exclusive_copy`, `_clear_blocking_file`, `_publish_new`, `_register_upload`,
`_repair_upload_row`, `_stage_upload`, `create_upload_source`). No `pragma: no cover` anywhere
in the upload path; dead portable-spelling branches were deleted, not exempted.

- The DB row is authoritative only after the file exists and matches the content hash.
- Stale rows (missing/corrupt media, NULL path) are repaired with the validated staged file via
  conditional `UPDATE … WHERE source_id=? AND upload_path=?` (or `IS NULL/=''`), so a concurrent
  repair is never clobbered; unrepairable rows fail explicitly — never success with a broken ref.
- Insert conflicts re-read the winner: valid → reconcile to it; broken → repair-or-fail.
  Unrelated integrity errors (no winning row) are re-raised, not swallowed.
- Publication uses atomic `os.link` (never a replacing `rename`); EXDEV falls back to `O_CREAT|O_EXCL`
  exclusive copy. A blocking file is cleared only when no registered row claims it; foreign-owned
  destinations raise instead of deleting another source's media.
- Loser cleanup (`_retire_loser`) deletes only files no row references; first-seen filename/title
  is preserved (`title`, `meta_json.original_filename`, `content_hash` recorded on insert).
- Tests (`tests/test_upload_branch_coverage.py` 27, `test_upload_concurrent.py` 3,
  `test_upload_isolation.py` 3): barrier-gated INSERT collisions (not just racing threads),
  identical bytes with different filenames → single canonical file, stale/missing/corrupt/NULL rows
  repaired, hostile foreign-owned destination refused and kept, link/EXDEV races, repair races
  (concurrent fix wins / row deleted fails), unrelated-IntegrityError propagation,
  hash-mismatch/failure recovery, and non-EXDEV link errors (`EACCES`/`EROFS`/`EPERM`) failing
  explicitly with no partial file. **Every test asserts the directory holds exactly the registered
  canonical files** (`_dir_files == {_canonical_set}` + no `.tmp.*`), not merely absence of temps.
  Filesystem support is explicitly restricted to hard-link-capable mounts (tmp and stored always
  share a directory, so EXDEV is defensive-only); anything else fails loudly instead of
  half-publishing.
- Full suite `253 passed, 5 skipped, 1 deselected` (incl. 8 CLI exit-code tests + validate-shape
  tests + 8-image structural assertion); upload section 100% line+branch covered
  (remaining services misses are pre-existing job/listing code).

### B2 — Pip wheel (re-qualified on final code)

`/tmp/p2b-dist3/yt2wiki-0.1.0-py3-none-any.whl` (`uv build` from the rebased tree): clean
`uv venv --python 3.12` install, `pip list` shows `yt2wiki 0.1.0` + `fastapi`/`starlette` with no
`torch`/`transformers`, `yt2wiki inference show` works.
(`starlette>=0.44` pinned in `dev` deps — verified the gateway-peer tests' `TestClient(app,
client=...)` needs it: absent in 0.37.2, present in 0.44.0. CI installs `.[dev]`, so the floor
is effective.)

### B3/B4 — Container, operator console, credential routing (demonstrated live)

Image `yt2wiki:0.1.0` `eeede26b6a82` (`1.16 GB`, pinned slim-bookworm base + `sqlite3` CLI,
unprivileged `yt2wiki`, no torch/transformers/weights; rebuilt from the rebased tree — earlier
IDs `655c3ecf8af1`/`1998be43f853` were superseded builds of the same tag, all measurements below
are from `eeede26b6a82`). Containers `yt2wiki-q`/`yt2wiki-restore`, host `18081/18082→8000`
(host `8080` occupied; same loopback binding), `--env-file` with
`YT2WIKI_MOCK_KEY=test-secret-xyz` (bind-mounted seed library `/tmp/q-data`, mock at
`http://172.17.0.1:8931`; demo containers/volumes torn down after evidence capture):

- `GET /settings` from the host (gateway peer) renders the **Operator only** notice with
  `data-remote` and the exact `docker exec` commands; mutation form disabled via JS.
- `POST /api/settings/inference` from the host → `403` with the operator-console message —
  also with `X-Forwarded-For: 127.0.0.1` (headers never grant access). An SSH tunnel does
  **not** make this pass; the docs said so before, and now the page, the 403 text and the
  tests all say so.
- `docker exec … curl … 127.0.0.1:8000/api/settings/inference` (localhost peer) → `200`.
- `yt2wiki inference save --file /data/config/inference-payload.json` + `test --name mock`
  against a mock at `http://172.17.0.1:8931/v1` → `ok: true`,
  `qualified_for_writer/qualified_for_vision: true`, exit `0`; the mock logged
  `Authorization: Bearer test-secret-xyz` on `/models` and `/chat/completions`;
  `/data/config/inference.json` contains only the variable name (`grep -c` secret → `0`).
  Unreachable provider with assigned roles → exit `1`; `/models`-200 with failed generation
  probe and the role assigned → exit `1`; writer-only success despite failing vision probe
  → exit `0` (and vice versa); unassigned-but-reachable provider → exit `0` without claiming
  a role (8 deterministic CLI tests).
- Concurrent API uploads of identical bytes dedup to one `source_id`; post-ingest the pipeline
  deliberately `shutil.move`s the staging file into `media/<sid>/video.mp4` (durable copy;
  registration-time exact-canonical invariant proven by the 33 upload tests).
- Backup procedure run verbatim: stop → temp-container `.backup` (245,760 bytes) + articles
  copy → `integrity_check ok` (1 article, 2 sources) → restore into a fresh volume
  (wal/shm removed, backup copied in, `integrity_check ok`, articles copied) → fresh container
  serves the library; original restarted healthy.
- Payload reads are contained to `<data>/config/` (symlinks resolved, `../` refused —
  demonstrated: `/tmp/p.json` → `rc=2` with a clear error), satisfying S8707.
- Compose: untracked `chmod 600 .env` via `env_file` (+ tracked `.env.example`);
  interpolation-vs-injection documented. Portainer: explicit `${VAR:-}` stack-variable
  mappings documented; `yt2wiki:0.1.0` honestly stated as **local-build-only, not a public image**.

### B5 — Sidecar: experimental, explicitly unqualified

`docker-compose.yml`/`docker-compose.portainer.yml` reference the correct upstream CUDA build
`ghcr.io/ggml-org/llama.cpp:server-cuda` (old `ggerganov` account deprecated) with **no pinned
digest** — `docker pull` was denied from this network so none was verified here, and the files
say so. Flags are transcribed from upstream docs, not validated. No weights were downloaded
(per scope), no 8-image ranking/CUDA-offload/VRAM numbers exist. The services work
independently without it; production vision comes from external OpenAI-compatible endpoints.

### B6 — CPU ASR qualified on real audio (host CUDA hidden + in-container)

Fixture `jfk.flac` (11 s speech, sha256 `63a4b1e4…9a4a`) → ffmpeg to 16 kHz mono PCM16.
`CUDA_VISIBLE_DEVICES=""` throughout; `import faster-whisper` leaves `torch` unimported
(container image has no torch at all).

| model | compute | cold load | transcribe 11 s | RTF | peak RSS | transcript |
|---|---|---|---|---|---|---|
| faster-whisper-small | `int8` (`cpu_compute_type`) | 1.2 s host / 11.7 s container (first download) | 2.0–2.3 s | ~5× | 0.8–1.2 GB | correct + word timestamps |
| faster-whisper-large-v3 (default) | `int8` (`cpu_compute_type`) | 4.8 s cached (68 s incl. first download) | 9.3 s | 1.18× | 4.3 GB | correct + word timestamps |

- The previous GPU default (`int8_float16`, `float16` for enhanced) fails hard on CPU
  (`ValueError: … do not support efficient int8_float16 computation` — reproduced before fixing).
- Fix is registry-driven (`cpu_compute_type: int8` on all ASR variants) with backend substitution
  recorded in the load report; GPU defaults untouched. Registry edit invalidates cached
  `transcribe` outputs once by design (documented).
- Consequence: new-video ASR **works** on the GPU-free service. No external ASR capability required.

### CodeRabbit findings — all addressed or explicitly deferred

19 inline threads, verified against the rebased tree:
- Upload idempotency heavy lift (winner inspection, stale repair, no orphans, no replace,
  no swallowed conflicts) → the full `_read/_canonical/_retire/_publish/_register/_repair/_stage`
  rewrite + 33 exact-canonical tests. Supersedes the outdated thread on the old implementation.
- CLI exit codes for assigned-role qualification → `_cmd_inference_test` requires `ok` plus each
  assigned role's probe; 8 deterministic tests (unreachable/401/failed generation/both roles/
  writer-only/vision-only/unassigned).
- `validate()` shape hardening (`InferenceConfigError`, never `AttributeError`/`TypeError`) +
  malformed-shape tests — matches the suggested fix.
- EXDEV-only fallback kept deliberately: extending to `EPERM`/`ENOTSUP` would need retry
  coordination for the EEXIST-while-copying edge (spurious 500s on legitimate duplicates);
  failure today is explicit with no partial/corrupt state, so unsupported filesystems are
  documented as restricted instead (docstring + `EACCES`/`EROFS`/`EPERM` propagation tests).
  Deferred with rationale, not silently dropped.
- Portainer env mappings added; mounted-secret wording removed (files unsupported).
- `0.0.0.0:8080` suggestion removed everywhere; only host-loopback upstream or isolated
  internal network remain.
- Sidecar weights staging via `docker run --rm -v yt2wiki-models:/models … cp` documented;
  sidecar stays experimental, digest explicitly unpinned.
- `sqlite3` added to the image (verified 3.40.1 + `.backup` in-container); single stop-first
  backup procedure with restore + `integrity_check`, tested end-to-end (see B3/B4).
- Host-backup claim corrected with host-native commands; separate DB/articles copies while
  writers run are no longer presented as consistent.
- 8-image request asserted structurally (exactly 8 `image_url` parts).
- `starlette>=0.44` dev floor (verified needed); S7761 fixed; stale `S9073` split; report
  CI-mapping and image-identity corrected here (single identity `eeede26b6a82` for all
  current measurements; earlier tag builds labelled superseded).
- Remaining ruff notes (`cli.py` F401/SIM108/I001, `app.py:995`, `inference.py` lazy imports,
  `asr.py` dead helpers) are pre-existing base lines outside this PR's hunks; `pyproject`
  `SIM105/SIM102` ignore likewise predates the rewrite (ruff not CI-gated).

## Completion status vs the task

PR #11 is merged (`facf3da`); PR #12 is rebased onto it with a clean P2-only diff (redundant
`sonar.projectVersion` hunk gone). PR #12 provides: genuinely idempotent upload registration
with DB-boundary tests and exact-canonical assertions; role-aware operator-console management
with the Docker settings UI honestly marked read-only; env_file/Portainer credential routing
proven against the running container with bearer capture; CPU ASR qualified with numbers; an
explicitly experimental sidecar; honest Portainer/image docs with a tested WAL-safe backup.
No merges performed. Blockers for #3 (Gemma) / #4 (Halogen) remain separate follow-ups.

## Evidence index

- PRs: #11 merged as `facf3da`; #12 `0bfc77e` (CI `39901180146` success,
  Sonar `projectStatus OK`, `new_coverage 95.2%` on verified `new_lines_to_cover 217`,
  zero open issues)
- Code: `services/__init__.py` upload block (no pragmas, EXDEV-only documented),
  `cli.py` `inference …` (role-aware exits), `web/app.py` peer guard + `remote_viewer`,
  `settings.html` notice, `settings.js` dataset guard, `web/inference.py` containment +
  shape validation, `backends/asr.py` CPU mapping, `config/models.yaml` `cpu_compute_type`
- Tests: 253 passed, 5 skipped; `test_upload_branch_coverage` 27, `test_inference_cli` 16
  (exit codes, shapes, containment, gateway, bearer), 8-image structural assert
- Artifacts: wheel `/tmp/p2b-dist3/*.whl`, image `yt2wiki:0.1.0` `eeede26b6a82`,
  fixture `/tmp/asr-qual/{jfk.flac,jfk_16k.wav,cpu_asr_qual.py,large-v3.log}`, mock `/tmp/q-demo-mock.log`
- Docs: `docs/deploy.md` (single stop-first backup + restore/verify, no `0.0.0.0` publish,
  isolated-network proxy), `.env.example`, both compose files yaml-validated

*Preserved: `~/.local/share/yt2wiki`, all three worktrees, stashes, `HF_HOME`, `UV_CACHE_DIR`,
`build/`, `uv.lock`. Demo container `yt2wiki-demo` + mock stopped after evidence capture.*
