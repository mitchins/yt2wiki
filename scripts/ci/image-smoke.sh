#!/usr/bin/env bash
# Image smoke qualification: start the publisher image for one platform and
# verify it behaves as a thin library server.
#
# Usage: image-smoke.sh <image> <platform>
# Exits non-zero on any failed check.
set -euo pipefail

IMAGE="$1"
PLATFORM="${2:-linux/amd64}"
NAME="yt2wiki-smoke-$$"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "--- pull/resolve $IMAGE for $PLATFORM"
if [[ "$IMAGE" == *"/"* ]]; then
  docker pull --platform "$PLATFORM" "$IMAGE" >/dev/null
else
  docker image inspect "$IMAGE" >/dev/null
fi

echo "--- start container (no GPUs, no model weights, no LLM)"
docker run -d --rm --name "$NAME" --platform "$PLATFORM" \
  -p 127.0.0.1:18080:8000 \
  -e YT2WIKI_HOME=/data \
  "$IMAGE" >/dev/null

echo "--- wait for /health"
for i in $(seq 1 30); do
  if curl -sf --max-time 3 http://127.0.0.1:18080/health >/dev/null 2>&1; then
    break
  fi
  [ "$i" = 30 ] && { echo "health check failed"; docker logs "$NAME" 2>&1 | tail -20; exit 1; }
  sleep 2
done
curl -sf http://127.0.0.1:18080/health
echo

echo "--- platform + application import"
docker exec "$NAME" python -c "import platform; print('machine:', platform.machine())"
docker exec "$NAME" python -c "import yt2wiki, yt2wiki.web.app; print('app:', yt2wiki.__version__)"
docker exec "$NAME" python -c "from yt2wiki.generations import build_info; print(build_info())"

echo "--- no ASR/LLM runtimes in the default image"
for mod in torch transformers faster_whisper ctranslate2 sentencepiece bitsandbytes; do
  if docker exec "$NAME" python -c "import $mod" 2>/dev/null; then
    echo "FORBIDDEN module present: $mod"
    exit 1
  fi
done
echo "runtime modules absent: ok"

echo "--- non-root execution and /data persistence"
docker exec "$NAME" sh -c 'test "$(id -u)" = "1000" && touch /data/.smoke && test -f /data/.smoke && echo "uid1000 + writable /data: ok"'

echo "--- library endpoints"
curl -sf --max-time 10 http://127.0.0.1:18080/library >/dev/null
curl -sf --max-time 10 http://127.0.0.1:18080/ >/dev/null
echo "library: ok"

echo "SMOKE PASS: $IMAGE @ $PLATFORM"
