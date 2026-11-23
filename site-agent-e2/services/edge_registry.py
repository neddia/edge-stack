from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Optional, Tuple


class EdgeIpRegistry:
    """
    Minimal JSON-backed registry of last-known IPs per edge id.
    Also tracks MAC/DevEUI fingerprints so we can verify identities later.
    """

    def __init__(self, path: str = "storage/edge-ips.json", *, ttl_s: int = 86400):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = max(0, int(ttl_s))
        self._records: Dict[str, Dict[str, object]] = {}
        self._mac_index: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            self._records = data
            for edge_id, rec in data.items():
                mac = rec.get("mac")
                if isinstance(mac, str) and mac:
                    self._mac_index[mac] = edge_id

    def get(self, edge_id: str) -> Optional[str]:
        resolved = self.resolve(edge_id)
        if resolved:
            return resolved[0]
        return None

    def resolve(self, edge_id: str) -> Optional[Tuple[str, Optional[int]]]:
        rec = self._records.get(edge_id)
        if not isinstance(rec, dict):
            return None
        if self._ttl:
            ts = rec.get("ts") or 0
            if isinstance(ts, (int, float)) and (time.time() - float(ts) > self._ttl):
                return None
        ip = rec.get("ip")
        if not isinstance(ip, str) or not ip:
            return None
        port = rec.get("port")
        try:
            port_val = int(port) if port is not None else None
        except (TypeError, ValueError):
            port_val = None
        return ip, port_val

    def resolve_mac(self, mac_hex: str) -> Optional[str]:
        return self._mac_index.get(mac_hex.lower())

    def update(self, *, edge_id: str, ip: str, port: Optional[int], mac_hex: Optional[str], dev_eui_hex: Optional[str]) -> None:
        record = self._records.setdefault(edge_id, {})
        record.update({
            "ip": ip,
            "port": port,
            "ts": int(time.time()),
        })
        if mac_hex:
            mac_norm = mac_hex.lower()
            record["mac"] = mac_norm
            self._mac_index[mac_norm] = edge_id
        if dev_eui_hex:
            record["dev_eui"] = dev_eui_hex.lower()
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._records, indent=2, sort_keys=True))
        tmp.replace(self.path)
