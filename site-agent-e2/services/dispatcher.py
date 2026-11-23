# app/services/dispatcher.py
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from app.config import SiteConfig
from app.services import edge_link as edge_link_mod
from app.services.e2_setup import (
    RID_SYS,
    RID_BUS,
    RID_EXEC,
    RID_INFO,
    RID_POLLS,
    RID_REPORT,
    RID_SAFEHZ,
    RID_STATUS,
    RID_CTRL,
    VID_ACK,
    VID_DEL,
    VID_GET,
    VID_RUN,
    VID_SET,
    encode_exec,
)
from app.services.e2_setup import _ensure_byte  # reuse validation
from app.services.translator.profile_loader import ProfileRepo
from app.settings import Settings
from app.util.logging import get_logger, log_kv

if TYPE_CHECKING:
    from app.services.edge_registry import EdgeIpRegistry
    from app.services.edge_roles import RoleMap
    from app.services.provision import Provisioner


@dataclass
class PendingLora:
    future: asyncio.Future
    expected: Optional[set[tuple[int, int]]]
    expected_counts: Optional[dict[tuple[int, int], int]] = None
    frames: List[bytes] = field(default_factory=list)
    seen: set[tuple[int, int]] = field(default_factory=set)
    satisfied: dict[tuple[int, int], int] = field(default_factory=dict)

@dataclass
class _LoraFragState:
    updated: float
    last_idx: Optional[int] = None
    parts: Dict[int, bytes] = field(default_factory=dict)
    total_body: int = 0

VF_FINAL = 0x02
FRAG_LAST = 0x80
FRAG_IDX_MASK = 0x7F
SYS_VID_BUNDLE = 1
LORA_FRAG_TTL_S = 5.0
LORA_FRAG_MAX_BODY = 4096


