#!/usr/bin/env bash
# Idempotent helper to prep a Heltec sx1302_hal gateway:
# - clones/updates sx1302_hal (optional)
# - builds chip_id + packet forwarder (optional)
# - installs a udev rule for /dev/ht1303-0 (optional)
# - installs/enables lora-pkt-fwd.service (optional)
# - stamps the concentrator EUI into global_conf.json
set -euo pipefail

SCRIPT_ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd
)"

TARGET_USER="${TARGET_USER:-${SUDO_USER:-edgeadm}}"
TARGET_HOME="$(
  getent passwd "$TARGET_USER" 2>/dev/null | awk -F: '{print $6}' || true
)"
TARGET_HOME="${TARGET_HOME:-/home/$TARGET_USER}"

BASE="${SX1302_HAL_DIR:-$TARGET_HOME/sx1302_hal}"
PF_DIR="${PF_DIR:-$BASE/packet_forwarder}"
CHIP_ID_BIN="${CHIP_ID_BIN:-$BASE/util_chip_id/chip_id}"
CHIP_ID_TIMEOUT="${CHIP_ID_TIMEOUT:-20}"
CHIP_ID_RETRIES="${CHIP_ID_RETRIES:-5}"
CHIP_ID_RETRY_DELAY="${CHIP_ID_RETRY_DELAY:-1}"
DEV_PATH="${DEV_PATH:-/dev/ht1303-0}"
DEV_WAIT_TIMEOUT="${DEV_WAIT_TIMEOUT:-60}"
TEMPLATE="${TEMPLATE:-$PF_DIR/global_conf.json.sx1250.US915.USB}"
OUT="${OUT:-$PF_DIR/global_conf.json}"
GATEWAY_EUI_FILE="${GATEWAY_EUI_FILE:-$SCRIPT_ROOT/site-pack/.secrets/gateway_eui}"

SX1302_HAL_REPO="${SX1302_HAL_REPO:-https://github.com/Lora-net/sx1302_hal.git}"
SX1302_HAL_REF="${SX1302_HAL_REF:-}"

INSTALL_DEPS="${INSTALL_DEPS:-1}"
FETCH_SRC="${FETCH_SRC:-1}"
BUILD_SRC="${BUILD_SRC:-1}"
INSTALL_UDEV="${INSTALL_UDEV:-1}"
INSTALL_SERVICE="${INSTALL_SERVICE:-1}"

UDEV_VID="${UDEV_VID:-0483}"
UDEV_PID="${UDEV_PID:-5740}"
UDEV_SYMLINK="${UDEV_SYMLINK:-ht1303-0}"
UDEV_RULE_FILE="${UDEV_RULE_FILE:-/etc/udev/rules.d/80-heltec-ht1303.rules}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/lora-pkt-fwd.service}"
PKT_FWD_SERVICE="${PKT_FWD_SERVICE:-lora-pkt-fwd.service}"
UDEV_TRIGGER_ALWAYS="${UDEV_TRIGGER_ALWAYS:-1}"

log() { echo "[lora] $*"; }
warn() { echo "[lora] warning: $*" >&2; }
die() { echo "[lora] error: $*" >&2; exit 1; }

need_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    die "run as root (try: sudo $0)"
  fi
}

apt_install() {
  need_root
  apt-get update -y >/dev/null
  apt-get install -y "$@"
}

as_target_user() {
  if [ "${EUID:-$(id -u)}" -eq 0 ] && id "$TARGET_USER" >/dev/null 2>&1; then
    sudo -u "$TARGET_USER" -H "$@"
  else
    "$@"
  fi
}

ensure_deps() {
  if [ "$INSTALL_DEPS" != "1" ]; then
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found; skipping deps install"
    return 0
  fi
  log "installing build/runtime deps (git, make, gcc)"
  apt_install ca-certificates git build-essential make python3 pkg-config
}

