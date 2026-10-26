# Releases — GHCR images, tags, platforms

The publisher ships as a prebuilt multi-arch image. Ordinary installs and
Portainer stacks never build locally:

```bash
docker compose pull
docker compose up -d
```

No migration command, custom build step or manual architecture selection is
needed — one manifest reference automatically resolves to the host platform.

## Tags

| Tag | Meaning |
|---|---|
| `ghcr.io/mitchins/yt2wiki:main` | Latest successfully qualified main build |
| `ghcr.io/mitchins/yt2wiki:sha-<commit>` | Immutable commit-specific build |
| `ghcr.io/mitchins/yt2wiki:vX.Y.Z` | Intentional version releases only |

`:latest` is never moved automatically. Pin production with a `sha-<commit>`
or `vX.Y.Z` tag via the `YT2WIKI_IMAGE_TAG` variable (no leading colon —
the `:` separator is already in the Compose reference):

```bash
YT2WIKI_IMAGE_TAG=sha-abc1234 docker compose pull
YT2WIKI_IMAGE_TAG=sha-abc1234 docker compose up -d
```

Immutable contract: a `sha-<commit>` tag is first-build-wins. CI refuses to
move an existing `sha-<commit>` tag (`scripts/ci/check-immutable.sh` fails
the manifest job loudly instead of silently overwriting), so a rebuild of
the same commit can never shift the reference out from under a pinned
deployment. Re-running a publish for the same commit requires deleting the
tag first — an explicit operator act. For strict deployments, pin the
manifest digest from the `release-report` artifact instead of any tag:
digests are content-addressed and cannot be moved at all. `:main` stays the
moving tag for the latest qualified main build.

### Version tags promote, never rebuild

A `v*` tag names a commit. When that commit is already qualified (its
`sha-<commit>` tag is published from the main build), the version-tag
workflow **promotes** the existing manifest instead of rebuilding:
`scripts/ci/promote-version.sh` re-labels `sha-<commit>` as `vX.Y.Z` after
verifying the source manifest carries both release platforms
(`linux/amd64` + `linux/arm64`). Promotion keeps the manifest digest — the
version tag resolves to exactly the image the release report recorded. A
version tag is never overwritten once published, and promoting never moves
`:main` (or any other tag). If the commit is *not* yet published (a release
cut directly from a commit without a main build), the workflow builds,
qualifies and publishes it exactly like a main push. `docker-build` skips
its per-platform rebuild when the commit's immutable tag already exists.

## Platforms

The manifest qualifies one image per platform; Compose pulls the matching one:

- `linux/amd64` — standard x86-64 servers
- `linux/arm64` — ARM64 servers and 64-bit Raspberry Pi

`linux/arm/v7` is **not** advertised: the pinned dependencies (`numpy`,
`pillow`, `psutil`, `pyyaml`, plus the `uvicorn[standard]` stack via
`uvloop`/`httptools`) publish no `armv7l` wheels for the required versions,
so the image cannot be built from the same dependency set within a bounded
CI budget. A platform joins the manifest only after it passes qualification.

ARMv7 stays an explicit manual qualification path: run the CI workflow with
`qualify_armv7` enabled (Actions → CI → Run workflow). That job builds and
smoke-tests `linux/arm/v7` only — nothing is pushed and nothing joins the
manifest. No QEMU build runs on routine main releases. If dependencies ever
grow `armv7l` wheels and the manual run passes, a follow-up PR wires the
platform into the release matrix.

## Publishing

`.github/workflows/ci.yml` (`docker-build`, `docker-manifest`, all actions
pinned to immutable SHAs) builds each platform natively
(`ubuntu-24.04-arm` for ARM64), pushes per-platform images by digest, then
assembles the manifest. Publishing runs only on `main` (or a `v*` tag)
after the test job passes **and** the Sonar quality gate succeeds; PRs build
and smoke-test `linux/amd64` without publishing. `GITHUB_TOKEN` uses the
job-scoped `packages: write` permission. The base image stays digest-pinned
(`python:3.12-slim-bookworm`, itself a multi-arch manifest).

## Verification evidence

Every publish uploads a `release-report` artifact with the commit, the
manifest digest, the qualified platform list and the anonymous-pull probe.
See "Package visibility" above for how the probe result gates the login-free
installation claim. The image records `GIT_COMMIT` in its OCI `revision`
label and `/app/BUILD`. The manifest digest
is recorded in each generation manifest (`app.image_digest`) only when the
operator declares `YT2WIKI_IMAGE_DIGEST` for a pinned deployment.

## Package visibility (one-time, operator)

The first successful main-branch publish creates the `yt2wiki` container
package under the repository. A fresh package may not be publicly pullable:
after the first publish, the operator opens the package page
(`github.com/mitchins/yt2wiki/pkgs/container/yt2wiki`), confirms or changes
visibility to public in the package settings, and records the outcome.
Package visibility is never changed by automation — this step is an explicit
operator act, and no login-free installation is claimed until it is done.

Until the package is public, pull authenticated (replace with a classic
token or `GITHUB_TOKEN` holding at least `read:packages`):

```bash
echo "$CR_PAT" | docker login ghcr.io -u <github-username> --password-stdin
docker compose pull
docker compose up -d
```

Every publish uploads a `release-report` artifact recording the commit, the
manifest digest, the qualified platform list and the anonymous-pull probe
(whether `ghcr.io` serves the tag without credentials). That probe result —
not an assumption — is the release-readiness evidence for login-free
installation: `anonymous_pull=yes` means `docker compose pull` works with no
login; `anonymous_pull=no` means the package is still private and the
authenticated flow above applies.

## Developer builds

Local development only — never for installs:

```bash
GIT_COMMIT=$(git rev-parse --short HEAD) \
  docker compose -f docker-compose.yml -f docker-compose.build.yml build
```

The local image is tagged `yt2wiki:local` and records the working-tree commit
in `/app/BUILD` (release builds record the exact commit instead).
