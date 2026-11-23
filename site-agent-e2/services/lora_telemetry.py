# app/services/lora_telemetry.py
from __future__ import annotations

from typing import Optional

from app.util.logging import get_logger

log = get_logger(__name__)


def handle_event(topic: str, payload: dict, *, influx) -> None:
    if not isinstance(payload, dict):
        return
    if topic.endswith("/event/up"):
        record_lora_uplink(influx, payload)
    elif topic.endswith("/event/log"):
        record_lora_log(influx, payload)
    elif topic.endswith("/event/join"):
        record_lora_join(influx, payload)


def record_lora_uplink(influx, msg: dict) -> None:
    info = msg.get("deviceInfo") or {}
    dev_eui = (info.get("devEui") or "").lower()
    region = (msg.get("regionConfigId") or "").lower()
    dr = _as_int(msg.get("dr"))
    adr_val = msg.get("adr")
    adr = bool(adr_val) if isinstance(adr_val, (bool, int)) else None
    f_cnt = _as_int(msg.get("fCnt"))

    tx_info = msg.get("txInfo") or {}
    lora = (tx_info.get("modulation") or {}).get("lora") or {}
    sf = _as_int(lora.get("spreadingFactor"))
    bw = _as_int(lora.get("bandwidth"))
    tx_freq = _as_int(tx_info.get("frequency"))

    for rx in msg.get("rxInfo") or []:
        gateway_id = (rx or {}).get("gatewayId") or ""
        rssi = _as_int((rx or {}).get("rssi"))
        snr = _as_float((rx or {}).get("snr"))
        freq = _as_int((rx or {}).get("frequency")) or tx_freq
        tags = {
            "dev_eui": dev_eui,
            "gateway_id": gateway_id,
            "region": region,
        }
        fields = {}
        if dr is not None:
            fields["dr"] = dr
        if adr is not None:
            fields["adr"] = bool(adr)
        if rssi is not None:
            fields["rssi"] = rssi
        if snr is not None:
            fields["snr"] = snr
        if sf is not None:
            fields["sf"] = sf
        if bw is not None:
            fields["bandwidth"] = bw
        if freq is not None:
            fields["frequency"] = freq
        if f_cnt is not None:
            fields["f_cnt"] = f_cnt
        if fields:
            try:
                influx.write_point("lora_uplink", tags, fields)
            except Exception as exc:
                log.warning("lora uplink write failed: %s", exc)


def record_lora_log(influx, msg: dict) -> None:
    info = msg.get("deviceInfo") or {}
    dev_eui = (info.get("devEui") or "").lower()
    code = msg.get("code") or ""
    level = msg.get("level") or ""
    ctx = msg.get("context") or {}
    tags = {"dev_eui": dev_eui, "code": code, "level": level}
    fields = {"count": 1}
    item_size = _as_int(ctx.get("item_size"))
    max_payload = _as_int(ctx.get("max_payload_size"))
    if item_size is not None:
        fields["item_size"] = item_size
    if max_payload is not None:
        fields["max_payload_size"] = max_payload
    try:
        influx.write_point("lora_log", tags, fields)
    except Exception as exc:
        log.warning("lora log write failed: %s", exc)


def record_lora_join(influx, msg: dict) -> None:
    info = msg.get("deviceInfo") or {}
    dev_eui = (info.get("devEui") or "").lower()
    region = (msg.get("regionConfigId") or "").lower()
    dev_addr = msg.get("devAddr") or ""
    tags = {"dev_eui": dev_eui, "region": region}
    fields = {"dev_addr": dev_addr, "count": 1}
    try:
        influx.write_point("lora_join", tags, fields)
    except Exception as exc:
        log.warning("lora join write failed: %s", exc)


def _as_int(val) -> Optional[int]:
    try:
        return int(val)
    except Exception:
        return None


def _as_float(val) -> Optional[float]:
    try:
        return float(val)
    except Exception:
        return None
