#!/usr/bin/env bash
# Idempotent helper to stamp the concentrator EUI into global_conf.json.
# Defaults assume sx1302_hal is checked out under the current user's home.
set -euo pipefail

BASE="${SX1302_HAL_DIR:-/home/${SUDO_USER:-$(whoami)}/sx1302_hal}"
PF_DIR="${PF_DIR:-$BASE/packet_forwarder}"
CHIP_ID_BIN="${CHIP_ID_BIN:-$BASE/util_chip_id/chip_id}"
DEV_PATH="${DEV_PATH:-/dev/ht1303-0}"
TEMPLATE="${TEMPLATE:-$PF_DIR/global_conf.template.json}"
OUT="${OUT:-$PF_DIR/global_conf.json}"
GATEWAY_EUI_FILE="${GATEWAY_EUI_FILE:-/opt/edge-stack/site-pack/.secrets/gateway_eui}"

if [ ! -x "$CHIP_ID_BIN" ]; then
  echo "chip_id binary not found at $CHIP_ID_BIN (build sx1302_hal first)" >&2
  exit 1
fi
if [ ! -c "$DEV_PATH" ]; then
  echo "device $DEV_PATH not found; ensure udev rule creates the Heltec symlink" >&2
  exit 1
fi
if [ ! -f "$TEMPLATE" ]; then
  echo "template missing: $TEMPLATE" >&2
  exit 1
fi

GW_EUI=$("$CHIP_ID_BIN" -u -d "$DEV_PATH" | awk '/concentrator EUI/ {gsub("0x","",$NF); print $NF}')
if [ -z "$GW_EUI" ]; then
  echo "failed to read concentrator EUI" >&2
  exit 1
fi
echo "Detected concentrator EUI: $GW_EUI"

tmp="$(mktemp)"
jq --arg gw "$GW_EUI" '.gateway_conf.gateway_ID = $gw' "$TEMPLATE" > "$tmp"
mv "$tmp" "$OUT"
echo "Wrote gateway_ID=$GW_EUI into $OUT"

if [ -n "$GATEWAY_EUI_FILE" ]; then
  mkdir -p "$(dirname "$GATEWAY_EUI_FILE")" || true
  printf "%s\n" "$GW_EUI" > "$GATEWAY_EUI_FILE"
  chmod 600 "$GATEWAY_EUI_FILE" || true
  echo "Saved gateway EUI to $GATEWAY_EUI_FILE"
fi

if systemctl list-unit-files | grep -q '^lora-pkt-fwd.service'; then
  systemctl restart lora-pkt-fwd.service || true
fi
