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
| `ghcr.io/mitchins/yt2wiki:sha-<commit>` | Immutable build for one commit (pinned deployments) |
| `ghcr.io/mitchins/yt2wiki:vX.Y.Z` | Intentional version releases only |

`:latest` is never moved automatically. Pin production with a `:sha-<commit>`
or `:vX.Y.Z` tag via the `YT2WIKI_IMAGE_TAG` variable:

```bash
YT2WIKI_IMAGE_TAG=:sha-abc1234 docker compose pull
YT2WIKI_IMAGE_TAG=:sha-abc1234 docker compose up -d
```

## Platforms

The manifest qualifies one image per platform; Compose pulls the matching one:

- `linux/amd64` — standard x86-64 servers
- `linux/arm64` — ARM64 servers and 64-bit Raspberry Pi

`linux/arm/v7` is **not** advertised: the pinned dependencies (`numpy`,
`pillow`, `psutil`, `pyyaml`, plus the `uvicorn[standard]` stack via
`uvloop`/`httptools`) publish no `armv7l` wheels for the required versions,
so the image cannot be built from the same dependency set within a bounded
CI budget. A platform joins the manifest only after it passes qualification.

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
manifest digest, the qualified platform list and an anonymous-pull probe
(whether `ghcr.io` serves the tag without credentials). The digest is also
baked into the image as an OCI `revision` label and recorded in each
generation manifest (`app.image_digest`) when the operator declares
`YT2WIKI_IMAGE_DIGEST` for a pinned deployment.

## Developer builds

Local development only — never for installs:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
```

The local image is tagged `yt2wiki:local` and records the working-tree commit
in `/app/BUILD` (release builds record the exact commit instead).
