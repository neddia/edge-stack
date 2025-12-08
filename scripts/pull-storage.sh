#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_STORAGE="${SRC_STORAGE:-$ROOT/../site-agent/storage}"
DEST="${DEST:-$ROOT/site-pack/storage}"

if [ ! -d "$SRC_STORAGE" ]; then
  echo "source storage not found at $SRC_STORAGE" >&2
  exit 1
fi

mkdir -p "$DEST"

echo "Copying storage from $SRC_STORAGE -> $DEST"
rsync -av \
  --exclude='__pycache__' \
  "$SRC_STORAGE"/ "$DEST"/

echo "Done. Remember to drop real site seeds (site-config, registries, profiles, miners.db) here before bringing up the stack."
