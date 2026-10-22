# yt2wiki runtime image.
#
# The image deliberately reuses a base CUDA + PyTorch image so that the torch,
# transformers and bitsandbytes wheels match the CUDA runtime.  It does NOT
# bundle model weights: those live in the persistent `models` volume and are
# downloaded on first use (the container has network access for that).
ARG CUDA_VERSION=12.6.3
ARG UBUNTU_VERSION=24.04
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_NO_ADVISORY_WARNINGS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ffmpeg git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

# CPU-only torch plus the NVIDIA runtime libraries for the GPU wheels.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch==2.11.0" \
    && /opt/venv/bin/pip install --no-cache-dir \
        "transformers>=4.57" "accelerate>=1.0" "sentencepiece" "safetensors>=0.4" \
        "bitsandbytes>=0.45" \
        "faster-whisper>=1.1" "av>=14" \
        "nvidia-cublas-cu12" "nvidia-cudnn-cu12" \
    && /opt/venv/bin/pip install --no-cache-dir -e . \
    && /opt/venv/bin/pip cache purge

# GPU wheels need the CUDA 12 shared libraries that CTranslate2 dlopen()s at
# runtime; the runtime base image supplies the driver side.
ENV PATH="/opt/venv/bin:${PATH}" \
    YT2WIKI_HOME=/data \
    HF_HOME=/data/models

COPY docker/entrypoint.sh /usr/local/bin/yt2wiki-entrypoint
RUN chmod +x /usr/local/bin/yt2wiki-entrypoint

EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/yt2wiki-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
