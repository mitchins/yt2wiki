#!/usr/bin/env bash
# Refuse to move an existing immutable tag.
# Usage: check-immutable.sh <image-reference>
# Exits 0 when the tag does not resolve (safe to publish), 1 with an error
# when it already exists. The DOCKER variable overrides the docker binary
# (used by tests to stub the registry probe).
set -euo pipefail

TAG="${1:?usage: check-immutable.sh <image-reference>}"
DOCKER="${DOCKER:-docker}"

if "$DOCKER" buildx imagetools inspect "$TAG" >/dev/null 2>&1; then
  echo "::error::Refusing to move immutable tag $TAG: it is already published. First-build-wins — delete the tag explicitly to republish, or cut a new commit." >&2
  exit 1
fi
echo "immutable tag $TAG is free to publish"
