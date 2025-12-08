#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACKS="${STACKS:-$ROOT/edge-stacks}"
SITEPACK="${SITEPACK:-$ROOT/site-pack}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; install Docker/Compose first" >&2
  exit 1
fi

if [ ! -d "$SITEPACK" ]; then
  echo "site-pack missing at $SITEPACK" >&2
  exit 1
fi
if [ ! -f "$SITEPACK/.env" ]; then
  echo "warning: $SITEPACK/.env not found; compose will fall back to defaults or fail for missing vars" >&2
fi

echo "Ensuring shared network..."
docker network inspect edge_core >/dev/null 2>&1 || docker network create edge_core

if ! command -v masscan >/dev/null 2>&1; then
  echo "Installing masscan (required for miner discovery)..."
  sudo apt-get update && sudo apt-get install -y masscan
fi
if command -v masscan >/dev/null 2>&1; then
  if ! sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/masscan 2>/dev/null; then
    echo "warning: failed to set capabilities on /usr/bin/masscan; you may need to run as root" >&2
  fi
fi

if command -v setup-lora-gateway.sh >/dev/null 2>&1; then
  echo "Setting up LoRa gateway (Heltec / sx1302_hal)..."
  if ! sudo setup-lora-gateway.sh; then
    echo "warning: setup-lora-gateway.sh failed; check Heltec USB or sx1302_hal build" >&2
  fi
else
  echo "Skipping LoRa gateway setup (setup-lora-gateway.sh not installed)"
fi

if [ -f "$ROOT/site-pack/lorawan.env" ]; then
  echo "Staging ChirpStack env from site-pack/lorawan.env"
  cp "$ROOT/site-pack/lorawan.env" "$STACKS/lorawan/.env"
fi

if [ ! -f "$STACKS/lorawan/docker-compose.yml" ]; then
  echo "ChirpStack compose not found; cloning upstream repo..."
  mkdir -p "$STACKS/lorawan"
  tmpdir="$(mktemp -d)"
  if git clone --depth 1 https://github.com/chirpstack/chirpstack-docker.git "$tmpdir"; then
    # Preserve any locally added files (e.g., custom region configs) on first sync.
    rsync -a --ignore-existing "$tmpdir"/ "$STACKS/lorawan"/
    rm -rf "$tmpdir"
  else
    echo "warning: failed to clone chirpstack-docker; Lorawan stack will be skipped" >&2
  fi
fi

echo "Starting data stack (Influx + Grafana)..."
( cd "$STACKS/data" && docker compose up -d )

if [ -f "$STACKS/lorawan/docker-compose.yml" ]; then
  echo "Starting ChirpStack stack..."
  if [ -x "$ROOT/scripts/init-chirpstack-config.sh" ]; then
    "$ROOT/scripts/init-chirpstack-config.sh" || echo "warning: chirpstack config init failed" >&2
  fi
  ( cd "$STACKS/lorawan" && docker compose up -d )
  if [ -f "$STACKS/lorawan/docker-compose.init.yml" ]; then
    echo "Starting ChirpStack init overlay..."
    ( cd "$STACKS/lorawan" && docker compose -f docker-compose.yml -f docker-compose.init.yml up -d cs-init )
  fi
  if [ -x "$ROOT/scripts/cs-seed.py" ] && [ -f "$ROOT/site-pack/storage/chirpstack-devices.yml" ]; then
    echo "Seeding ChirpStack (gateway/app/devices)..."
    ( cd "$ROOT" && CHIRPSTACK_API_KEY_FILE="$ROOT/site-pack/.secrets/chirpstack.api_key" \
      CHIRPSTACK_TENANT_ID="${CHIRPSTACK_TENANT_ID:-}" \
      CHIRPSTACK_DEFAULT_APP_KEY="${CHIRPSTACK_DEFAULT_APP_KEY:-}" \
      CHIRPSTACK_API_URL="${CHIRPSTACK_API_URL:-http://localhost:8080}" \
      "$ROOT/scripts/cs-seed.py" --devices-file "$ROOT/site-pack/storage/chirpstack-devices.yml" \
      --gateway-eui-file "$ROOT/site-pack/.secrets/gateway_eui" ) || echo "warning: ChirpStack seeding failed" >&2
  else
    echo "Skipping ChirpStack seeding (script or devices file missing)"
  fi
else
  echo "Skipping ChirpStack (no docker-compose.yml in $STACKS/lorawan)"
fi

if [ -f "$STACKS/app/docker-compose.yml" ]; then
  echo "Starting site-agent stack..."
  ( cd "$STACKS/app" && docker compose up -d )
else
  echo "Skipping site-agent (no docker-compose.yml in $STACKS/app)"
fi

echo "Done. Secrets and tokens live in site-pack/.secrets (bind-mounted into containers)."
