#!/usr/bin/env bash
# Compute the GHCR tags for a qualified build.
# Usage: compute-tags.sh <github-ref> <github-sha>
# Prints one tag per line: always :sha-<short>; :main only for main pushes
# (a version tag must never move :main); :vX.Y.Z only for version tags.
set -euo pipefail

REF="${1:?usage: compute-tags.sh <ref> <sha>}"
SHA="${2:?usage: compute-tags.sh <ref> <sha>}"
SHORT="${SHA:0:7}"

echo "ghcr.io/mitchins/yt2wiki:sha-$SHORT"
if [[ "$REF" == "refs/heads/main" ]]; then
  echo "ghcr.io/mitchins/yt2wiki:main"
fi
if [[ "$REF" == refs/tags/v* ]]; then
  echo "ghcr.io/mitchins/yt2wiki:${REF#refs/tags/}"
fi