ensure_sx1302_hal() {
  if [ "$FETCH_SRC" != "1" ]; then
    return 0
  fi
  if [ -d "$BASE/.git" ]; then
    log "updating sx1302_hal at $BASE"
    as_target_user git -C "$BASE" fetch --prune --tags
    if [ -n "$SX1302_HAL_REF" ]; then
      as_target_user git -C "$BASE" checkout "$SX1302_HAL_REF"
    fi
    as_target_user git -C "$BASE" pull --ff-only || true
    return 0
  fi

  log "cloning sx1302_hal into $BASE"
  mkdir -p "$(dirname "$BASE")"
  as_target_user git clone --depth 1 "$SX1302_HAL_REPO" "$BASE"
  if [ -n "$SX1302_HAL_REF" ]; then
    as_target_user git -C "$BASE" fetch --tags
    as_target_user git -C "$BASE" checkout "$SX1302_HAL_REF"
  fi
}

ensure_built() {
  if [ "$BUILD_SRC" != "1" ]; then
    return 0
  fi
  if [ -x "$CHIP_ID_BIN" ] && [ -x "$PF_DIR/lora_pkt_fwd" ]; then
    return 0
  fi
  [ -d "$BASE" ] || die "sx1302_hal not found at $BASE (set SX1302_HAL_DIR or enable FETCH_SRC=1)"
  if [ ! -f "$BASE/libtools/libtinymt32.a" ]; then
    log "building libtools (tinymt32)"
    as_target_user make -C "$BASE/libtools" || die "build failed: libtools"
  fi
  if [ ! -f "$BASE/libloragw/inc/config.h" ]; then
    log "building libloragw.a (generates config.h)"
    as_target_user make -C "$BASE/libloragw" libloragw.a || die "build failed: libloragw"
  fi
  log "building sx1302_hal utilities"
  as_target_user make -C "$BASE/util_chip_id" || die "build failed: util_chip_id"
  as_target_user make -C "$BASE/packet_forwarder" || die "build failed: packet_forwarder"
  [ -x "$CHIP_ID_BIN" ] || die "chip_id binary not found at $CHIP_ID_BIN after build"
}

ensure_udev_rule() {
  if [ "$INSTALL_UDEV" != "1" ]; then
    return 0
  fi
  need_root
  log "installing udev rule $UDEV_RULE_FILE (VID=$UDEV_VID PID=$UDEV_PID SYMLINK=$UDEV_SYMLINK)"
  if [ -f "$UDEV_RULE_FILE" ] && grep -q "$UDEV_VID" "$UDEV_RULE_FILE" && grep -q "$UDEV_PID" "$UDEV_RULE_FILE"; then
    log "udev rule already present"
  else
    cat >"$UDEV_RULE_FILE" <<EOF
SUBSYSTEM=="tty", ATTRS{idVendor}=="$UDEV_VID", ATTRS{idProduct}=="$UDEV_PID", SYMLINK+="$UDEV_SYMLINK"
EOF
  fi
  if command -v udevadm >/dev/null 2>&1; then
    if [ "$UDEV_TRIGGER_ALWAYS" = "1" ]; then
      timeout 5s udevadm control --reload-rules 2>/dev/null || true
      timeout 10s udevadm trigger --subsystem-match=tty 2>/dev/null || true
      timeout 10s udevadm settle --timeout=10 2>/dev/null || true
    fi
  fi
}

ensure_service() {
  if [ "$INSTALL_SERVICE" != "1" ]; then
    return 0
  fi
  need_root
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; skipping service install"
    return 0
  fi
  if [ ! -x "$PF_DIR/lora_pkt_fwd" ]; then
    die "packet forwarder not found at $PF_DIR/lora_pkt_fwd (build sx1302_hal first)"
  fi
  log "installing systemd service $SERVICE_FILE"
  cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Semtech UDP packet forwarder
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PF_DIR
ExecStart=$PF_DIR/lora_pkt_fwd
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload || true
  systemctl enable --now lora-pkt-fwd.service || true
}

