#!/usr/bin/env bash
set -euo pipefail

# Generate a site-pack skeleton with random secrets and templated env files.
# Usage: gen-site-pack.sh <site-id> <output-dir>

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <site-id> <output-dir>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SEEDS_ROOT="$ROOT/.."
if [ -d "$SEEDS_ROOT/sites" ] && [ -d "$SEEDS_ROOT/defaults" ]; then
  : # setpoint-seeds layout
elif [ -d "$ROOT/../setpoint-seeds/sites" ]; then
  SEEDS_ROOT="$ROOT/../setpoint-seeds"
fi
DEFAULTS_DIR="${DEFAULTS_DIR:-$SEEDS_ROOT/defaults}"
# Prefer a defaults/.env.smtp next to the seeds vault; allow override.
PRIMARY_SMTP="${DEFAULTS_SMTP:-$DEFAULTS_DIR/.env.smtp}"
ALT_SMTP="$ROOT/defaults/.env.smtp"

SITE_ID="$1"
DEST_RAW="$2"
mkdir -p "$DEST_RAW"
DEST="$(realpath "$DEST_RAW")"

mkdir -p "$DEST"/.secrets "$DEST"/storage

DEFAULT_SITE_CONFIG="${DEFAULT_SITE_CONFIG:-$DEFAULTS_DIR/site-config.dev.yml}"
if [ -f "$DEFAULT_SITE_CONFIG" ] && [ ! -f "$DEST/storage/site-config.yml" ]; then
  cp "$DEFAULT_SITE_CONFIG" "$DEST/storage/site-config.yml"
fi

rand_hex() { openssl rand -hex "$1"; }
rand_b32() { openssl rand -base64 "$1" | tr -d '\n'; }

# Influx admin
INFLUX_USER="influx-admin-${SITE_ID}"
INFLUX_PASS="$(rand_b32 16)"
INFLUX_TOKEN="$(rand_hex 32)"

# Grafana admin
GRAFANA_USER="grafana-admin-${SITE_ID}"
GRAFANA_PASS="$(rand_b32 16)"

# Default app key for devices (can be overridden per-device)
DEFAULT_APP_KEY="$(rand_hex 16)$(rand_hex 16)"

# ChirpStack admin (standardized per-site); you still need to create this user/tenant in CS.
CS_ADMIN_EMAIL="cs-admin-${SITE_ID}@edge.local"
CS_ADMIN_PASSWORD="$(rand_b32 18)"

cat > "$DEST/.env" <<EOF
LOG_LEVEL=INFO
SITE_CONFIG_PATH=/app/storage/site-config.yml
PROFILES_DIR=/app/profiles
INFLUX_URL=http://influxdb:8086
INFLUX_ORG=edge-org
INFLUX_BUCKET=edge-site
INFLUX_RETENTION=30d
INFLUX_TOKEN_FILE=/secrets/influx.token
CS_GRPC_SERVER=chirpstack:8080
CS_GRPC_TOKEN=
CS_FPORT=1
LORA_CHUNK_CAP=220
MQTT_HOST=chirpstack-mqtt:1883
MQTT_USERNAME=
MQTT_PASSWORD=
MQTT_TOPICS=["application/+/device/+/event/up"]
EOF

cat > "$DEST/.secrets/influx.admin.user" <<EOF
$INFLUX_USER
EOF
cat > "$DEST/.secrets/influx.admin.pass" <<EOF
$INFLUX_PASS
EOF
cat > "$DEST/.secrets/influx.admin.token" <<EOF
$INFLUX_TOKEN
EOF

cat > "$DEST/.secrets/.env.grafana" <<EOF
GF_SECURITY_ADMIN_USER=$GRAFANA_USER
GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_PASS
ALERT_EMAIL=${ALERT_EMAIL:-alerts@example.com}
# Optional:
# GF_SERVER_ROOT_URL=
# GF_INSTALL_PLUGINS=
EOF

if [ -f "$PRIMARY_SMTP" ]; then
  cp "$PRIMARY_SMTP" "$DEST/.secrets/.env.smtp"
elif [ -f "$ALT_SMTP" ]; then
  cp "$ALT_SMTP" "$DEST/.secrets/.env.smtp"
