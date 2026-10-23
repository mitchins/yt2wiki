# Provenance — schema versions and generation history

## Version layers

Five things that look like "the version" are tracked separately:

| Layer | Where | Example |
|---|---|---|
| Database schema version | `meta.schema_version` (current: **4**) | `4` |
| Application/package version | `yt2wiki.__version__` + installed metadata | `0.1.0` |
| Build commit / image digest | `/app/BUILD`, OCI `revision` label, `YT2WIKI_IMAGE_DIGEST` | `sha-abc1234` |
| Generation recipe version | `generations.recipe_version`, `prompts_json` | V9 recipe `1` |
| Generated article version | `generations` rows (immutable, parent-linked) | `gen-…` → `gen-…` |
| Human-edited revision | `article_revisions` with `origin = 'user'` | version `3` |

## Generations

Every successful publication records one row in `generations` and snapshots
`article.md`, `article.html`, `article.standalone.html` and `assets/` under
`articles/<id>/generations/<gid>/`. The manifest pins:

- generation id, article, source, job, parent generation and operation
  (`initial` or `regenerate`)
- app version, git commit and (when declared) the OCI image digest
- recipe + recipe version, hashes of the exact writer/judge prompt templates
  and the selection policy
- the **job-captured** ASR/writer/vision provider identities (provider, URL,
  model; model revision is `unknown` — HTTP inference never reports one)
- settings fingerprint, profile, source content hash, selected frame ids and
  per-file SHA-256 artifact hashes with creation timestamps

Snapshots may carry env-var *names* but never keys, tokens or credential
values — anything outside the allowlist is dropped before persisting.

Regenerating never erases the parent: the previous generation's files stay
byte-identical under its own directory (verified by hash), the new
generation links to it via `parent_generation_id`, and the reader keeps
serving the current paths. Restoring an older generation copies its files
back over the current outputs and is itself recorded as a **user** revision,
not a new model generation. Refinement (`refine` operations, child
generations from edits) is not implemented.

## Schema evolution

Upgrades are automatic and additive: opening a v3 library creates the
`generations` table and the `articles.current_generation_id` pointer while
leaving every article, revision, source and asset untouched. Pre-v4
publications read as `legacy/unknown` provenance — history is never
fabricated. Migrations are idempotent and restart-safe; there is no
migration command to run.

A database stamped by a **newer** unsupported schema is refused outright
instead of being silently written — upgrade yt2wiki rather than opening a
newer library with an older build. Rollback across a schema version is not
supported: restore the pre-upgrade data-directory backup (see the backup
procedure in docs/deploy.md) if you must go back.
