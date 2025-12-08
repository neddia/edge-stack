#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${SRC:-$ROOT/../site-agent}"
OUT="${OUT:-$ROOT/artifacts/site-agent.tar.gz}"

if [ ! -d "$SRC" ]; then
  echo "site-agent source not found at $SRC" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

echo "Packaging site-agent from $SRC -> $OUT"
tar \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='node_modules' \
  -czf "$OUT" -C "$SRC" .

echo "Done. Copy $OUT to the IPC and unpack into /opt/site-agent (or wherever you run it)."
