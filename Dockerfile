# yt2wiki thin service image — no Torch, no weights, unprivileged.
# Pinned base for reproducibility; update digest via `docker pull` + `docker inspect`.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# Build commit baked in by CI (--build-arg GIT_COMMIT=<sha>); local builds
# record the working-tree commit via `git rev-parse` in docker-compose.build.
ARG GIT_COMMIT=unknown
# Deno JS runtime (pinned): yt-dlp's modern YouTube extraction requires a JS
# runtime. Official upstream release zips, resolved per target architecture.
ARG DENO_VERSION=2.5.4

LABEL org.opencontainers.image.title="yt2wiki" \
      org.opencontainers.image.description="Turn technical videos into illustrated, searchable guides" \
      org.opencontainers.image.source="https://github.com/mitchins/yt2wiki" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.version="${GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YT2WIKI_HOME=/data \
    HF_HOME=/data/models \
    UV_CACHE_DIR=/tmp/uv-cache \
    TMPDIR=/tmp

# System deps: ffmpeg for media, tesseract for OCR veto, curl for healthcheck,
# sqlite3 CLI for the documented WAL-safe `.backup` procedure.
# Deno provides the JS runtime yt-dlp needs for modern YouTube extraction
# (unzip fetches the pinned upstream release zip per target architecture).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng \
        curl \
        ca-certificates \
        sqlite3 \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 yt2wiki \
    && case "$(dpkg --print-architecture)" in \
         amd64) DENO_ARCH=x86_64-unknown-linux-gnu ;; \
         arm64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
         *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://dl.deno.land/release/v${DENO_VERSION}/deno-${DENO_ARCH}.zip" \
         -o /tmp/deno.zip \
    && unzip -p /tmp/deno.zip deno > /usr/local/bin/deno \
    && chmod 755 /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

WORKDIR /app

# Copy dependency metadata first for layer caching.
COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src

# Install the project (no GPU extras) — wheel includes templates/static via package-data.
RUN pip install --upgrade pip \
    && pip install --no-cache-dir . \
    && rm -rf /root/.cache

# Data directory (SQLite, uploads, frames, articles, assets, revisions, config)
RUN mkdir -p /data /data/config /data/models && chown -R yt2wiki:yt2wiki /data /app \
    # Record the exact build commit inside the image: the .git directory is
    # excluded from the build context, so `git rev-parse` cannot run here.
    && echo "${GIT_COMMIT}" > /app/BUILD && chown yt2wiki:yt2wiki /app/BUILD

USER yt2wiki
EXPOSE 8000

# Persist everything a library needs: bind-mount /data from the host.
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/health || exit 1

# Inside the container listen on all interfaces so Docker can forward host 127.0.0.1:8080 -> container 8000.
# Host-side binding to 127.0.0.1 (see compose `ports: ["127.0.0.1:8080:8000"]`) is the security boundary,
# not the container's loopback. For LAN behind an authenticated proxy, keep container on 0.0.0.0 and put
# the proxy in front; do not expose the admin UI directly on an unrestricted network.
CMD ["yt2wiki", "serve", "--host", "0.0.0.0", "--port", "8000"]
