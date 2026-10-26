#!/usr/bin/env bash
# Promote an already-qualified commit image to a version tag (no rebuild).
# Usage: promote-version.sh <source-ref> <version-ref> [report-file]
#
# A version tag must reference exactly the qualified image of the commit it
# points at. When that commit is already published (its immutable
# sha-<commit> tag exists), re-building would produce new, unverifiable
# digests — so the existing qualified manifest is re-labelled instead:
#   - the version tag must not exist yet (never overwritten);
#   - the source must be a published multi-platform manifest (amd64+arm64);
#   - imagetools create re-labels the manifest, keeping its digest.
set -euo pipefail

SRC="${1:?usage: promote-version.sh <source-ref> <version-ref> [report-file]}"
DEST="${2:?usage: promote-version.sh <source-ref> <version-ref> [report-file]}"
REPORT="${3:-}"
DOCKER="${DOCKER:-docker}"

# Never overwrite an existing tag of any kind.
if "$DOCKER" buildx imagetools inspect "$DEST" >/dev/null 2>&1; then
  echo "::error::Refusing to overwrite existing tag $DEST: version tags are immutable once published." >&2
  exit 1
fi

# The source manifest must exist: the commit has a qualified image.
if ! "$DOCKER" buildx imagetools inspect "$SRC" >/dev/null 2>&1; then
  echo "::error::$SRC is not published; this commit has no qualified image to promote." >&2
  exit 1
fi

# The promoted image must stay multi-platform: both release platforms must
# already be qualified members of the source manifest.
INSPECT="$(mktemp)"
trap 'rm -f "$INSPECT"' EXIT
"$DOCKER" buildx imagetools inspect "$SRC" > "$INSPECT"
grep -q 'linux/amd64' "$INSPECT" || { echo "::error::$SRC lacks a linux/amd64 member; not a qualified release manifest." >&2; exit 1; }
grep -q 'linux/arm64' "$INSPECT" || { echo "::error::$SRC lacks a linux/arm64 member; not a qualified release manifest." >&2; exit 1; }

# Re-label the existing manifest: same content, same digest, new tag.
"$DOCKER" buildx imagetools create -t "$DEST" "$SRC"
"$DOCKER" buildx imagetools inspect "$DEST" > "${REPORT:-/dev/null}"
echo "promoted $SRC -> $DEST (same manifest digest)"