class Dispatcher:
    """
    One place serializes access per edge, picks route, retries, and correlates LoRa replies.
    """
    def __init__(
        self,
        settings: Settings,
        profiles: Optional[ProfileRepo] = None,
        site_cfg: Optional[SiteConfig] = None,
        *,
        rolemap: Optional["RoleMap"] = None,
        edge_ips: Optional["EdgeIpRegistry"] = None,
    ):
        self.settings = settings
        self.profiles = profiles
        self.site_cfg = site_cfg
        self.rolemap = rolemap
        self.edge_ips = edge_ips
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pending: Dict[tuple[str, int], PendingLora] = {}  # (dev_eui, msgid) -> pending
        self._lora_fragments: Dict[tuple[str, int, int, int], _LoraFragState] = {}
        self._ingester: Optional["E2Ingester"] = None
        self._provisioner: Optional["Provisioner"] = None
        self.log = get_logger("dispatcher")

    def health(self):
        return {"edges": len(self._locks), "pending": len(self._pending)}

    def attach_ingester(self, ingester: "E2Ingester") -> None:
        self._ingester = ingester

    def attach_provisioner(self, provisioner: "Provisioner") -> None:
        self._provisioner = provisioner

    def code_id_for_op(
        self,
        edge_id: str,
        dev_id: int,
        op: Optional[str],
        *,
        default: Optional[int] = None,
    ) -> int:
        """
        Resolve a code_id for the given op, falling back to the VFD's configured default.
        """
        if op and self._provisioner:
            try:
                return self._provisioner.ensure_code_id(edge_id, dev_id, op)
            except Exception as exc:
                log_kv(
                    self.log,
                    logging.WARNING,
                    evt="dispatch.code_id.resolve_err",
                    edge=edge_id,
                    dev=dev_id,
                    op=op,
                    err=type(exc).__name__,
                )
        if default is not None:
            return default
        raise RuntimeError(f"code_id unavailable for edge={edge_id} dev={dev_id} op={op}")

    async def apply_group_hz(self, group_id: str, hz: float) -> None:
        """
        Batch SET_HZ for every VFD in a miners.group entry. Prefers a single
        EXEC over LoRa (fragmented as needed); falls back to TCP when the edge
        lacks a DevEUI.
        """
        if not self.site_cfg or not self.profiles:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="unconfigured")
            return

        groups = getattr(getattr(self.site_cfg, "miners", None), "groups", []) or []
        group_cfg = next((g for g in groups if getattr(g, "id", None) == group_id), None)
        if not group_cfg:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="unknown_group")
            return

        vfd_cfgs = []
        missing_vfds: List[str] = []
        resolved_vfds = self.site_cfg.resolve_group_vfd_ids(group_cfg) if self.site_cfg else []
        for vid in resolved_vfds:
            try:
                vfd_cfgs.append(self.site_cfg.get_vfd(vid))
            except KeyError:
                missing_vfds.append(str(vid))

        if missing_vfds:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.missing_vfd",
                   group=group_id,
                   vfds=",".join(missing_vfds))

        if not vfd_cfgs:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="no_vfds")
            return

        edge_ids = {cfg.edge_id for cfg in vfd_cfgs}
        pair_primary = self.rolemap.primary_edge_for_vfds(vfd_cfgs) if self.rolemap else None
        if len(edge_ids) != 1 and not pair_primary:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="mixed_edges",
                   edges=",".join(sorted(edge_ids)))
            return

        if pair_primary:
            edge_id = pair_primary
        else:
            edge_id = next(iter(edge_ids))
        try:
            edge_cfg = self.site_cfg.get_edge(edge_id)
        except KeyError:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="edge_missing",
                   edge=edge_id)
            return

        dev_eui = self.site_cfg.get_edge_dev_eui(edge_id, self.settings.DEFAULT_DEV_EUI)
        prefer = (getattr(edge_cfg, "prefer", "TCP") or "TCP").upper()
        if prefer not in {"TCP", "LORA"}:
            prefer = "TCP"
        if prefer == "LORA" and not dev_eui:
            # No LoRa path configured; degrade to TCP.
            prefer = "TCP"
        if pair_primary:
            log_kv(self.log, logging.INFO,
                   evt="dispatch.apply_group_hz.route_override",
                   group=group_id,
                   primary=edge_id,
                   vfds=",".join(cfg.vfd_id for cfg in vfd_cfgs))

        hz_value = float(hz)
        rows: List[Tuple[int, int, bytes]] = []
        profile_cache: Dict[str, Any] = {}

        for cfg in vfd_cfgs:
            profile = profile_cache.get(cfg.model)
            if profile is None:
                try:
                    profile = self.profiles.load(cfg.model)
                except Exception as exc:
                    log_kv(self.log, logging.WARNING,
                           evt="dispatch.apply_group_hz.skip_vfd",
                           group=group_id,
                           vfd=cfg.vfd_id,
                           reason="profile_load",
                           err=type(exc).__name__)
                    continue
                profile_cache[cfg.model] = profile
            try:
                payload = self.profiles.build(profile, "set_hz", {"hz": hz_value}, unit_id=cfg.unit_id)
            except Exception as exc:
                log_kv(self.log, logging.WARNING,
                       evt="dispatch.apply_group_hz.skip_vfd",
                       group=group_id,
                       vfd=cfg.vfd_id,
                       reason="build_failed",
                       err=type(exc).__name__)
                continue
            code_id = self.code_id_for_op(edge_id, cfg.dev_id, "set_hz", default=cfg.code_id)
            rows.append((cfg.dev_id & 0xFF, code_id & 0xFF, payload))

        if not rows:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.skip",
                   group=group_id,
                   reason="no_payloads")
            return

        log_kv(self.log, logging.INFO,
               evt="dispatch.apply_group_hz",
               group=group_id,
               edge=edge_id,
               hz=hz_value,
               vfds=",".join(cfg.vfd_id for cfg in vfd_cfgs))

        if prefer == "LORA" and dev_eui:
            await edge_link_mod.send_exec_lora(
                self.settings,
                dev_eui=dev_eui,
                rows=rows,
                fport=getattr(self.settings, "CS_FPORT", 1),
                confirmed=True,
                ack_req=True,
            )
            return

        # TCP (or fallback) path: single aggregated EXEC via dispatcher.send_raw
        body = encode_exec(rows)
        timeout_ms = int(max(1.0, getattr(self.settings, "TCP_READ_TIMEOUT", 1.0)) * 1000)
        try:
            await self.send_raw(
                edge_id=edge_id,
                dev_eui=dev_eui,
                rid=RID_EXEC,
                vid=VID_RUN,
                body=body,
                ack=True,
                prefer=prefer,
                timeout_ms=timeout_ms,
                allow_fallback=getattr(edge_cfg, "allow_fallback", True),
            )
        except Exception as exc:
            log_kv(self.log, logging.WARNING,
                   evt="dispatch.apply_group_hz.err",
                   group=group_id,
                   edge=edge_id,
                   err=type(exc).__name__,
                   msg=str(exc))

    # Called by LoRa uplink listener
    def resolve_lora(self, dev_eui: str, payload: bytes):
        if len(payload) < 9 or payload[:2] != b"E2":
            return
        vf = payload[2]
        rid = payload[3]
        vid = payload[4]
        msgid = int.from_bytes(payload[5:7], "little")
        frag = payload[7]
        body_len = payload[8]
        if len(payload) < 9 + body_len:
            return
        body = payload[9 : 9 + body_len]
        key = (dev_eui, msgid)
        pending = self._pending.get(key)
        if pending is None:
            return
        frag_idx = frag & FRAG_IDX_MASK
        frag_last = bool(frag & FRAG_LAST)
        frag_key = (dev_eui, rid, vid, msgid)

        now = time.monotonic()
        self._purge_lora_fragments(now)

        if frag_idx == 0 and frag_last and frag_key not in self._lora_fragments:
            # Single-fragment fast path
            if rid == RID_SYS and vid == SYS_VID_BUNDLE:
                for inner_rid, inner_vid, inner_body in self._iter_bundle(body):
                    synth = self._build_synth_frame(inner_rid, inner_vid, msgid, inner_body, final=True)
                    self._handle_pending_frame(pending, synth, key=key)
                return

            self._handle_pending_frame(
                pending,
                payload,
                vf=vf & ~VF_FINAL,
                rid=rid,
                vid=vid,
                frag=frag,
                key=key,
            )
            return

        state = self._lora_fragments.get(frag_key)
        if state is None:
            state = _LoraFragState(updated=now)
            self._lora_fragments[frag_key] = state
        else:
            state.updated = now

        if frag_idx == 0 and frag_last:
            state.parts.clear()
            state.total_body = 0
            state.last_idx = None

        if frag_idx not in state.parts:
            state.parts[frag_idx] = payload
            state.total_body += body_len
            if state.total_body > LORA_FRAG_MAX_BODY:
                self._lora_fragments.pop(frag_key, None)
                return

        if frag_last:
            if state.last_idx is None or frag_idx > state.last_idx:
                state.last_idx = frag_idx

        if state.last_idx is None:
            return
        if any(idx not in state.parts for idx in range(state.last_idx + 1)):
            return

        if self._pending.get(key) is None:
            self._lora_fragments.pop(frag_key, None)
            return

        ordered = [state.parts[idx] for idx in range(state.last_idx + 1)]
        self._lora_fragments.pop(frag_key, None)
        if rid == RID_SYS and vid == SYS_VID_BUNDLE:
            full_body = b"".join(fr[9 : 9 + fr[8]] for fr in ordered)
            for inner_rid, inner_vid, inner_body in self._iter_bundle(full_body):
                synth = self._build_synth_frame(inner_rid, inner_vid, msgid, inner_body, final=True)
                self._handle_pending_frame(pending, synth, key=key)
            return

        for fr in ordered:
            self._handle_pending_frame(
                pending,
                fr,
                vf=fr[2] & ~VF_FINAL,
                rid=rid,
                vid=vid,
                frag=fr[7],
                key=key,
            )

    def _handle_pending_frame(
        self,
        pending: PendingLora,
        payload: bytes,
        *,
        vf: Optional[int] = None,
        rid: Optional[int] = None,
        vid: Optional[int] = None,
        frag: Optional[int] = None,
        key: Optional[tuple[str, int]] = None,
    ) -> None:
        if len(payload) < 9 or payload[:2] != b"E2":
            return
        if vf is None or rid is None or vid is None or frag is None:
            vf = payload[2]
            rid = payload[3]
            vid = payload[4]
            frag = payload[7]
        body_len = payload[8]
        body = payload[9 : 9 + body_len]

        allowed = pending.expected or (pending.expected_counts.keys() if pending.expected_counts else None)
        if allowed and (rid, vid) not in allowed:
            return

        clear = self._is_clear_preamble(rid, vid, body)
        if clear:
            pending.frames.clear()
        else:
            pending.frames.append(payload)

        if self._is_final(vf, frag):
            if pending.expected_counts:
                if (rid, vid) in pending.expected_counts:
                    pending.satisfied[(rid, vid)] = pending.satisfied.get((rid, vid), 0) + 1
                done = all(pending.satisfied.get(k, 0) >= pending.expected_counts.get(k, 0) for k in pending.expected_counts)
                if not done:
                    return
            elif pending.expected:
                pending.seen.add((rid, vid))
                if not pending.seen.issuperset(pending.expected):
                    return
            if key:
                self._pending.pop(key, None)
            if not pending.future.done():
                pending.future.set_result(list(pending.frames))

    async def send_raw(self, *, edge_id: str, dev_eui: Optional[str], rid: int, vid: int,
                       body: bytes, ack: bool, prefer: str="TCP", timeout_ms: int=1500,
                       cap: int=220, read_timeout: Optional[float]=None,
                       allow_fallback: bool=False,
                       expected: Optional[set[tuple[int, int]]] = None,
                       expected_counts: Optional[dict[tuple[int, int], int]] = None) -> Tuple[str,int,List[bytes]]:
        lock = self._locks.setdefault(edge_id, asyncio.Lock())
        async with lock:
            t0 = time.perf_counter()
            # Build E2 frames
            mid = await edge_link_mod.next_msgid()
            self._mark_ingester_tx(edge_id=edge_id, dev_eui=dev_eui, msgid=mid)
            frames = edge_link_mod.build_e2_frames(rid=rid, vid=vid, body=body, ack=ack, cap=cap, msgid=mid)
            expected_pairs = expected if expected is not None else self._expected_reply(rid, vid)
            if expected_counts is None and expected_pairs is not None:
                expected_counts = {pair: 1 for pair in expected_pairs}
            if not ack:
                expected_pairs = None
                expected_counts = None
            stop_pairs: Optional[set[tuple[int, int]]] = None
            if expected_pairs:
                ack_pairs = {pair for pair in expected_pairs if pair[1] == VID_ACK}
                stop_pairs = ack_pairs or expected_pairs

            # TCP path (fast path)
            if prefer == "TCP":
                try:
                    rt = read_timeout if read_timeout is not None else (timeout_ms / 1000.0)
                    rt = max(0.0, rt) + 0.4  # add a small grace period for scheduling jitter
                    host_override, port_override = self._resolve_edge_host(edge_id)
                    replies = await edge_link_mod.send_tcp_frames(
                        self.settings,
                        frames,
                        read_timeout=rt,
                        expected=stop_pairs,
                        expected_counts=expected_counts,
                        msgid=mid,
                        host=host_override,
                        port=port_override,
                    )
                    rtt = int((time.perf_counter()-t0)*1000)
                    return ("TCP", rtt, replies)
                except Exception:
                    if not allow_fallback:
                        raise

            # LoRa path
            if not dev_eui:
                raise RuntimeError("LoRa path requires dev_eui")
            if not ack:
                for fr in frames:
                    await edge_link_mod.send_lora_frame(
                        self.settings,
                        dev_eui=dev_eui,
                        frame=fr,
                        fport=self.settings.CS_FPORT,
                        confirmed=False,
                    )
                rtt = int((time.perf_counter() - t0) * 1000)
                return ("LORA", rtt, [])

            fut = asyncio.get_running_loop().create_future()
            self._pending[(dev_eui, mid)] = PendingLora(
                future=fut,
                expected=expected_pairs,
                expected_counts=expected_counts,
            )
            # E2 over LoRa: one frame per downlink; enqueue in order
            for fr in frames:
                await edge_link_mod.send_lora_frame(self.settings, dev_eui=dev_eui, frame=fr, fport=self.settings.CS_FPORT, confirmed=False)
            try:
                payloads = await asyncio.wait_for(fut, timeout=timeout_ms/1000)
                if isinstance(payloads, bytes):
                    replies = [payloads]
                else:
                    replies = list(payloads)
                rtt = int((time.perf_counter()-t0)*1000)
                return ("LORA", rtt, replies)
            finally:
                self._pending.pop((dev_eui, mid), None)

    # convenience: immediate EXEC of single RS-485 tx bytes
    async def exec_single(self, *, edge_id: str, dev_eui: Optional[str], dev_id: int, code_id: int,
                          tx: Optional[bytes], prefer: str, timeout_ms: int,
                          exec_mode: str = "run") -> Tuple[str, int, bytes]:
        """
        Convenience for E2 EXEC with a single RTU ADU. `exec_mode` controls VID:
          - run: VID_RUN (default)
          - set: VID_SET (persist + run on edge)
          - del: VID_DEL (clear persisted boot exec; ignores tx)
        """
        mode = (exec_mode or "run").lower()
        if mode not in {"run", "set", "del"}:
            raise ValueError("exec_mode must be run, set, or del")

        vid = { "run": VID_RUN, "set": VID_SET, "del": VID_DEL }[mode]
        if mode == "del":
            body = b""
        else:
            if tx is None:
                raise ValueError("tx is required unless exec_mode='del'")
            # Exec body (count=1)
            body = bytes([1, dev_id & 0xFF, code_id & 0xFF, len(tx) & 0xFF]) + tx

        route, rtt, replies = await self.send_raw(edge_id=edge_id, dev_eui=dev_eui,
                                                  rid=RID_EXEC, vid=vid, body=body,
                                                  ack=True, prefer=prefer, timeout_ms=timeout_ms)
        reply_frame = replies[-1] if replies else b""
        reply_payload = self._extract_exec_reply(replies) if replies else b""
        return route, rtt, (reply_payload or reply_frame)

    def _resolve_edge_host(self, edge_id: str) -> tuple[Optional[str], Optional[int]]:
        if not self.edge_ips:
            return (None, None)
        resolved = self.edge_ips.resolve(edge_id)
        if not resolved:
            return (None, None)
        return resolved

    @staticmethod
    def _is_clear_preamble(rid: int, vid: int, body: bytes) -> bool:
        if rid == 2 and vid == 5:  # POLLS / ACK
            return len(body) >= 1 and body[0] == 0
        if rid == 4 and vid == 5:  # SAFEHZ / ACK
            return len(body) >= 3 and body[2] == 0
        return False

    @staticmethod
    def _is_final(vf: int, frag: int) -> bool:
        if vf & VF_FINAL:
            return True
        if frag & 0x80:
            return True
        return False

    def _purge_lora_fragments(self, now: float) -> None:
        if not self._lora_fragments:
            return
        expired = [
            key for key, state in self._lora_fragments.items()
            if now - state.updated > LORA_FRAG_TTL_S
        ]
        for key in expired:
            self._lora_fragments.pop(key, None)

    @staticmethod
    def _expected_reply(rid: int, vid: int) -> Optional[set[tuple[int, int]]]:
        expected: set[tuple[int, int]] = set()
        if rid == RID_EXEC:
            if vid == VID_RUN:
                expected.update({(RID_REPORT, VID_ACK)})
            elif vid == VID_SET:
                expected.update({(RID_EXEC, VID_ACK)})
            elif vid == VID_DEL:
                expected.update({(RID_EXEC, VID_ACK)})
        elif rid == RID_POLLS and vid in {VID_SET, VID_GET, VID_DEL}:
            expected.update({(RID_POLLS, VID_ACK)})
        elif rid == RID_SAFEHZ and vid in {VID_SET, VID_GET, VID_DEL}:
            expected.update({(RID_SAFEHZ, VID_ACK)})
        elif rid == RID_BUS and vid in {VID_SET, VID_GET}:
            expected.update({(RID_BUS, VID_ACK)})
        elif rid == RID_STATUS and vid == VID_GET:
            expected.update({(RID_STATUS, VID_ACK)})
        elif rid == RID_INFO and vid in {VID_GET, VID_SET}:
            expected.update({(RID_INFO, VID_ACK)})
        elif rid == RID_CTRL and vid in {VID_SET, VID_DEL}:
            expected.update({(RID_CTRL, VID_ACK)})
        elif rid == RID_SYS and vid == 1:
            return None
        return expected

    @staticmethod
    def _iter_bundle(body: bytes) -> list[tuple[int, int, bytes]]:
        items: list[tuple[int, int, bytes]] = []
        idx = 0
        total = len(body)
        while idx + 3 <= total:
            rid = body[idx]
            vid = body[idx + 1]
            idx += 2
            length_byte = body[idx]
            idx += 1
            if length_byte == 0xFF:
                if idx + 2 > total:
                    break
                length = int.from_bytes(body[idx : idx + 2], "big")
                idx += 2
            else:
                length = int(length_byte)
            if length < 0 or idx + length > total:
                break
            payload = body[idx : idx + length]
            idx += length
            items.append((rid, vid, payload))
        return items

    @staticmethod
    def _build_synth_frame(rid: int, vid: int, msgid: int, body: bytes, *, final: bool = True) -> bytes:
        _ensure_byte(rid, "rid")
        _ensure_byte(vid, "vid")
        length = len(body)
        if length > 255:
            # Synth frames are only for ACKs/payloads which should be small
            raise ValueError("synthetic frame body too large")
        vf = 0x20  # ack flag not needed
        frag = 0x80 if final else 0x00
        header = bytearray()
        header += b"E2"
        header.append(vf)
        header.append(rid & 0xFF)
        header.append(vid & 0xFF)
        header += msgid.to_bytes(2, "little")
        header.append(frag)
        header.append(length & 0xFF)
        return bytes(header + body)

    @staticmethod
    def _assemble_e2_bodies(frames: List[bytes]) -> List[tuple[int, int, int, bytes]]:
        in_progress: Dict[tuple[int, int, int], bytearray] = {}
        completed: List[tuple[int, int, int, bytes]] = []
        for frame in frames:
            if len(frame) < 9 or frame[:2] != b"E2":
                continue
            vf = frame[2]
            rid = frame[3]
            vid = frame[4]
            msgid = int.from_bytes(frame[5:7], "little")
            frag = frame[7]
            chunk_len = frame[8]
            if len(frame) < 9 + chunk_len:
                continue
            body_chunk = frame[9 : 9 + chunk_len]
            key = (msgid, rid, vid)
            buf = in_progress.setdefault(key, bytearray())
            buf.extend(body_chunk)
            if (vf & VF_FINAL) or (frag & 0x80):
                completed.append((msgid, rid, vid, bytes(buf)))
                in_progress.pop(key, None)
        return completed

    @staticmethod
    def _extract_exec_reply(frames: List[bytes]) -> bytes:
        for _, rid, vid, body in reversed(Dispatcher._assemble_e2_bodies(frames)):
            if rid != RID_REPORT or vid != VID_ACK or not body:
                continue
            n = body[0]
            if n == 0:
                return b""
            idx = 1

            def slice_bytes(count: int) -> bytes:
                nonlocal idx
                if count < 0 or len(body) < idx + count:
                    raise ValueError
                segment = body[idx : idx + count]
                idx += count
                return segment

            try:
                slice_bytes(n)      # dev_id column
                slice_bytes(n)      # code_id column
                slice_bytes(n)      # ok column
                slice_bytes(n * 2)  # rtt column
                slice_bytes(n * 4)  # ts column
            except ValueError:
                continue

            length_modes = (
                ("big", 2),
                ("little", 2),
                ("single", 1),
            )
            for order, width in length_modes:
                idx_lengths = idx
                if width == 1:
                    if len(body) < idx_lengths + n:
                        continue
                    lengths = [body[idx_lengths + i] for i in range(n)]
                else:
                    needed = n * width
                    if len(body) < idx_lengths + needed:
                        continue
                    try:
                        lengths = [
                            int.from_bytes(
                                body[idx_lengths + i * width : idx_lengths + (i + 1) * width],
                                order,
                            )
                            for i in range(n)
                        ]
                    except Exception:
                        continue
                idx_payload = idx_lengths + n * width
                total = sum(lengths)
                if total < 0 or len(body) < idx_payload + total:
                    continue
                rx_concat = body[idx_payload : idx_payload + total]
                offset = 0
                for length in lengths:
                    segment = rx_concat[offset : offset + max(length, 0)]
                    offset += max(length, 0)
                    if length <= 0:
                        continue
                    if len(segment) == length:
                        return bytes(segment)
            # fall through to next body if heuristics fail
        return b""

    def _mark_ingester_tx(self, *, edge_id: str, dev_eui: Optional[str], msgid: int) -> None:
        if not self._ingester:
            return
        self._ingester.mark_tx(edge_id, msgid)
        if dev_eui:
            self._ingester.mark_tx(dev_eui, msgid)


if TYPE_CHECKING:  # pragma: no cover
    from app.services.ingester import E2Ingester