PKT_FWD_WAS_ACTIVE=0
cleanup() {
  if [ "${PKT_FWD_WAS_ACTIVE:-0}" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl start "$PKT_FWD_SERVICE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

ensure_deps
ensure_sx1302_hal
ensure_built
ensure_udev_rule

log "waiting for concentrator device at $DEV_PATH..."
for _i in $(seq 1 $((DEV_WAIT_TIMEOUT * 5))); do
  [ -c "$DEV_PATH" ] && break
  sleep 0.2
done
if [ ! -c "$DEV_PATH" ]; then
  die "device $DEV_PATH not found after ${DEV_WAIT_TIMEOUT}s; replug the concentrator or adjust UDEV_VID/UDEV_PID/DEV_PATH"
fi
if [ ! -f "$TEMPLATE" ]; then
  die "template missing: $TEMPLATE"
fi

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$PKT_FWD_SERVICE"; then
  log "stopping $PKT_FWD_SERVICE to read concentrator EUI"
  systemctl stop "$PKT_FWD_SERVICE" >/dev/null 2>&1 || true
  PKT_FWD_WAS_ACTIVE=1
fi

GW_EUI=""
for _i in $(seq 1 "$CHIP_ID_RETRIES"); do
  if command -v timeout >/dev/null 2>&1; then
    GW_EUI="$(timeout "${CHIP_ID_TIMEOUT}s" "$CHIP_ID_BIN" -u -d "$DEV_PATH" 2>/dev/null | awk '/concentrator EUI/ {gsub("0x","",$NF); print $NF}')"
  else
    GW_EUI="$("$CHIP_ID_BIN" -u -d "$DEV_PATH" 2>/dev/null | awk '/concentrator EUI/ {gsub("0x","",$NF); print $NF}')"
  fi
  [ -n "$GW_EUI" ] && break
  sleep "$CHIP_ID_RETRY_DELAY"
done
if [ -z "$GW_EUI" ]; then
  die "failed to read concentrator EUI (timeout ${CHIP_ID_TIMEOUT}s, retries ${CHIP_ID_RETRIES})"
fi
log "Detected concentrator EUI: $GW_EUI"

tmp="$(mktemp)"
python3 - "$TEMPLATE" "$tmp" "$GW_EUI" "$DEV_PATH" <<'PY'
import re
import sys

template_path, out_path, gateway_eui, dev_path = sys.argv[1:5]
text = open(template_path, "r", encoding="utf-8").read()

def replace_first(pattern: str, replacement: str, haystack: str) -> tuple[str, int]:
    new_text, count = re.subn(pattern, replacement, haystack, count=1, flags=re.MULTILINE)
    return new_text, count

text, n_id = replace_first(r'("gateway_ID"\s*:\s*")[^"]*(")', r"\g<1>" + gateway_eui + r"\g<2>", text)
text, n_path = replace_first(r'("com_path"\s*:\s*")[^"]*(")', r"\g<1>" + dev_path + r"\g<2>", text)
text, _ = replace_first(r'("server_address"\s*:\s*")[^"]*(")', r"\g<1>127.0.0.1\g<2>", text)
text = re.sub(r'("serv_port_up"\s*:\s*)\d+', r"\g<1>1700", text, count=1)
text = re.sub(r'("serv_port_down"\s*:\s*)\d+', r"\g<1>1700", text, count=1)

if n_id == 0:
    raise SystemExit("gateway_ID not found in template")
if n_path == 0:
    # Not all templates have com_path; keep going.
    pass

with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
    if not text.endswith("\n"):
        f.write("\n")
PY
mv "$tmp" "$OUT"
log "Wrote gateway_ID=$GW_EUI into $OUT"

ensure_service

if [ -n "$GATEWAY_EUI_FILE" ]; then
  eui_dir="$(dirname "$GATEWAY_EUI_FILE")"
  if [ "${EUID:-$(id -u)}" -eq 0 ] && id "$TARGET_USER" >/dev/null 2>&1; then
    install -d -m 700 -o "$TARGET_USER" -g "$TARGET_USER" "$eui_dir" || true
    install -m 600 -o "$TARGET_USER" -g "$TARGET_USER" /dev/null "$GATEWAY_EUI_FILE" || true
  else
    mkdir -p "$eui_dir" || true
  fi
  printf "%s\n" "$GW_EUI" > "$GATEWAY_EUI_FILE"
  if [ "${EUID:-$(id -u)}" -eq 0 ] && id "$TARGET_USER" >/dev/null 2>&1; then
    chown "$TARGET_USER:$TARGET_USER" "$GATEWAY_EUI_FILE" 2>/dev/null || true
  fi
  chmod 600 "$GATEWAY_EUI_FILE" || true
  log "Saved gateway EUI to $GATEWAY_EUI_FILE"
fi

if systemctl list-unit-files | grep -q '^lora-pkt-fwd.service'; then
  systemctl restart lora-pkt-fwd.service || true
fi
