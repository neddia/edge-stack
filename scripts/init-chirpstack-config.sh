#!/usr/bin/env bash
# Generate chirpstack.toml from chirpstack.toml.template if missing, with a fresh secret.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG_DIR="${CFG_DIR:-$ROOT/edge-stacks/lorawan/configuration/chirpstack}"
TEMPLATE="${TEMPLATE:-$CFG_DIR/chirpstack.toml.template}"
TARGET="${TARGET:-$CFG_DIR/chirpstack.toml}"

if [ -f "$TARGET" ]; then
  echo "chirpstack.toml already exists at $TARGET; leaving untouched"
  exit 0
fi

if [ ! -f "$TEMPLATE" ]; then
  echo "chirpstack.toml.template not found at $TEMPLATE" >&2
  exit 1
fi

SECRET="${CHIRPSTACK_SECRET:-$(openssl rand -base64 32)}"
tmp="$(mktemp)"
sed "s|secret=\"you-must-replace-this\"|secret=\"$SECRET\"|" "$TEMPLATE" > "$tmp"
mv "$tmp" "$TARGET"
chmod 600 "$TARGET"
echo "Generated $TARGET with a new secret."
