#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACKS="${STACKS:-$ROOT/edge-stacks}"
SITEPACK="${SITEPACK:-$ROOT/site-pack}"
SECRETS_DIR="${SECRETS_DIR:-$SITEPACK/.secrets}"
CHIRPSTACK_API_KEY_FILE_PATH="${CHIRPSTACK_API_KEY_FILE_PATH:-$SECRETS_DIR/chirpstack.api_key}"
GATEWAY_EUI_FILE_PATH="${GATEWAY_EUI_FILE_PATH:-$SECRETS_DIR/gateway_eui}"
LORAWAN_DIR="${LORAWAN_DIR:-$STACKS/lorawan}"
SITE_AGENT_TAR="${SITE_AGENT_TAR:-$ROOT/images/site-agent.tar}"

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

SCAN_USER="${SCAN_USER:-$(id -un)}"
SCAN_SCRIPT_SRC="$ROOT/scripts/setpoint-scan-miners"
SCAN_SERVICE_SRC="$ROOT/scripts/setpoint-scan-miners.service"
SCAN_TIMER_SRC="$ROOT/scripts/setpoint-scan-miners.timer"

if command -v systemctl >/dev/null 2>&1; then
  if [ -f "$SCAN_SCRIPT_SRC" ] && [ -f "$SCAN_SERVICE_SRC" ] && [ -f "$SCAN_TIMER_SRC" ]; then
    echo "Installing setpoint miner scan service..."
    if ! python3 - <<'PY' >/dev/null 2>&1
import yaml  # noqa: F401
PY
    then
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3-yaml
      else
        echo "warning: python3-yaml missing and apt-get unavailable; scan service may fail" >&2
      fi
    fi

    sudo install -m 0755 "$SCAN_SCRIPT_SRC" /usr/local/bin/setpoint-scan-miners
    tmp_unit="$(mktemp)"
    sed -e "s|__SCAN_USER__|$SCAN_USER|g" -e "s|__SITEPACK__|$SITEPACK|g" \
      "$SCAN_SERVICE_SRC" > "$tmp_unit"
    sudo install -m 0644 "$tmp_unit" /etc/systemd/system/setpoint-scan-miners.service
    rm -f "$tmp_unit"
    sudo install -m 0644 "$SCAN_TIMER_SRC" /etc/systemd/system/setpoint-scan-miners.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now setpoint-scan-miners.timer
  else
    echo "Skipping miner scan service install (script or unit files missing)"
  fi
fi

LORA_SETUP="$ROOT/scripts/setup-lora-gateway.sh"
if [ -x "$LORA_SETUP" ]; then
  echo "Setting up LoRa gateway (Heltec / sx1302_hal)..."
  if ! sudo "$LORA_SETUP"; then
    echo "warning: setup-lora-gateway.sh failed; check Heltec USB or sx1302_hal build" >&2
  fi
else
  echo "Skipping LoRa gateway setup ($LORA_SETUP not found/executable)"
fi

if [ -f "$SITEPACK/lorawan.env" ]; then
  echo "Staging ChirpStack env from site-pack/lorawan.env"
  mkdir -p "$STACKS/lorawan"
  cp "$SITEPACK/lorawan.env" "$STACKS/lorawan/.env"
fi

if [ ! -f "$LORAWAN_DIR/docker-compose.yml" ]; then
  echo "ChirpStack compose not found; cloning upstream repo..."
  mkdir -p "$LORAWAN_DIR"
  tmpdir="$(mktemp -d)"
  if git clone --depth 1 https://github.com/chirpstack/chirpstack-docker.git "$tmpdir"; then
    # Preserve any locally added files (e.g., custom region configs) on first sync.
    rsync -a --ignore-existing "$tmpdir"/ "$LORAWAN_DIR"/
    rm -rf "$tmpdir"
  else
    echo "warning: failed to clone chirpstack-docker; Lorawan stack will be skipped" >&2
  fi
fi

MQTT_TOPIC_PREFIX="${MQTT_TOPIC_PREFIX:-us915_1}"
LORAWAN_COMPOSE="$LORAWAN_DIR/docker-compose.yml"
if [ -f "$LORAWAN_COMPOSE" ]; then
  python3 - "$LORAWAN_COMPOSE" "$MQTT_TOPIC_PREFIX" <<'PY'
import re, sys
from pathlib import Path

path = Path(sys.argv[1])
prefix = sys.argv[2]
s = path.read_text(encoding="utf-8", errors="replace")
orig = s
for k in (
    "INTEGRATION__MQTT__EVENT_TOPIC_TEMPLATE",
    "INTEGRATION__MQTT__STATE_TOPIC_TEMPLATE",
    "INTEGRATION__MQTT__COMMAND_TOPIC_TEMPLATE",
):
    s = re.sub(
        rf"(^\\s*-\\s*{re.escape(k)}=)[^\\s]*/gateway/",
        rf"\\1{prefix}/gateway/",
        s,
        flags=re.MULTILINE,
    )

