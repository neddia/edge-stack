from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Iterable, List, Optional, Tuple

from app.config import SiteConfig
from app.services import edge_link
from app.services.edge_registry import EdgeIpRegistry
from app.services.e2_info import parse_info_body
from app.services.e2_setup import RID_INFO, VID_ACK, VID_GET
from app.settings import Settings
from app.util.logging import get_logger, log_kv


async def run_edge_scan(site_cfg: SiteConfig, settings: Settings, registry: EdgeIpRegistry,
                        *, log: Optional[logging.Logger] = None) -> None:
    """
    Scan configured CIDRs for Edge TCP listeners, ask each one for its reported
    edge_id, and cache the IP locally for Dispatcher overrides.
    """
    logger = log or get_logger("edge.discovery")
    mgmt = getattr(site_cfg, "edge_management", None)
    ranges = getattr(mgmt, "edge_ip_ranges", None) or []
    if not ranges:
        return

    port = getattr(mgmt, "edge_discovery_port", None) or settings.EDGE_TCP_PORT
    concurrency = max(1, int(getattr(mgmt, "edge_scan_concurrency", 64) or 64))
    timeout_ms = max(100, int(getattr(mgmt, "edge_scan_timeout_ms", 800) or 800))
    ips = list(_iter_hosts(ranges))
    if not ips:
        return

    sem = asyncio.Semaphore(concurrency)
    hits = 0

    async def _worker(ip: str) -> bool:
        async with sem:
            dev_hex, mac_hex = await _probe_ip(ip, port, timeout_ms / 1000.0, settings)
        if dev_hex is None and mac_hex is None:
            return False
        edge_id = _match_edge_id(site_cfg, mac_hex, dev_hex)
        if not edge_id:
            log_kv(logger, logging.INFO,
                   evt="edge.discovery.unclaimed",
                   ip=ip,
                   mac=mac_hex or "",
                   dev_eui=dev_hex or "")
            # Persist unclaimed edges so we can inspect MAC/DevEUI from edge-ips.json.
            key = None
            if mac_hex:
                key = f"unclaimed:mac:{mac_hex.lower()}"
            elif dev_hex:
                key = f"unclaimed:dev_eui:{dev_hex.lower()}"
            if key:
                registry.update(edge_id=key, ip=ip, port=port, mac_hex=mac_hex, dev_eui_hex=dev_hex)
            return False
        registry.update(edge_id=edge_id, ip=ip, port=port, mac_hex=mac_hex, dev_eui_hex=dev_hex)
        log_kv(logger, logging.DEBUG,
               evt="edge.discovery.bound",
               edge=edge_id,
               ip=ip,
               mac=mac_hex or "",
               dev_eui=dev_hex or "")
        return True

    tasks = [asyncio.create_task(_worker(ip)) for ip in ips]
    for fut in asyncio.as_completed(tasks):
        result = await fut
        if result:
            hits += 1

    log_kv(logger, logging.INFO,
           evt="edge.discovery.scan",
           cidrs=len(ranges),
           tested=len(ips),
           hits=hits,
           port=port)


def _iter_hosts(ranges: Iterable[str]) -> Iterable[str]:
    for cidr in ranges:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except Exception:
            continue
        for host in net.hosts():
            yield str(host)


async def _probe_ip(ip: str, port: int, timeout_s: float, settings: Settings) -> Tuple[Optional[str], Optional[str]]:
    """
    Send a small INFO/GET request; if we receive an INFO/ACK payload, try to
    parse a printable edge identifier out of the body.
    """
    msgid = await edge_link.next_msgid()
    frames = edge_link.build_e2_frames(
        rid=RID_INFO,
        vid=VID_GET,
        body=b"",
        ack=True,
        cap=220,
        msgid=msgid,
    )
    expected = {(RID_INFO, VID_ACK)}
    try:
        replies = await edge_link.send_tcp_frames(
            settings,
            frames,
            read_timeout=timeout_s,
            expected=expected,
            msgid=msgid,
            host=ip,
            port=port,
        )
    except Exception:
        return (None, None)
    return _extract_info_ids(replies)


def _extract_info_ids(replies: List[bytes]) -> Tuple[Optional[str], Optional[str]]:
    for payload in replies:
        if len(payload) < 9 or payload[:2] != b"E2":
            continue
        if payload[3] != RID_INFO or payload[4] != VID_ACK:
            continue
        body_len = payload[8]
        body = payload[9 : 9 + body_len]
        if len(body) != body_len:
            continue
        info = parse_info_body(body)
        if not info.is_valid:
            continue
        return info.dev_hex, info.mac_hex
    return (None, None)


def _match_edge_id(site_cfg: SiteConfig, mac_hex: Optional[str], dev_hex: Optional[str]) -> Optional[str]:
    if mac_hex:
        mac_norm = mac_hex.lower()
        for edge in getattr(site_cfg, "edges", []) or []:
            if getattr(edge, "eth_mac", None) == mac_norm:
                return edge.id
    if dev_hex:
        dev_norm = dev_hex.lower()
        for edge in getattr(site_cfg, "edges", []) or []:
            if getattr(edge, "dev_eui", None) == dev_norm:
                return edge.id
    return None
