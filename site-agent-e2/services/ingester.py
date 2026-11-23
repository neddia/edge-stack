from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional

from app.config import EdgeCfg, SiteConfig, VfdCfg
from app.services.e2_info import parse_info_body
from app.services.e2_setup import RID_INFO, RID_REPORT, RID_SAFEHZ, RID_STATUS, RID_SYS, VID_ACK
from app.services.ingest import write_vfd_telemetry
from app.services.edge_registry import EdgeIpRegistry
from app.services.translator.profile_loader import ProfileRepo
from app.services.provision import Provisioner
from app.services.dispatcher import Dispatcher
from app.settings import Settings
from app.util.logging import get_logger, log_kv

log = get_logger(__name__)


class E2Ingester:
    """
    Passive listener that re-assembles unsolicited REPORT/STATUS frames and persists useful metrics.
    """

    # TTLs keep caches bounded while tolerating short bursts/duplicates.
    _RECENT_TX_TTL = 30.0       # seconds
    _RECENT_DONE_TTL = 120.0    # seconds
    _MAX_BUFFER_FRAMES = 256

    def __init__(
        self,
        settings: Settings,
        dispatcher: Dispatcher,
        provisioner: Provisioner,
        site_cfg: SiteConfig,
        profiles: ProfileRepo,
        influx,
        edge_ips: EdgeIpRegistry,
    ) -> None:
        self.settings = settings
        self.dispatcher = dispatcher
        self.provisioner = provisioner
        self.site_cfg = site_cfg
        self.profiles = profiles
        self.influx = influx
        self.edge_ips = edge_ips
        mgmt = getattr(site_cfg, "edge_management", None)
        ranges = getattr(mgmt, "edge_ip_ranges", None) or []
        self._discovery_active = bool(ranges)

        self._recent_tx: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._rx_buf: Dict[tuple[str, int], List[bytes]] = defaultdict(list)
        self._recent_done: OrderedDict[tuple[str, int, int, int], float] = OrderedDict()

        self._profile_cache: Dict[str, dict] = {}
        self._code_index: Dict[tuple[str, int, int], tuple[VfdCfg, str]] = {}
        self._code_index_ts: float = 0.0

        self._tap_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """
        Spin up passive TCP taps (if configured) to ingest unsolicited frames.
        """
        self._stop_event.clear()
        if not (self.settings.EDGE_TCP_HOST and self.settings.EDGE_TCP_PORT):
            return

        for edge in self.site_cfg.edges:
            if not self._supports_tcp(edge):
                log_kv(
                    log,
                    20,
                    evt="ingest.tap_skip",
                    edge=edge.id,
                    reason="lora_only",
                )
                continue
            task = asyncio.create_task(self._tcp_tap_loop(edge.id))
            self._tap_tasks[edge.id] = task

    async def stop(self) -> None:
        self._stop_event.set()
        tasks = list(self._tap_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tap_tasks.clear()

    # ---------- public API ----------

    def mark_tx(self, peer: str, msgid: int) -> None:
        """
        Track outbound msgids so we can drop matching replies when they echo back via ingest taps.
        """
        now = time.monotonic()
        key = (peer, msgid)
        self._recent_tx[key] = now
        self._recent_tx.move_to_end(key)
        self._purge_old(self._recent_tx, now, self._RECENT_TX_TTL)

    def handle(self, route: str, peer: str, payload: bytes, *, edge_id: Optional[str] = None) -> None:
        """
        Passive ingest entry point. `peer` is dev_eui (LoRa) or edge_id (TCP).
        """
        if len(payload) < 9 or payload[:2] != b"E2":
            return

        msgid = int.from_bytes(payload[5:7], "little")
        key = (peer, msgid)
        now = time.monotonic()
        self._purge_old(self._recent_tx, now, self._RECENT_TX_TTL)
        self._purge_old(self._recent_done, now, self._RECENT_DONE_TTL)

        if key in self._recent_tx:
            log_kv(
                log,
                20,
                evt="ingest.drop",
                reason="reply",
                route=route,
                peer=peer,
                msgid=f"0x{msgid:04x}",
            )
            return

        if key in getattr(self.dispatcher, "_pending", {}):
            log_kv(
                log,
                20,
                evt="ingest.drop",
                reason="pending",
                route=route,
                peer=peer,
                msgid=f"0x{msgid:04x}",
            )
            return

        buf_key = (peer, msgid)
        frames = self._rx_buf[buf_key]
        if len(frames) >= self._MAX_BUFFER_FRAMES:
            frames.clear()

        frames.append(payload)
        vf = payload[2]
        frag = payload[7]
        final = bool(vf & 0x02) or bool(frag & 0x80)
        if not final:
            return

        self._rx_buf.pop(buf_key, None)
        bodies = self.dispatcher._assemble_e2_bodies(list(frames))  # type: ignore[attr-defined]
        edge = edge_id or self._edge_id_from_peer(peer)

        for mid, rid, vid, body in bodies:
            self._handle_e2_body(route, peer, edge, mid, rid, vid, body, now)

    # ---------- private helpers ----------

    async def _tcp_tap_loop(self, edge_id: str) -> None:
        backoff = 0.5
        jitter = random.uniform

        while not self._stop_event.is_set():
            try:
                host, port = self._tap_target(edge_id)
                if not host or not port:
                    await asyncio.sleep(backoff + jitter(0, 0.5))
                    continue
                reader, writer = await asyncio.open_connection(host, port)
                log_kv(log, 20, evt="ingest.tap_open", route="TCP", edge=edge_id)
                try:
                    await self._tap_hello(writer, edge_id)
                    while not self._stop_event.is_set():
                        hdr = await reader.readexactly(2)
                        length = int.from_bytes(hdr, "big")
                        payload = await reader.readexactly(length) if length else b""
                        if not payload:
                            continue
                        self.handle("TCP", edge_id, payload, edge_id=edge_id)
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_kv(
                    log,
                    30,
                    evt="ingest.tap_err",
                    route="TCP",
                    edge=edge_id,
                    err=type(exc).__name__,
                )
                await asyncio.sleep(backoff + jitter(0, 0.5))
                backoff = min(backoff * 2, 10.0)
            else:
                backoff = 0.5
        log_kv(log, 20, evt="ingest.tap_stop", route="TCP", edge=edge_id)

    async def _tap_hello(self, writer: asyncio.StreamWriter, edge_id: str) -> None:
        """
        Hook for optional tap subscription handshake. Default is no-op.
        """
        return None

    def _tap_target(self, edge_id: str) -> tuple[Optional[str], Optional[int]]:
        resolved = self.edge_ips.resolve(edge_id)
        if resolved:
            host_override, port_override = resolved
        elif self._discovery_active:
            return (None, None)
        else:
            host_override, port_override = (None, None)
        host = host_override or self.settings.EDGE_TCP_HOST
        port = port_override if port_override is not None else self.settings.EDGE_TCP_PORT
        if not host or not port:
            return (None, None)
        return host, port

    def _supports_tcp(self, edge: EdgeCfg) -> bool:
        prefer = (getattr(edge, "prefer", "TCP") or "TCP").upper()
        return prefer == "TCP"

    def _handle_e2_body(
        self,
        route: str,
        peer: str,
        edge: Optional[str],
        msgid: int,
        rid: int,
        vid: int,
        body: bytes,
        now: float,
    ) -> None:
        if rid == RID_SYS and vid == 1:  # bundle envelope
            try:
                for inner_rid, inner_vid, payload in self._iter_bundle(body):
                    self._handle_e2_body(route, peer, edge, msgid, inner_rid, inner_vid, payload, now)
            except ValueError as exc:
                log_kv(
                    log,
                    30,
                    evt="ingest.bundle_err",
                    route=route,
                    edge=edge,
                    peer=peer,
                    msgid=f"0x{msgid:04x}",
                    err=type(exc).__name__,
                )
            return

        dedup_scope = edge or peer
        dedup_key = (dedup_scope, msgid, rid, vid)
        if dedup_key in self._recent_done:
            log_kv(
                log,
                20,
                evt="ingest.drop",
                reason="dup",
                route=route,
                peer=peer,
                msgid=f"0x{msgid:04x}",
                rid=rid,
                vid=vid,
            )
            return

        self._recent_done[dedup_key] = now
        self._recent_done.move_to_end(dedup_key)

        log_kv(
            log,
            20,
            evt="ingest.rx",
            route=route,
            edge=edge,
            peer=peer,
            msgid=f"0x{msgid:04x}",
            rid=rid,
            vid=vid,
            blen=len(body),
        )

        try:
            if rid == RID_REPORT and vid == VID_ACK:
                self._ingest_report(route, peer, edge, msgid, body)

            elif rid == RID_STATUS and vid == VID_ACK:
                self._ingest_status(route, peer, edge, body)

            elif rid == RID_INFO and vid == VID_ACK:
                info = parse_info_body(body)
                if info.is_valid:
                    log_kv(
                        log,
                        10,
                        evt="edge.info",
                        route=route,
                        edge=edge,
                        peer=peer,
                        msgid=f"0x{msgid:04x}",
                        dev_len=info.dev_len,
                        mac_len=info.mac_len,
                        dev_eui=info.dev_hex or "",
                        mac=info.mac_hex or "",
                    )
                else:
                    banner = body.decode("utf-8", "ignore") if body else ""
                    log_kv(
                        log,
                        20,
                        evt="edge.info",
                        route=route,
                        edge=edge,
                        peer=peer,
                        msgid=f"0x{msgid:04x}",
                        dev_len=info.dev_len,
                        mac_len=info.mac_len,
                        truncated=info.truncated,
                        invalid_mac=info.invalid_mac_len,
                        banner=banner.strip() or body.hex(),
                    )

            elif rid == RID_SAFEHZ and vid == VID_ACK:
                self._ingest_safehz(route, peer, edge, body)

            # optional: surface SYS distinctly if you plan to parse it later
            elif rid == RID_SYS:
                log_kv(log, 10, evt="edge.sys", route=route, edge=edge, peer=peer,
                       rid=rid, vid=vid, len=len(body),
                       body=body.hex() if len(body) <= 64 else body[:64].hex())

            else:
                # keep a catch-all for anything you don’t yet handle
                log_kv(
                    log, 20, evt="ingest.unknown", route=route, edge=edge, peer=peer,
                    rid=rid, vid=vid, len=len(body),
                    body=body.hex() if len(body) <= 64 else body[:64].hex(),
                )

        except Exception as exc:  # broad: parsing is best-effort
            log_kv(
                log,
                30,
                evt="ingest.parse_err",
                route=route,
                edge=edge,
                peer=peer,
                rid=rid,
                vid=vid,
                err=type(exc).__name__,
                msg=str(exc),
            )

    @staticmethod
    def _iter_bundle(body: bytes) -> list[tuple[int, int, bytes]]:
        items: list[tuple[int, int, bytes]] = []
        idx = 0
        total = len(body)
        while idx < total:
            if total - idx < 3:
                raise ValueError("bundle header truncated")
            rid = body[idx]
            vid = body[idx + 1]
            idx += 2
            length_byte = body[idx]
            idx += 1
            if length_byte == 0xFF:
                if total - idx < 2:
                    raise ValueError("bundle length truncated")
                length = (body[idx] << 8) | body[idx + 1]
                idx += 2
            else:
                length = length_byte
            if total - idx < length:
                raise ValueError("bundle payload truncated")
            payload = body[idx : idx + length]
            idx += length
            items.append((rid, vid, payload))
        return items

    def _ingest_status(self, route: str, peer: str, edge_id: str, body: bytes) -> None:
        if len(body) < 16:
            raise ValueError("STATUS body too short")

        status = {
            "ts_s": int.from_bytes(body[0:4], "big"),
            "cfg_epoch": int.from_bytes(body[4:8], "big"),
            "timeouts": int.from_bytes(body[8:10], "big"),
            "overflow": int.from_bytes(body[10:12], "big"),
            "rtt_p50": int.from_bytes(body[12:14], "big"),
            "rtt_p95": int.from_bytes(body[14:16], "big"),
            "last_seen_s": int(time.time()),
        }

        tags = {"site": self.site_cfg.site, "edge_id": edge_id or "unknown"}
        self.influx.write_point("edge_status", tags, status)
        log_kv(
            log,
            20,
            evt="edge.status",
            edge=edge_id,
            route=route,
            peer=peer,
            rtt_p50=status["rtt_p50"],
            rtt_p95=status["rtt_p95"],
            timeouts=status["timeouts"],
            overflow=status["overflow"],
        )

    def _ingest_report(self, route: str, peer: str, edge_id: str, msgid: int, body: bytes) -> None:
        if not body:
            return
        count = body[0]
        idx = 1
        needed = count * 3 + count * 2 + count * 4
        if len(body) < 1 + needed:
            raise ValueError("REPORT body incomplete")

        dev_ids = body[idx : idx + count]
        idx += count
        code_ids = body[idx : idx + count]
        idx += count
        oks = body[idx : idx + count]
        idx += count
        rtts = [int.from_bytes(body[idx + i * 2 : idx + (i + 1) * 2], "big") for i in range(count)]
        idx += 2 * count
        # ts column (ignored)
        idx += 4 * count

        lengths, idx = self._parse_lengths(body, idx, count)
        total_len = sum(max(length, 0) for length in lengths)
        if len(body) < idx + total_len:
            raise ValueError("REPORT body payload truncated")

        payload_concat = body[idx : idx + total_len]
        offset = 0

        for i in range(count):
            length = lengths[i]
            if length <= 0 or oks[i] == 0:
                offset += max(length, 0)
                continue

            chunk = payload_concat[offset : offset + length]
            offset += length

            dev_id = int(dev_ids[i])
            code_id = int(code_ids[i])
            rtt_ms = rtts[i]

            try:
                vfd_cfg, op = self._lookup_vfd_op(edge_id, dev_id, code_id)
            except KeyError:
                log_kv(
                    log,
                    20,
                    evt="ingest.unknown_code",
                    edge=edge_id,
                    peer=peer,
                    dev_id=dev_id,
                    code_id=code_id,
                    body=body.hex(),
                )
                continue

            profile = self._load_profile(vfd_cfg.model)
            parsed = self.profiles.parse(profile, op, bytes(chunk))
            write_vfd_telemetry(vfd_cfg, parsed, route, rtt_ms, op=op, code_id=code_id)
            
    def _ingest_safehz(self, route: str, peer: str, edge_id: str, body: bytes) -> None:
        if len(body) < 3:  # interval(2) + count(1)
            return
        ttl = int.from_bytes(body[0:2], "big")
        n = body[2]
        idx = 3
        if len(body) < idx + n:
            return
        dev_ids = [int(b) for b in body[idx: idx+n]]
        # Optional: parse ok/rtt/ts/lengths like REPORT if present; guard with bounds.

        # Map dev -> vfd + SAFEHZ meta
        vfd_by_edge_dev = self._map_vfds_by_edge_dev()
        registry = self.provisioner.registry().get("edges", {}).get(edge_id, {})
        safehz_meta = registry.get("safehz", [])  # list of dicts with dev_id, mode, hz, op, op_sig

        for dev in dev_ids:
            vfd = vfd_by_edge_dev.get((edge_id, dev))
            meta = next((m for m in safehz_meta if int(m.get("dev_id", 0)) == dev), None)
            vfd_id = vfd.vfd_id if vfd else f"dev{dev}"
            mode = (meta or {}).get("mode")
            hz = (meta or {}).get("hz")

            # Log + write Influx
            log_kv(log, 20, evt="safehz.fire", edge=edge_id, vfd=vfd_id, ttl_s=ttl, mode=mode or "?", hz=hz if hz is not None else -1, route=route)
            self.influx.write_point(
                "safehz_event",
                {"site": self.site_cfg.site, "edge_id": edge_id, "vfd_id": vfd_id, "route": route},
                {"ttl_s": ttl, "hz": float(hz) if hz is not None else 0.0, "ok": 1},
            )


    @staticmethod
    def _parse_lengths(body: bytes, idx: int, count: int) -> tuple[List[int], int]:
        remaining = len(body) - idx
        if remaining < count:
            if remaining < count * 2:
                raise ValueError("REPORT lengths missing")
            lengths = [
                int.from_bytes(body[idx + i * 2 : idx + (i + 1) * 2], "big")
                for i in range(count)
            ]
            idx += count * 2
            return lengths, idx
        lengths = [int(body[idx + i]) for i in range(count)]
        idx += count
        return lengths, idx

    def _lookup_vfd_op(self, edge_id: str, dev_id: int, code_id: int) -> tuple[VfdCfg, str]:
        now = time.monotonic()
        if now - self._code_index_ts > 15.0:
            self._refresh_code_index()

        key = (edge_id, dev_id, code_id)
        if key not in self._code_index:
            self._refresh_code_index(force=True)
        return self._code_index[key]

    def _refresh_code_index(self, *, force: bool = False) -> None:
        if not force and self._code_index and (time.monotonic() - self._code_index_ts) < 1.0:
            return
        self._code_index.clear()
        registry = self.provisioner.registry()
        codes = registry.get("codes", {})
        vfd_by_edge_dev = self._map_vfds_by_edge_dev()

        for edge_id, dev_map in codes.items():
            for dev_id_str, ops_map in dev_map.items():
                try:
                    dev_id = int(dev_id_str)
                except ValueError:
                    continue
                vfd = vfd_by_edge_dev.get((edge_id, dev_id))
                for op_name, code_val in ops_map.items():
                    try:
                        code_id = int(code_val)
                    except (TypeError, ValueError):
                        continue
                    if vfd is None:
                        continue
                    self._code_index[(edge_id, dev_id, code_id)] = (vfd, op_name)
        self._code_index_ts = time.monotonic()

    def _map_vfds_by_edge_dev(self) -> Dict[tuple[str, int], VfdCfg]:
        mapping: Dict[tuple[str, int], VfdCfg] = {}
        pair_map = {p.id: p for p in getattr(self.site_cfg, "edge_pairs", []) or []}
        for vfd in self.site_cfg.vfds:
            dev_id = int(vfd.dev_id)
            members = [vfd.edge_id]
            group_id = getattr(vfd, "control_group", None)
            pair = pair_map.get(group_id) if group_id else None
            if pair:
                members.extend(filter(None, [pair.primary, pair.secondary]))
            for member_edge in members:
                if member_edge:
                    mapping[(member_edge, dev_id)] = vfd
        return mapping

    def _edge_id_from_peer(self, peer: str) -> str:
        target = peer.lower()
        for edge in self.site_cfg.edges:
            dev_eui = (edge.dev_eui or "").lower()
            if dev_eui and dev_eui == target:
                return edge.id
        return peer

    @staticmethod
    def _purge_old(store: OrderedDict, now: float, ttl: float) -> None:
        for key, ts in list(store.items()):
            if now - ts > ttl:
                store.pop(key, None)
            else:
                break

    def _load_profile(self, model: str) -> dict:
        cached = self._profile_cache.get(model)
        if cached is not None:
            return cached
        profile = self.profiles.load(model)
        self._profile_cache[model] = profile
        return profile