if s != orig:
    path.write_text(s, encoding="utf-8")
PY
fi

echo "Starting data stack (Influx + Grafana)..."
( cd "$STACKS/data" && docker compose up -d )

if [ -f "$LORAWAN_DIR/docker-compose.yml" ]; then
  echo "Starting ChirpStack stack..."
  if [ -x "$ROOT/scripts/init-chirpstack-config.sh" ]; then
    "$ROOT/scripts/init-chirpstack-config.sh" || echo "warning: chirpstack config init failed" >&2
  fi
  ( cd "$LORAWAN_DIR" && docker compose up -d )
  if [ -x "$ROOT/scripts/cs-seed.py" ] && [ -f "$SITEPACK/storage/chirpstack-devices.yml" ]; then
    echo "Seeding ChirpStack (gateway/app/devices)..."
    if ! python3 -c 'import httpx' >/dev/null 2>&1; then
      echo "Installing python3-httpx (required for cs-seed.py)..."
      sudo apt-get update && sudo apt-get install -y python3-httpx
    fi
    if [ -f "$SITEPACK/lorawan.env" ]; then
      set -a
      # shellcheck disable=SC1090
      . "$SITEPACK/lorawan.env"
      set +a
    fi
    if [ ! -s "$CHIRPSTACK_API_KEY_FILE_PATH" ]; then
      echo "Creating /site-pack/.secrets/chirpstack.api_key via ChirpStack CLI (if available)..."
      CS_SVC="${CHIRPSTACK_SERVICE:-chirpstack}"
      if [ "${QUIET_CS_CLI:-0}" = "1" ]; then
        out="$(
          cd "$LORAWAN_DIR" && docker compose exec -T "$CS_SVC" sh -lc '
            set -u
            BIN="$(command -v chirpstack 2>/dev/null || true)"
            if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
              for cand in /opt/chirpstack/chirpstack /usr/bin/chirpstack /usr/local/bin/chirpstack; do
                if [ -x "$cand" ]; then BIN="$cand"; break; fi
              done
            fi
            [ -n "$BIN" ] && [ -x "$BIN" ] || exit 1

            CFG_DIR=
            if [ -f /etc/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack"
            elif [ -d /etc/chirpstack ] && [ -f /etc/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack"
            elif [ -d /etc/chirpstack/chirpstack ] && [ -f /etc/chirpstack/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack/chirpstack"
            fi
            [ -n "$CFG_DIR" ] || exit 1
            CFG_OPT="--config $CFG_DIR"
            for cmd in \
              "$BIN $CFG_OPT create-api-key --name site-agent" \
              "$BIN $CFG_OPT api-key create --name site-agent" \
              "$BIN create-api-key --name site-agent" \
              "$BIN api-key create --name site-agent"; do
              if out="$($cmd 2>&1)" && [ -n "$out" ]; then
                printf "%s\n" "$out"
                exit 0
              fi
            done
            exit 1
          ' 2>/dev/null
        )" || out=""
      else
        out="$(
          cd "$LORAWAN_DIR" && docker compose exec -T "$CS_SVC" sh -lc '
            set -u
            BIN="$(command -v chirpstack 2>/dev/null || true)"
            if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
              for cand in /opt/chirpstack/chirpstack /usr/bin/chirpstack /usr/local/bin/chirpstack; do
                if [ -x "$cand" ]; then BIN="$cand"; break; fi
              done
            fi
            if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
              echo "chirpstack CLI not found" >&2
              ls -la /opt/chirpstack 2>/dev/null || true
              exit 1
            fi

            CFG_DIR=
            if [ -f /etc/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack"
            elif [ -d /etc/chirpstack ] && [ -f /etc/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack"
            elif [ -d /etc/chirpstack/chirpstack ] && [ -f /etc/chirpstack/chirpstack/chirpstack.toml ]; then
              CFG_DIR="/etc/chirpstack/chirpstack"
            fi
            if [ -z "$CFG_DIR" ]; then
              echo "chirpstack config directory not found under /etc/chirpstack" >&2
              ls -la /etc/chirpstack 2>/dev/null || true
              exit 1
            fi
            CFG_OPT="--config $CFG_DIR"
            for cmd in \
              "$BIN $CFG_OPT create-api-key --name site-agent" \
              "$BIN $CFG_OPT api-key create --name site-agent" \
              "$BIN create-api-key --name site-agent" \
              "$BIN api-key create --name site-agent"; do
              if out="$($cmd 2>&1)" && [ -n "$out" ]; then
                printf "%s\n" "$out"
                exit 0
              fi
            done
            echo "chirpstack CLI failed to create API key" >&2
            exit 1
          '
        )" || out=""
      fi

      if [ -n "$out" ]; then
        token="$(
          printf "%s\n" "$out" | python3 -c '
import re, sys

s = sys.stdin.read()
s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)
m = re.search(r"\btoken:\s*\"?([A-Za-z0-9._-]+)\"?", s)
print(m.group(1) if m else "", end="")
'
        )"
        if [ -n "$token" ]; then
          mkdir -p "$SECRETS_DIR" || true
          if [ ! -w "$SECRETS_DIR" ]; then
            sudo chown -R "$(id -un):$(id -gn)" "$SECRETS_DIR" 2>/dev/null || true
          fi
          if ( umask 077; printf "%s\n" "$token" > "$CHIRPSTACK_API_KEY_FILE_PATH" ); then
            chmod 600 "$CHIRPSTACK_API_KEY_FILE_PATH" 2>/dev/null || true
            echo "Wrote $CHIRPSTACK_API_KEY_FILE_PATH"
          else
            echo "warning: failed to write $CHIRPSTACK_API_KEY_FILE_PATH (permissions?)" >&2
          fi
        else
          echo "warning: failed to parse token from ChirpStack CLI output:" >&2
          printf "%s\n" "$out" >&2
        fi
      else
        echo "warning: failed to create ChirpStack API key via CLI; set $CHIRPSTACK_API_KEY_FILE_PATH manually" >&2
      fi
    fi
    if [ ! -s "$CHIRPSTACK_API_KEY_FILE_PATH" ]; then
      echo "warning: ChirpStack API key still missing; skipping cs-seed.py" >&2
    else
    ( cd "$ROOT" && CHIRPSTACK_API_KEY_FILE="$CHIRPSTACK_API_KEY_FILE_PATH" \
      CHIRPSTACK_TENANT_ID="${CHIRPSTACK_TENANT_ID:-}" \
      CHIRPSTACK_DEFAULT_APP_KEY="${CHIRPSTACK_DEFAULT_APP_KEY:-}" \
      CHIRPSTACK_API_URL="${CHIRPSTACK_API_URL:-http://localhost:8090}" \
      "$ROOT/scripts/cs-seed.py" --devices-file "$SITEPACK/storage/chirpstack-devices.yml" \
      --gateway-eui-file "$GATEWAY_EUI_FILE_PATH" ) || echo "warning: ChirpStack seeding failed" >&2
    fi
  else
    echo "Skipping ChirpStack seeding (script or devices file missing)"
  fi
else
  echo "Skipping ChirpStack (no docker-compose.yml in $STACKS/lorawan)"
fi

if [ -f "$STACKS/app/docker-compose.yml" ]; then
  echo "Starting site-agent stack..."
  SITE_AGENT_IMAGE="${SITE_AGENT_IMAGE:-ghcr.io/neddia/site-agent:latest}"
  if ! docker image inspect "$SITE_AGENT_IMAGE" >/dev/null 2>&1; then
    if [ -f "$SITE_AGENT_TAR" ]; then
      echo "Loading site-agent image from $SITE_AGENT_TAR"
      load_out="$(docker load -i "$SITE_AGENT_TAR" 2>&1)" || {
        echo "warning: docker load failed" >&2
        printf "%s\n" "$load_out" >&2
        load_out=""
      }
      [ -n "$load_out" ] && printf "%s\n" "$load_out"
      loaded_ref="$(printf "%s\n" "$load_out" | awk -F': ' '/Loaded image:/ {print $2}' | tail -n1)"
      loaded_id="$(printf "%s\n" "$load_out" | awk -F': ' '/Loaded image ID:/ {print $2}' | tail -n1)"
      if [ -n "$loaded_ref" ] && [ "$loaded_ref" != "$SITE_AGENT_IMAGE" ]; then
        docker tag "$loaded_ref" "$SITE_AGENT_IMAGE" || true
      elif [ -z "$loaded_ref" ] && [ -n "$loaded_id" ]; then
        docker tag "$loaded_id" "$SITE_AGENT_IMAGE" || true
      fi
    else
      echo "warning: $SITE_AGENT_TAR not found; cannot start site-agent without pulling" >&2
    fi
  fi
  if docker image inspect "$SITE_AGENT_IMAGE" >/dev/null 2>&1; then
    ( cd "$STACKS/app" && docker compose up -d --pull never )
  else
    echo "Skipping site-agent (image not available locally)" >&2
  fi
else
  echo "Skipping site-agent (no docker-compose.yml in $STACKS/app)"
fi

echo "Done. Secrets and tokens live in site-pack/.secrets (bind-mounted into containers)."
