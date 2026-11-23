"""Heartbeats for edges over E2 links."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from app.config import SiteConfig
from app.services.dispatcher import Dispatcher
from app.services.e2_setup import RID_SYS
from app.util.logging import get_logger


def _iter_edge_ids(pairs: Iterable, seen: set[str]) -> Iterable[str]:
    for pair in pairs or []:
        for edge_id in (pair.primary, pair.secondary):
            if not edge_id or edge_id in seen:
                continue
            seen.add(edge_id)
            yield edge_id


async def heartbeat_loop(
    dispatcher: Dispatcher,
    site_cfg: SiteConfig,
    period_s: int,
    *,
    allow_fallback: bool = False,
    log: logging.Logger | None = None,
) -> None:
    """Ping every edge in configured pairs to keep SAFEHZ alive."""
    if period_s <= 0:
        return
    log = log or get_logger("heartbeat")
    while True:
        seen: set[str] = set()
        for edge_id in _iter_edge_ids(getattr(site_cfg, "edge_pairs", []), seen):
            try:
                edge_cfg = site_cfg.get_edge(edge_id)
            except KeyError:
                log.warning("heartbeat skipped edge=%s reason=unknown", edge_id)
                continue

            prefer = (getattr(edge_cfg, "prefer", "TCP") or "TCP").upper()
            dev_eui = site_cfg.get_edge_dev_eui(edge_id, None)
            if prefer == "LORA" and not dev_eui:
                log.warning("heartbeat skipped edge=%s reason=missing_dev_eui", edge_id)
                continue

            log.debug("heartbeat send edge=%s prefer=%s route=%s dev_eui=%s",
                      edge_id, prefer, prefer, dev_eui or "")
            try:
                await dispatcher.send_raw(
                    edge_id=edge_id,
                    dev_eui=dev_eui,
                    rid=RID_SYS,
                    vid=0,
                    body=b"",
                    ack=False,
                    prefer=prefer,
                    timeout_ms=300,
                    read_timeout=0,
                    allow_fallback=allow_fallback,
                )
            except Exception as exc:
                log.debug("heartbeat edge=%s err=%s", edge_id, exc)
        await asyncio.sleep(period_s)
