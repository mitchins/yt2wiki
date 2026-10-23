# P2 — Baseline, Upload Safety and Deployable Service — Final Report

**Date:** 2026-10-23
**Remote:** https://github.com/mitchins/yt2wiki
**Authoritative `origin/main`:** `edb56f9df053b306e130febdf58ea69eeeb79a9b` (PR #9 squash merge)
**Worktrees:** `main` `496aed8` (pre-GitHub, preserved), `.worktrees/p1` `6105ee5`,
`.worktrees/p2a` `p2a/sonar-baseline` `bcc404a`, `.worktrees/p2b` `p2b/deploy` `053b8aa`
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

**PR:** https://github.com/mitchins/yt2wiki/pull/12 `p2b/deploy` `053b8aa` → `main` (`edb56f9`)
**Chain:** `ea0473c` base → `f6d9db3` docs → `c0d5590` coverage refactor → `210646f` retrigger →
`d2b3c31` ruff → `d8fb776`/`97af25f` genuine idempotency → `371ae18` operator console →
`3204262` CPU ASR → `247ee0a` compose/secrets → `9aa90eb` docs → `1cf16b7` retrigger →
`f36034b` gate fixes → `ab691cb` taint scope → `7316ec8` containment → `053b8aa` S7761 →
`3b5d70a` report → `071dc31` CodeRabbit batch (sqlite3, backup, 8-image) → `a11954e` retrigger →
`a495089` S9073 split
**CI:** `39887119549` `4m15s` `success` (head `071dc31`; `39886533928` cancelled on apt-mirror
stall; earlier `39884390012` success on `7316ec8`, `39882861645` failed only on the then-open
S8707). Docs/test-only pushes re-run the full suite green.
**Sonar PR 12:** `projectStatus OK` — `new_security_rating 1`, `new_reliability_rating 1`,
`new_maintainability_rating 1`, `new_coverage 94.5` (≥80), `new_duplicated_lines_density 0.0`,
`new_security_hotspots_reviewed 100.0`. Open issues after the gate batch: **zero**
(the last, pre-existing `S9073 test_persistence.py:56`, split in `a495089`; prior S8707 /
complexity / S108 / S5713 / S1857-class findings all closed by the refactor + containment).
**CodeRabbit:** incremental `@coderabbitai review` + `full review` both rate-limited on the
final heads (`26 min` cooldown message); all earlier actionable findings addressed
(PR #11 timestamp; PR #12 S8707/complexity/blocks via the Sonar gate, which is now green).

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
- Tests (`tests/test_upload_branch_coverage.py` 22, `test_upload_concurrent.py` 3,
  `test_upload_isolation.py` 3): barrier-gated INSERT collisions (not just racing threads),
  identical bytes with different filenames → single canonical file, stale/missing/corrupt/NULL rows
  repaired, hostile foreign-owned destination refused and kept, link/EXDEV races both spellings,
  repair races (concurrent fix wins / row deleted fails), unrelated-IntegrityError propagation,
  hash-mismatch/failure recovery. **Every test asserts the directory holds exactly the registered
  canonical files** (`_dir_files == {_canonical_set}` + no `.tmp.*`), not merely absence of temps.
- Full suite `241 passed, 5 skipped, 1 deselected`; upload section 100% line+branch covered
  (remaining services misses are pre-existing job/listing code).

### B2 — Pip wheel (re-qualified on final code)

`/tmp/p2b-dist2/yt2wiki-0.1.0-py3-none-any.whl` (`225,730` bytes, `uv build`): clean
`uv venv --python 3.12` install, `pip list` shows `yt2wiki 0.1.0` with no `torch`/`transformers`,
`yt2wiki inference show` works (`{"providers": {}, "roles": {}, "status": {}}`).

### B3/B4 — Container, operator console, credential routing (demonstrated live)

Image `yt2wiki:0.1.0` `1998be43f853` (`1.16 GB`, pinned slim-bookworm base + `sqlite3` CLI,
unprivileged `yt2wiki`, no torch/transformers/weights). Container `yt2wiki-demo`,
host `18080→8000` (host `8080` occupied by open-webui; same loopback binding),
`--env-file` with `YT2WIKI_MOCK_KEY=test-secret-xyz` (demo volume `yt2wiki-demo-data`,
mock at `http://172.17.0.1:8931`, since torn down after evidence capture):

- `GET /settings` from the host (gateway peer) renders the **Operator only** notice with
  `data-remote` and the exact `docker exec` commands; mutation form disabled via JS.
- `POST /api/settings/inference` from the host → `403` with the operator-console message —
  also with `X-Forwarded-For: 127.0.0.1` (headers never grant access). An SSH tunnel does
  **not** make this pass; the docs said so before, and now the page, the 403 text and the
  tests all say so.
- `docker exec … curl … 127.0.0.1:8000/api/settings/inference` (localhost peer) → `200`.
- `yt2wiki inference save --file /data/config/inference-payload.json` + `test --name mock`
  against a mock at `http://172.17.0.1:8931/v1` → `ok: true`,
  `qualified_for_writer/qualified_for_vision: true`; the mock logged
  `Authorization: Bearer test-secret-xyz` on `/models` and `/chat/completions`;
  `/data/config/inference.json` contains only the variable name (`grep -c` secret → `0`).
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

### Remaining Sonar/CodeRabbit state

- Gate `OK` on head `7316ec8` (94.5% new coverage). Post-gate commit `053b8aa` is a one-line JS
  equivalent (`"remote" in root.dataset`, syntax-checked) addressing the last own finding S7761;
  CI re-ran on it. Only pre-existing `S9073 test_persistence.py:56` (untouched file) may remain open.
- `pyproject.toml` still ignores `SIM105`/`SIM102` from the earlier iteration — no longer exercised
  by the rewritten code; left untouched as pre-existing config (ruff is not CI-gated).
- `src/yt2wiki/backends/asr.py` has pre-existing findings (`F821 Sequence`, `UP035`, `SIM113`,
  `I001` on lines not touched here) — noted, not in scope.
- `src/yt2wiki/cli.py` F401/SIM108/I001 and `app.py:995` E741 are pre-existing base-code findings
  on lines outside this PR's hunks.

## Completion status vs the task

PR #11 is ready for operator merge (timestamp corrected, gate green, six conditions confirmed,
version matched). After it merges, PR #12 needs a rebase dropping the redundant
`sonar.projectVersion` hunk. PR #12 provides: genuinely idempotent upload registration with
DB-boundary tests and exact-canonical assertions; a working operator-console management path with
the Docker settings UI honestly marked read-only; env_file/Portainer credential routing proven
against the running container with bearer capture; CPU ASR qualified with numbers; an explicitly
experimental (not production-claimed) sidecar; honest Portainer/image docs with WAL-safe backup.
No merges performed. Blockers for #3 (Gemma) / #4 (Halogen) remain separate follow-ups.

## Evidence index

- PRs: #11 `bcc404a` (CI `39878142654` success), #12 `053b8aa` (CI `39884390012` success,
  Sonar `projectStatus OK` `new_coverage 94.5` `new_security_rating 1`)
