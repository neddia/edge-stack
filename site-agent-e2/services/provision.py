from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from fastapi import HTTPException

from app.config import SiteConfig, VfdCfg, VfdPollCfg, VfdSafeCfg
from app.services.dispatcher import Dispatcher
from app.services.e2_setup import (
    RID_SYS,
    RID_POLLS,
    RID_INFO,
    RID_SAFEHZ,
    RID_STATUS,
    RID_CTRL,
    VID_ACK,
    VID_GET,
    VID_DEL,
    VID_SET,
    encode_bundle,
    encode_polls,
    encode_info_dict,
    encode_safehz,
    encode_ctrl_mask,
)
from app.services.translator.profile_loader import ProfileRepo
from app.settings import Settings
from app.util.logging import get_logger
from app.services import edge_link as edge_link_mod

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.services.edge_roles import RoleMap


@dataclass
class PollMeta:
    edge_id: str
    vfd_id: str
    dev_id: int
    code_id: int
    op: str
    period_s: int
    op_sig: str


@dataclass
class SafeMeta:
    edge_id: str
    vfd_id: str
    dev_id: int
    code_id: int
    mode: str
    hz: Optional[float]
    ttl_s: int
    op: Optional[str]
    op_sig: Optional[str]


class Provisioner:
    """
    Turns site-config policy into Edge101 POLLS/SAFEHZ plans and keeps a local registry
    so ingest can map report frames back to profile ops.
    """

    def __init__(
        self,
        settings: Settings,
        dispatcher: Dispatcher,
        profiles: ProfileRepo,
        site_cfg: SiteConfig,
        *,
        rolemap: Optional["RoleMap"] = None,
        registry_path: str = "storage/provision-registry.json",
        ctrl_registry=None,
    ):
        self.settings = settings
        self.dispatcher = dispatcher
        self.profiles = profiles
        self.site_cfg = site_cfg
        self.rolemap = rolemap
        self.ctrl_registry = ctrl_registry
        self.registry_path = Path(registry_path)
        self._lock = asyncio.Lock()
        self._state = self._load_state()
        self._state.setdefault("codes", {})
        self.dispatcher.attach_provisioner(self)

    async def apply(
        self,
        edge_id: str,
        *,
        vfd_ids: Optional[Sequence[str]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        async with self._lock:
            edge_cfg, vfds = self._select_vfds(edge_id=edge_id, vfd_ids=vfd_ids)
            edge_state = self._edge_state(edge_id)
            plan = self._build_plan(edge_id, vfds)
            previous_hash = edge_state.get("hash")
            previous_poll_hash = edge_state.get("poll_hash") or ""
            previous_safe_hash = edge_state.get("safe_hash") or ""
            previous_mask_hash = edge_state.get("mask_hash") or ""

            if not force and plan["hash"] and plan["hash"] == previous_hash:
                return {
                    "edge_id": edge_id,
                    "skipped": True,
                    "reason": "unchanged",
                    "hash": plan["hash"],
                    "status": {"skipped": True, "reason": "not_requested"},
                }

            prefer = self._edge_prefer(edge_cfg)
            dev_eui = self._resolve_dev_eui(edge_cfg, prefer)
            # LoRa bundles can take a while (multiple ACKs, NVS writes); temporarily widen timeout.
            timeout_ms = 20000 if prefer == "LORA" else self._default_timeout_ms(prefer)

            poll_result: Optional[Dict[str, Any]] = None
            safe_result: Optional[Dict[str, Any]] = None
            mask_result: Optional[Dict[str, Any]] = None

            bundle_items: list[tuple[int, int, bytes]] = []
            expected_pairs: set[tuple[int, int]] = set()
            expected_counts: dict[tuple[int, int], int] = {}

            def _add_bundle_item(rid: int, vid: int, payload: bytes) -> None:
                bundle_items.append((rid, vid, payload))
                exp = self.dispatcher._expected_reply(rid, vid)
                if exp:
                    # Only wait for ACK variants; the original rid/vid won't reappear in replies.
                    ack_only = {pair for pair in exp if pair[1] == VID_ACK}
                    expected_pairs.update(ack_only)
                    for pair in ack_only:
                        expected_counts[pair] = expected_counts.get(pair, 0) + 1

            if plan["poll_rows"]:
                poll_changed = plan["poll_hash"] != previous_poll_hash
                if poll_changed or force or not previous_poll_hash:
                    poll_body = encode_polls(plan["poll_rows"])
                    _add_bundle_item(RID_POLLS, VID_SET, poll_body)
                    poll_result = {"pending": True, "rows": len(plan["poll_rows"])}
                else:
                    poll_result = {"skipped": True, "reason": "unchanged"}
            elif previous_poll_hash:
                _add_bundle_item(RID_POLLS, VID_DEL, b"")
                poll_result = {"pending": True, "rows": 0, "action": "cleared"}

            if plan["safe_interval"] is not None:
                safe_changed = (
                    plan["safe_hash"] != previous_safe_hash
                    or plan["safe_interval"] != edge_state.get("safe_interval")
                )
                if safe_changed or force or not previous_safe_hash:
                    safe_body = encode_safehz(plan["safe_interval"], plan["safe_rows"])
                    _add_bundle_item(RID_SAFEHZ, VID_SET, safe_body)
                    safe_result = {"pending": True, "rows": len(plan["safe_rows"])}
                else:
                    safe_result = {"skipped": True, "reason": "unchanged"}
            elif previous_safe_hash:
                _add_bundle_item(RID_SAFEHZ, VID_DEL, b"")
                safe_result = {"pending": True, "rows": 0, "action": "cleared"}

            # CTRL masks: on change (or force), wipe then set full rows. If empty and previously set, clear.
            if plan["mask_rows"]:
                mask_changed = plan["mask_hash"] != previous_mask_hash
                if mask_changed or force or not previous_mask_hash:
                    _add_bundle_item(RID_CTRL, VID_DEL, b"")
                    mask_body = encode_ctrl_mask(plan["mask_rows"])
                    _add_bundle_item(RID_CTRL, VID_SET, mask_body)
                    mask_result = {"pending": True, "rows": len(plan["mask_rows"]), "action": "reset_and_set"}
                else:
                    mask_result = {"skipped": True, "reason": "unchanged"}
            elif previous_mask_hash:
                _add_bundle_item(RID_CTRL, VID_DEL, b"")
                mask_result = {"pending": True, "rows": 0, "action": "cleared"}

            # Best-effort: publish dev/code name dictionary for edge logs.
            try:
                dev_rows: list[tuple[int, str]] = []
                seen_devs: set[int] = set()
                for vfd in vfds:
                    if vfd.dev_id in seen_devs:
                        continue
                    seen_devs.add(vfd.dev_id)
                    dev_rows.append((vfd.dev_id, vfd.vfd_id))

                code_rows: list[tuple[int, str]] = []
                seen_codes: set[int] = set()
                for meta in plan["poll_meta"]:
                    if meta.edge_id != edge_id:
                        continue
                    if meta.code_id in seen_codes:
                        continue
                    seen_codes.add(meta.code_id)
                    code_rows.append((meta.code_id, meta.op))

                if dev_rows or code_rows:
                    info_body = encode_info_dict(dev_rows, code_rows)
                    _add_bundle_item(RID_INFO, VID_SET, info_body)
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("info_dict failed edge=%s err=%s", edge_id, type(exc).__name__)

            # Optional: fetch status after apply; skipping to reduce bundle round-trip time.
            include_status = False
            if include_status:
                _add_bundle_item(RID_STATUS, VID_GET, b"")

            if not bundle_items:
                return {
                    "edge_id": edge_id,
                    "hash": plan["hash"],
                    "polls": poll_result,
                    "safehz": safe_result,
                    "ctrl_mask": mask_result,
                    "status": {"skipped": True, "reason": "no_ops"},
                }

            bundle_body = encode_bundle(bundle_items)
            if prefer == "LORA" and dev_eui:
                cap = edge_link_mod.get_lorawan_body_cap(dev_eui=dev_eui, settings=self.settings)
            else:
                cap = 255

            item_labels = ",".join(f"{rid}/{vid}" for rid, vid, _ in bundle_items)
            expect_labels = ",".join(
                f"{rid}/{vid}x{expected_counts.get((rid, vid), 0)}" for rid, vid in sorted(expected_pairs)
            )
            logger.info(
                "provision send E2 bundle edge=%s prefer=%s dev_eui=%s items=%s expect=%s cap=%s timeout_ms=%s len=%s",
                edge_id,
                prefer,
                dev_eui or "",
                item_labels or "-",
                expect_labels or "-",
                cap,
                timeout_ms,
                len(bundle_body),
            )

            route, rtt_ms, replies = await self.dispatcher.send_raw(
                edge_id=edge_id,
                dev_eui=dev_eui,
                rid=RID_SYS,
                vid=1,
                body=bundle_body,
                ack=True,
                prefer=prefer,
                timeout_ms=timeout_ms,
                cap=cap,
                expected=expected_pairs or None,
                expected_counts=expected_counts or None,
            )

            # Persist state for drift detection and ingest mapping
            self._persist_plan_state(edge_id, plan)

            # Unpack replies per section
            if poll_result and poll_result.get("pending"):
                poll_result.update(
                    {
                        "route": route,
                        "rtt_ms": rtt_ms,
                        "acks": self._extract_ack_frames(replies, RID_POLLS),
                    }
                )
            if safe_result and safe_result.get("pending"):
                safe_result.update(
                    {
                        "route": route,
                        "rtt_ms": rtt_ms,
                        "acks": self._extract_ack_frames(replies, RID_SAFEHZ),
                    }
                )
            if mask_result and mask_result.get("pending"):
                mask_result.update(
                    {
                        "route": route,
                        "rtt_ms": rtt_ms,
                        "acks": self._extract_ack_frames(replies, RID_CTRL),
                    }
                )

            if include_status:
                try:
                    status_body = self._extract_status_body(replies)
                    if status_body is None:
                        raise HTTPException(status_code=502, detail="Status ACK missing")
                    status = {
                        "ts_s": int.from_bytes(status_body[0:4], "big"),
                        "cfg_epoch": int.from_bytes(status_body[4:8], "big"),
                        "timeouts": int.from_bytes(status_body[8:10], "big"),
                        "overflow": int.from_bytes(status_body[10:12], "big"),
                        "rtt_p50": int.from_bytes(status_body[12:14], "big"),
                        "rtt_p95": int.from_bytes(status_body[14:16], "big"),
                    }
                except HTTPException as exc:
                    status = {"error": exc.detail, "status_code": exc.status_code}
            else:
                status = {"skipped": True, "reason": "not_requested"}

            return {
                "edge_id": edge_id,
                "hash": plan["hash"],
                "polls": poll_result,
                "safehz": safe_result,
                "ctrl_mask": mask_result,
                "status": status,
            }

    async def fetch_status(
        self,
        edge_id: str,
        *,
        prefer: Optional[str] = None,
        dev_eui: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        edge_cfg = self._get_edge(edge_id)
        prefer = prefer or self._edge_prefer(edge_cfg)
        dev_eui = dev_eui or self._resolve_dev_eui(edge_cfg, prefer)
        timeout_ms = timeout_ms or self._default_timeout_ms(prefer)

        try:
            route, rtt_ms, replies = await self.dispatcher.send_raw(
                edge_id=edge_id,
                dev_eui=dev_eui,
                rid=RID_STATUS,
                vid=VID_GET,
                body=b"",
                ack=True,
                prefer=prefer,
                timeout_ms=timeout_ms,
            )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="STATUS request timed out") from exc

        body = self._extract_status_body(replies)

        if body is None or len(body) < 16:
            raise HTTPException(status_code=502, detail="Invalid STATUS/ACK body")

        status = {
            "ts_s": int.from_bytes(body[0:4], "big"),
            "cfg_epoch": int.from_bytes(body[4:8], "big"),
            "timeouts": int.from_bytes(body[8:10], "big"),
            "overflow": int.from_bytes(body[10:12], "big"),
            "rtt_p50": int.from_bytes(body[12:14], "big"),
            "rtt_p95": int.from_bytes(body[14:16], "big"),
        }
        return {"route": route, "rtt_ms": rtt_ms, "status": status}

    def registry(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._state, sort_keys=True))

    # ---------- internal helpers ----------

    def _build_plan(self, edge_id: str, vfds: Sequence[VfdCfg]) -> Dict[str, Any]:
        poll_rows: List[Tuple[int, int, int, bytes]] = []
        poll_meta: List[PollMeta] = []
        safe_rows: List[Tuple[int, bytes]] = []
        safe_meta: List[SafeMeta] = []
        mask_rows: List[Tuple[int, int]] = []
        safe_intervals: set[int] = set()
        hash_seed: Dict[str, Any] = {"vfds": [], "mask": []}

        seen_mask_ids: set[int] = set()

        for vfd in vfds:
            profile = self._load_profile(vfd)

            vfd_seed: Dict[str, Any] = {"vfd_id": vfd.vfd_id}
            allow_polls = self._allow_polls(edge_id, vfd)
            allow_safe = self._allow_safe(edge_id, vfd)
            allow_ctrl = self._allow_ctrl(edge_id, vfd)

            ctrl_state = self._ctrl_state(vfd)

            if vfd.polls and allow_polls:
                vfd_seed["polls"] = []
                for poll in vfd.polls:
                    row, meta = self._build_poll_row(edge_id, vfd, poll, profile)
                    poll_rows.append(row)
                    poll_meta.append(meta)
                    vfd_seed["polls"].append(
                        {"op": poll.op, "period_s": poll.period_s, "params": poll.params}
                    )

            # Apply safehz only if enabled in ctrl state
            if vfd.safe and allow_safe and ctrl_state.allow_safehz:
                vfd_seed["safe"] = {
                    "mode": vfd.safe.mode,
                    "hz": vfd.safe.hz,
                    "ttl_s": vfd.safe.ttl_s,
                }
                interval, rows, metas = self._build_safe_rows(edge_id, vfd, vfd.safe, profile)
                safe_intervals.add(interval)
                safe_rows.extend(rows)
                safe_meta.extend(metas)

            if vfd_seed.keys() - {"vfd_id"}:
                hash_seed["vfds"].append(vfd_seed)

            if allow_ctrl and vfd.dev_id not in seen_mask_ids:
                # Flags: bit0=ALLOW_BOOT, bit1=ALLOW_SAFEHZ
                flags = 0x00
                if ctrl_state.allow_boot:
                    flags |= 0x01
                if ctrl_state.allow_safehz:
                    flags |= 0x02
                seen_mask_ids.add(vfd.dev_id)
                mask_rows.append((vfd.dev_id, flags))
                hash_seed["mask"].append({"dev_id": vfd.dev_id, "flags": flags})

        hash_bytes = json.dumps(hash_seed, sort_keys=True).encode("utf-8")
        hash_value = hashlib.blake2s(hash_bytes, digest_size=16).hexdigest() if hash_seed["vfds"] else ""

        safe_interval: Optional[int]
        if not safe_intervals:
            safe_interval = None
        elif len(safe_intervals) == 1:
            safe_interval = safe_intervals.pop()
        else:
            raise HTTPException(
                status_code=400,
                detail=f"SafeHz ttl_s must match across VFDs on edge '{edge_id}'",
            )

        poll_hash = self._hash_obj([meta.__dict__ for meta in poll_meta])
        safe_hash = self._hash_obj([meta.__dict__ for meta in safe_meta])
        mask_hash = self._hash_obj(hash_seed["mask"])

        return {
            "hash": hash_value,
            "poll_hash": poll_hash,
            "safe_hash": safe_hash,
            "mask_hash": mask_hash,
            "poll_rows": poll_rows,
            "poll_meta": poll_meta,
            "safe_interval": safe_interval,
            "safe_rows": safe_rows,
            "safe_meta": safe_meta,
            "mask_rows": mask_rows,
        }

    def _allow_polls(self, edge_id: str, vfd: VfdCfg) -> bool:
        if self.rolemap:
            return self.rolemap.is_primary_for(edge_id, vfd)
        return vfd.edge_id == edge_id

    def _allow_safe(self, edge_id: str, vfd: VfdCfg) -> bool:
        if self.rolemap:
            return self.rolemap.is_member(edge_id, vfd)
        return vfd.edge_id == edge_id

    def _allow_ctrl(self, edge_id: str, vfd: VfdCfg) -> bool:
        # Mirror safehz eligibility
        return self._allow_safe(edge_id, vfd)

    def _ctrl_state(self, vfd: VfdCfg):
        if not getattr(self.site_cfg, "service_active", True):
            class _Inactive:
                allow_boot = False
                allow_safehz = False
                allow_auto = False
            return _Inactive()
        if self.ctrl_registry:
            try:
                return self.ctrl_registry.get(vfd.vfd_id)
            except Exception:
                pass
        # default allow-all
        class _Default:
            allow_boot = True
            allow_safehz = True
            allow_auto = True
        return _Default()

    def _build_poll_row(
        self,
        edge_id: str,
        vfd: VfdCfg,
        poll: VfdPollCfg,
        profile: Dict[str, Any],
    ) -> Tuple[Tuple[int, int, int, bytes], PollMeta]:
        params = dict(poll.params or {})
        try:
            tx = self.profiles.build(profile, poll.op, params, unit_id=vfd.unit_id)
        except Exception as exc:
            msg = f"Failed to build poll op '{poll.op}' for VFD '{vfd.vfd_id}': {exc}"
            raise HTTPException(status_code=400, detail=msg) from exc
        sig = hashlib.blake2s(tx, digest_size=12).hexdigest()
        code_id = self._assign_code_id(edge_id, vfd.dev_id, poll.op)
        row = (vfd.dev_id, code_id, poll.period_s, tx)
        meta = PollMeta(
            edge_id=edge_id,
            vfd_id=vfd.vfd_id,
            dev_id=vfd.dev_id,
            code_id=code_id,
            op=poll.op,
            period_s=poll.period_s,
            op_sig=sig,
        )
        return row, meta

    def _build_safe_rows(
        self,
        edge_id: str,
        vfd: VfdCfg,
        safe: VfdSafeCfg,
        profile: Dict[str, Any],
    ) -> Tuple[int, List[Tuple[int, bytes]], List[SafeMeta]]:
        rows: List[Tuple[int, bytes]] = []
        metas: List[SafeMeta] = []
        op_name: Optional[str]
        tx_bytes: Optional[bytes]

        if safe.mode == "hold_last":
            op_name = None
            tx_bytes = None
        elif safe.mode == "stop":
            op_name = "stop"
            params: Dict[str, Any] = {}
            try:
                tx_bytes = self.profiles.build(profile, op_name, params, unit_id=vfd.unit_id)
            except Exception as exc:
                msg = f"Failed to build safe op '{op_name}' for VFD '{vfd.vfd_id}': {exc}"
                raise HTTPException(status_code=400, detail=msg) from exc
        else:  # "safe"
            op_name = "set_hz"
            params = {"hz": safe.hz}
            try:
                tx_bytes = self.profiles.build(profile, op_name, params, unit_id=vfd.unit_id)
            except Exception as exc:
                msg = f"Failed to build safe Hz op for VFD '{vfd.vfd_id}': {exc}"
                raise HTTPException(status_code=400, detail=msg) from exc

        if tx_bytes:
            rows.append((vfd.dev_id, tx_bytes))
            op_sig = hashlib.blake2s(tx_bytes, digest_size=12).hexdigest()
        else:
            op_sig = None

        metas.append(
            SafeMeta(
                edge_id=edge_id,
                vfd_id=vfd.vfd_id,
                dev_id=vfd.dev_id,
                code_id=vfd.code_id,
                mode=safe.mode,
                hz=safe.hz,
                ttl_s=safe.ttl_s,
                op=op_name,
                op_sig=op_sig,
            )
        )

        return safe.ttl_s, rows, metas

    def _extract_status_body(self, frames: Sequence[bytes]) -> Optional[bytes]:
        bodies = self.dispatcher._assemble_e2_bodies(list(frames))  # type: ignore[attr-defined]
        short_len: Optional[int] = None
        for _, rid, vid, body in reversed(bodies):
            if rid != RID_STATUS or vid != VID_ACK:
                continue
            if len(body) >= 16:
                return body
            short_len = len(body)
        if short_len is not None:
            logger.warning("STATUS/ACK body too short (len=%s)", short_len)
        return None

    def _extract_ack_frames(self, frames: Sequence[bytes], rid: int) -> List[Dict[str, Any]]:
        acks: List[Dict[str, Any]] = []
        for _, fr_rid, fr_vid, body in self.dispatcher._assemble_e2_bodies(list(frames)):  # type: ignore[attr-defined]
            if fr_rid == rid and fr_vid == VID_ACK:
                acks.append({"rid": fr_rid, "vid": fr_vid, "len": len(body)})
        return acks

    def _persist_plan_state(self, edge_id: str, plan: Dict[str, Any]) -> None:
        edge_state = self._state.setdefault("edges", {}).setdefault(edge_id, {})
        edge_state["hash"] = plan["hash"]
        edge_state["poll_hash"] = plan.get("poll_hash", "")
        edge_state["safe_hash"] = plan.get("safe_hash", "")
        edge_state["safe_interval"] = plan.get("safe_interval")
        edge_state["polls"] = [meta.__dict__ for meta in plan["poll_meta"]]
        edge_state["safehz"] = [meta.__dict__ for meta in plan["safe_meta"]]
        edge_state["mask_hash"] = plan.get("mask_hash", "")
        edge_state["ctrl_mask"] = [{"dev_id": dev, "flags": flags} for dev, flags in plan.get("mask_rows", [])]
        self._save_state()

    @staticmethod
    def _hash_obj(obj: Any) -> str:
        if not obj:
            return ""
        blob = json.dumps(obj, sort_keys=True).encode("utf-8")
        return hashlib.blake2s(blob, digest_size=16).hexdigest()

    def _edge_state(self, edge_id: str) -> Dict[str, Any]:
        return self._state.setdefault("edges", {}).setdefault(edge_id, {})

    def _assign_code_id(self, edge_id: str, dev_id: int, op: str) -> int:
        codes = self._state.setdefault("codes", {})
        edge_codes = codes.setdefault(edge_id, {})
        dev_codes = edge_codes.setdefault(str(dev_id), {})
        if op in dev_codes:
            return int(dev_codes[op])

        used = {int(v) for v in dev_codes.values()}
        for cid in range(1, 255):
            if cid not in used:
                dev_codes[op] = cid
                self._save_state()
                return cid
        raise HTTPException(status_code=500, detail="Exhausted code_id space for device")

    def ensure_code_id(self, edge_id: str, dev_id: int, op: str) -> int:
        """
        Return the existing code_id for (edge, dev, op) or allocate a new one.
        """
        codes = self._state.setdefault("codes", {})
        edge_codes = codes.setdefault(edge_id, {})
        dev_codes = edge_codes.setdefault(str(dev_id), {})
        if op in dev_codes:
            return int(dev_codes[op])
        return self._assign_code_id(edge_id, dev_id, op)

    def _edge_prefer(self, edge_cfg) -> str:
        prefer = (edge_cfg.prefer or "TCP").upper()
        if prefer == "TCP" and not self._tcp_available():
            prefer = "LORA"
        if prefer not in {"TCP", "LORA"}:
            raise HTTPException(status_code=400, detail="edge.prefer must be TCP or LORA")
        return prefer

    def _resolve_dev_eui(self, edge_cfg, prefer: str) -> Optional[str]:
        if prefer != "LORA":
            return None
        dev_eui = edge_cfg.dev_eui or self.settings.DEFAULT_DEV_EUI
        if not dev_eui:
            raise HTTPException(
                status_code=400,
                detail=f"Edge '{edge_cfg.id}' is configured for LoRa but lacks a DevEUI",
            )
        return dev_eui

    def _default_timeout_ms(self, prefer: str) -> int:
        if prefer == "LORA":
            return 8000
        tcp_timeout = int(max(self.settings.TCP_READ_TIMEOUT * 1000, 1500))
        return tcp_timeout

    def _tcp_available(self) -> bool:
        return bool(self.settings.EDGE_TCP_HOST and self.settings.EDGE_TCP_PORT)

    def _get_edge(self, edge_id: str):
        try:
            return self.site_cfg.get_edge(edge_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Edge '{edge_id}' not found") from exc

    def _select_vfds(
        self,
        *,
        edge_id: str,
        vfd_ids: Optional[Sequence[str]],
    ) -> Tuple[Any, List[VfdCfg]]:
        edge_cfg = self._get_edge(edge_id)
        selected: List[VfdCfg] = []
        seen_ids: set[int] = set()
        for v in self.site_cfg.vfds:
            include = v.edge_id == edge_id
            if not include and self.rolemap and getattr(v, "control_group", None):
                include = self.rolemap.is_member(edge_id, v)
            if not include:
                continue
            marker = id(v)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            selected.append(v)
        vfds = selected
        if not vfds:
            raise HTTPException(
                status_code=404,
                detail=f"No VFDs configured for edge '{edge_id}'",
            )
        if vfd_ids:
            requested = set(vfd_ids)
            vfds = [v for v in vfds if v.vfd_id in requested]
            missing = requested - {v.vfd_id for v in vfds}
            if missing:
                raise HTTPException(status_code=404, detail=f"Unknown VFDs for edge '{edge_id}': {sorted(missing)}")
        return edge_cfg, vfds

    def _load_profile(self, vfd: VfdCfg) -> Dict[str, Any]:
        try:
            return self.profiles.load(vfd.model)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Profile '{vfd.model}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Profile '{vfd.model}' invalid: {exc}") from exc

    def _load_state(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {"edges": {}}
        try:
            return json.loads(self.registry_path.read_text())
        except Exception as exc:
            logger.warning("Failed to load provision registry (%s); starting fresh", exc)
            return {"edges": {}}

    def _save_state(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.registry_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        tmp_path.replace(self.registry_path)