else
  cat > "$DEST/.secrets/.env.smtp" <<'EOF'
GF_SMTP_ENABLED=false
GF_SMTP_HOST=
GF_SMTP_USER=
GF_SMTP_PASSWORD=
GF_SMTP_SKIP_VERIFY=true
GF_SMTP_FROM_ADDRESS=
EOF
fi

cat > "$DEST/lorawan.env" <<EOF
CHIRPSTACK_URL=http://chirpstack:8080
CHIRPSTACK_TENANT_ID=
CHIRPSTACK_APP_NAME=${SITE_ID}-app
CHIRPSTACK_ADMIN_EMAIL=$CS_ADMIN_EMAIL
CHIRPSTACK_ADMIN_PASSWORD=$CS_ADMIN_PASSWORD
# Optional default app key for cs-seed.py
CHIRPSTACK_DEFAULT_APP_KEY=$DEFAULT_APP_KEY
EOF

cat > "$DEST/storage/chirpstack-devices.yml" <<EOF
app:
  name: "${SITE_ID}-app"
  description: "Devices for ${SITE_ID}"

device_profile:
  name: "rs485-vfd"
  region_id: "us915_1"
  mac_version: "1.0.0"
  reg_params_revision: "RP002-1.0.3"
  supports_class_b: false
  supports_class_c: false

gateway:
  name: "${SITE_ID}-gw"
  description: "Heltec concentrator for ${SITE_ID}"

devices:
  - name: "edge-001"
    description: "RS485 node 1"
    dev_eui: ""
    app_key: "$DEFAULT_APP_KEY"
EOF

cat > "$DEST/storage/README.generated" <<EOF
Generated skeleton for site-pack ($SITE_ID).
Fill in:
- storage/site-config.yml
- storage/profiles/ (optional; only if you want site-local overrides; set PROFILES_DIR=/app/storage/profiles in site-pack/.env)
- storage/chirpstack-devices.yml (DevEUIs, names; app_key can stay default)
- site-pack/lorawan.env (ChirpStack tenant id; admin defaults to $CS_ADMIN_EMAIL with generated password)
- site-pack/.secrets/chirpstack.api_key (bootstrap-lite.sh creates it via the ChirpStack CLI)
- Optional: storage/registry_*.json, miners.sqlite if restoring
EOF

echo "Generated site-pack for $SITE_ID at $DEST"
echo "Influx admin user/pass/token stored in .secrets"
echo "Grafana admin user/pass stored in .secrets/.env.grafana"
echo "Default app key: $DEFAULT_APP_KEY"
echo "Reminder: set ChirpStack tenant ID, create/approve the generated admin, and fill DevEUIs in chirpstack-devices.yml"

if [ "${SKIP_IMAGE_BUILD:-0}" != "1" ]; then
  SITE_AGENT_IMAGE="${SITE_AGENT_IMAGE:-ghcr.io/neddia/site-agent:latest}"
  IMAGE_OUT_DIR="${IMAGE_OUT_DIR:-$SEEDS_ROOT/images}"
  IMAGE_OUT="${IMAGE_OUT:-$IMAGE_OUT_DIR/site-agent.tar}"

  if [ -n "${SITE_AGENT_SRC:-}" ]; then
    SA_SRC="$SITE_AGENT_SRC"
  elif [ -d "$SEEDS_ROOT/../site-agent" ]; then
    SA_SRC="$SEEDS_ROOT/../site-agent"
  elif [ -d "$ROOT/../site-agent" ]; then
    SA_SRC="$ROOT/../site-agent"
  else
    echo "error: site-agent source not found; set SITE_AGENT_SRC" >&2
    exit 1
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker not found; install Docker to build site-agent image" >&2
    exit 1
  fi

  echo "Building site-agent image from $SA_SRC -> $SITE_AGENT_IMAGE"
  docker build -t "$SITE_AGENT_IMAGE" "$SA_SRC"

  echo "Saving site-agent image to $IMAGE_OUT"
  mkdir -p "$IMAGE_OUT_DIR"
  docker save -o "$IMAGE_OUT" "$SITE_AGENT_IMAGE"
fi