- Code: `services/__init__.py` upload block (no pragmas), `cli.py` `inference …`,
  `web/app.py` `LOCAL_PEERS/peer_host/is_local_peer` + `remote_viewer`, `settings.html` notice,
  `settings.js` dataset guard, `web/inference.py` `read_payload_file` containment,
  `backends/asr.py` CPU compute mapping, `config/models.yaml` `cpu_compute_type`
- Tests: 241 passed; `test_upload_branch_coverage` 22 DB-boundary tests, `test_inference_cli` 7
  (incl. gateway 403/banner, bearer routing, containment, symlink escape)
- Artifacts: wheel `/tmp/p2b-dist2/*.whl`, image `yt2wiki:0.1.0` `655c3ecf8af1`,
  fixture `/tmp/asr-qual/{jfk.flac,jfk_16k.wav,cpu_asr_qual.py,large-v3.log}`, mock `/tmp/p2b-demo/`
- Docs: `docs/deploy.md` (§3 secrets+console, §4 experimental, §5 honest proxy/guard, §6 Portainer
  honesty+backup, §7 measured qualification), `.env.example`, both compose files validated by yaml parse

*Preserved: `~/.local/share/yt2wiki`, all three worktrees, stashes, `HF_HOME`, `UV_CACHE_DIR`,
`build/`, `uv.lock`. Demo container `yt2wiki-demo` + mock stopped after evidence capture.*
