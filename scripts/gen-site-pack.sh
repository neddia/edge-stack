#!/usr/bin/env bash
set -euo pipefail

# Generate a site-pack skeleton with random secrets and templated env files.
# Usage: gen-site-pack.sh <site-id> <output-dir>

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <site-id> <output-dir>" >&2
  exit 1
fi

SITE_ID="$1"
DEST="$(realpath "$2")"

mkdir -p "$DEST"/.secrets "$DEST"/storage

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

cat > "$DEST/.env" <<EOF
LOG_LEVEL=INFO
SITE_CONFIG_PATH=/app/storage/site-config.yml
PROFILES_DIR=/app/storage/profiles
INFLUX_URL=http://influxdb:8086
INFLUX_ORG=edge-org
INFLUX_BUCKET=edge-site
INFLUX_RETENTION=30d
INFLUX_TOKEN_FILE=/secrets/influx.token
CS_GRPC_SERVER=chirpstack:8080
CS_GRPC_TOKEN=
CS_FPORT=1
DEFAULT_DEV_EUI=
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
# Optional:
# GF_SERVER_ROOT_URL=
# GF_INSTALL_PLUGINS=
EOF

cat > "$DEST/.secrets/.env.smtp" <<'EOF'
GF_SMTP_ENABLED=false
GF_SMTP_HOST=
GF_SMTP_USER=
GF_SMTP_PASSWORD=
GF_SMTP_SKIP_VERIFY=true
GF_SMTP_FROM_ADDRESS=
EOF

cat > "$DEST/lorawan.env" <<EOF
CHIRPSTACK_URL=http://chirpstack:8080
CHIRPSTACK_TENANT_ID=
CHIRPSTACK_APP_NAME=${SITE_ID}-app
CHIRPSTACK_ADMIN_EMAIL=
CHIRPSTACK_ADMIN_PASSWORD=
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
- storage/profiles/
- storage/chirpstack-devices.yml (DevEUIs, names; app_key can stay default)
- site-pack/lorawan.env (ChirpStack admin/tenant creds)
- site-pack/.secrets/chirpstack.api_key (or let init overlay create it)
- Optional: storage/registry_*.json, miners.db if restoring
EOF

echo "Generated site-pack for $SITE_ID at $DEST"
echo "Influx admin user/pass/token stored in .secrets"
echo "Grafana admin user/pass stored in .secrets/.env.grafana"
echo "Default app key: $DEFAULT_APP_KEY"
echo "Reminder: fill chirpstack admin/tenant in lorawan.env and DevEUIs in chirpstack-devices.yml"
