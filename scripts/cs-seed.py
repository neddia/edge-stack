#!/usr/bin/env python3
"""
Seed ChirpStack with gateway, device profile, application, and devices
from a YAML file (default: site-pack/storage/chirpstack-devices.yml).

Requires:
- CHIRPSTACK_API_KEY or CHIRPSTACK_API_KEY_FILE (default: site-pack/.secrets/chirpstack.api_key)
- Optional CHIRPSTACK_TENANT_ID (otherwise first tenant is used)
- Optional CHIRPSTACK_API_URL (default: http://localhost:8080)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yaml


def read_token() -> str:
    env_key = os.getenv("CHIRPSTACK_API_KEY", "")
    if env_key:
        return env_key.strip()
    key_file = os.getenv("CHIRPSTACK_API_KEY_FILE", "site-pack/.secrets/chirpstack.api_key")
    try:
        return Path(key_file).read_text().strip()
    except OSError as exc:
        raise SystemExit(f"API key not set and cannot read {key_file}: {exc}")


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"devices file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def lower_hex(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum()).lower()


class CSClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.h = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self.h.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: dict) -> dict:
        r = self.h.post(path, json=json)
        r.raise_for_status()
        if r.content:
            return r.json()
        return {}

    def ensure_tenant(self, tenant_id: Optional[str]) -> str:
        if tenant_id:
            return tenant_id
        data = self._get("/api/tenants", params={"limit": 1, "offset": 0})
        items = data.get("result") or data.get("tenants") or []
        if not items:
            raise SystemExit("No tenants found; set CHIRPSTACK_TENANT_ID")
        return items[0].get("id")

    def ensure_application(self, tenant_id: str, app: dict) -> str:
        name = app.get("name")
        if not name:
            raise SystemExit("Application name missing in devices file")
        data = self._get("/api/applications", params={"tenantId": tenant_id, "limit": 1000})
        for item in data.get("result") or data.get("applications") or []:
            if item.get("name") == name:
                return item["id"]
        payload = {
            "name": name,
            "description": app.get("description", ""),
            "tenantId": tenant_id,
        }
        res = self._post("/api/applications", payload)
        return res.get("id") or res.get("applicationId") or ""

    def ensure_device_profile(self, tenant_id: str, dp: dict) -> str:
        name = dp.get("name")
        if not name:
            raise SystemExit("Device profile name missing in devices file")
        data = self._get("/api/device-profiles", params={"tenantId": tenant_id, "limit": 1000})
        for item in data.get("result") or data.get("deviceProfiles") or []:
            if item.get("name") == name:
                return item["id"]
        payload = {
            "name": name,
            "tenantId": tenant_id,
            "regionId": dp.get("region_id") or dp.get("regionId"),
            "macVersion": dp.get("mac_version") or dp.get("macVersion") or "1.0.0",
            "regParamsRevision": dp.get("reg_params_revision") or dp.get("regParamsRevision") or "RP002-1.0.3",
            "supportsClassB": bool(dp.get("supports_class_b", False)),
            "supportsClassC": bool(dp.get("supports_class_c", False)),
            "supportsOtaa": True,
        }
        res = self._post("/api/device-profiles", payload)
        return res.get("id") or res.get("deviceProfileId") or ""

    def ensure_gateway(self, tenant_id: str, gw: dict, gateway_eui: str) -> str:
        gw_id = lower_hex(gateway_eui)
        name = gw.get("name") or gw_id
        data = self._get("/api/gateways", params={"tenantId": tenant_id, "limit": 1000})
        for item in data.get("result") or data.get("gateways") or []:
            if item.get("gatewayId") == gw_id or item.get("name") == name:
                return item["gatewayId"]
        payload = {
            "gatewayId": gw_id,
            "name": name,
            "description": gw.get("description", ""),
            "tenantId": tenant_id,
        }
        self._post("/api/gateways", payload)
        return gw_id

    def ensure_device(self, dev: dict, app_id: str, dp_id: str, default_app_key: Optional[str] = None) -> None:
        dev_eui = lower_hex(dev.get("dev_eui", ""))
        if not dev_eui:
            raise SystemExit("Device missing dev_eui")
        name = dev.get("name") or dev_eui
        # Check existing
        data = self._get("/api/devices", params={"applicationId": app_id, "limit": 1000})
        for item in data.get("result") or data.get("devices") or []:
            if item.get("devEui") == dev_eui:
                return
        payload = {
            "devEui": dev_eui,
            "name": name,
            "description": dev.get("description", ""),
            "applicationId": app_id,
            "deviceProfileId": dp_id,
            "isDisabled": False,
        }
        self._post("/api/devices", payload)
        app_key = lower_hex(dev.get("app_key") or default_app_key or "")
        if app_key:
            keys_payload = {"devEui": dev_eui, "appKey": app_key, "nwkKey": app_key}
            self._post(f"/api/devices/{dev_eui}/keys", keys_payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed ChirpStack from YAML")
    parser.add_argument("--devices-file", default="site-pack/storage/chirpstack-devices.yml", help="Path to chirpstack-devices.yml")
    parser.add_argument("--gateway-eui", default=None, help="Gateway EUI (overrides file)")
    parser.add_argument("--gateway-eui-file", default="site-pack/.secrets/gateway_eui", help="Path to gateway EUI file")
    args = parser.parse_args()

    cfg = load_yaml(Path(args.devices_file))
    token = read_token()
    base_url = os.getenv("CHIRPSTACK_API_URL", "http://localhost:8080")
    tenant_id_env = os.getenv("CHIRPSTACK_TENANT_ID")
    default_app_key = os.getenv("CHIRPSTACK_DEFAULT_APP_KEY") or os.getenv("DEFAULT_APP_KEY")

    gateway_eui = args.gateway_eui
    if not gateway_eui and args.gateway_eui_file:
        try:
            gateway_eui = Path(args.gateway_eui_file).read_text().strip()
        except OSError:
            gateway_eui = None
    if not gateway_eui:
        print("warning: gateway EUI not provided; skipping gateway creation", file=sys.stderr)

    client = CSClient(base_url, token)
    tenant_id = client.ensure_tenant(tenant_id_env)
    app_id = client.ensure_application(tenant_id, cfg.get("app", {}))
    dp_id = client.ensure_device_profile(tenant_id, cfg.get("device_profile", {}))
    if gateway_eui:
        client.ensure_gateway(tenant_id, cfg.get("gateway", {}), gateway_eui)
    for dev in cfg.get("devices", []):
        client.ensure_device(dev, app_id, dp_id, default_app_key=default_app_key)

    print("ChirpStack seeding complete.")


if __name__ == "__main__":
    main()
