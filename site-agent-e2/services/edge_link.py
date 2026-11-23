# app/services/edge_link.py
import asyncio
import socket
from typing import Dict, List, Optional, Tuple

from app.services.e2_setup import RID_EXEC, VID_RUN, encode_exec
from app.settings import Settings
from app.util.logging import get_logger

# ---------- LoRa MTU helpers / cached DR caps ----------

_cap_cache: Dict[str, int] = {}          # dev_eui -> max FRMPayload bytes
_DEFAULT_FRMP_FALLBACK = 200             # conservative total payload if unknown
_DEFAULT_HEADROOM = 12                   # reserve for MAC cmds / FOpts


def update_edge_len_cap(dev_eui: str, frmp_cap: int) -> None:
    _cap_cache[dev_eui] = max(1, min(int(frmp_cap), 255))


def _e2_header_len() -> int:
    # "E2" + vf + rid + vid + msgid(le) + frag + len
    return 9


def get_lorawan_body_cap(*, dev_eui: str, settings: Settings, headroom: Optional[int] = None) -> int:
    """
    Calculate the E2 body capacity for a single fragment given the current DR.
    Falls back to settings.LORA_CHUNK_CAP (treated as existing body-cap) when
    no cached FRMPayload cap is available.
    """
    frmp_cap = _cap_cache.get(dev_eui)
    if frmp_cap is None:
        # Treat configured chunk cap as body-cap fallback for compatibility.
        body_cap = int(getattr(settings, "LORA_CHUNK_CAP", _DEFAULT_FRMP_FALLBACK))
        frmp_cap = body_cap + _DEFAULT_HEADROOM + _e2_header_len()

    h = _DEFAULT_HEADROOM if headroom is None else max(0, int(headroom))
    usable = max(1, frmp_cap - h - _e2_header_len())
    return max(1, min(usable, 255))


async def send_exec_lora(
    settings: Settings,
    *,
    dev_eui: str,
    rows: List[Tuple[int, int, bytes]],
    fport: Optional[int] = None,
    confirmed: bool = True,
    ack_req: bool = True,
    headroom: Optional[int] = None,
) -> int:
    """
    Build a columnar EXEC body for all rows and fragment according to the LoRa cap.
    Returns the msgid used for the transmission so callers can correlate acks.
    """
    if not rows:
        return 0

    body = encode_exec(rows)
    msgid = await next_msgid()
    cap = get_lorawan_body_cap(dev_eui=dev_eui, settings=settings, headroom=headroom)
    frames = build_e2_frames(rid=RID_EXEC, vid=VID_RUN, body=body, ack=ack_req, cap=cap, msgid=msgid)
    port = int(fport if fport is not None else getattr(settings, "CS_FPORT", 1))
    for fr in frames:
        await send_lora_frame(
            settings,
            dev_eui=dev_eui,
            frame=fr,
            fport=port,
            confirmed=confirmed,
        )
    return msgid

# ---------- E2 header / chunking ----------

class E2:
    # vf flags: bit5 fixed 1 (0x20), bit0 ack-req
    MAGIC = b"E2"

_msgid = 1
_msgid_lock = asyncio.Lock()

async def next_msgid() -> int:
    global _msgid
    async with _msgid_lock:
        _msgid = (_msgid + 1) & 0xFFFF or 1
        return _msgid

def build_e2_frames(*, rid: int, vid: int, body: bytes, ack: bool, cap: int, msgid: int) -> List[bytes]:
    cap = max(1, min(cap, 255))
    frames: List[bytes] = []
    off = 0
    idx = 0
    while True:
        chunk = body[off: off + cap]
        last = (off + len(chunk)) >= len(body)
        frag = (idx & 0x7F) | (0x80 if last else 0x00)
        vf = 0x20 | (0x01 if ack else 0x00)
        header = bytearray()
        header += E2.MAGIC
        header.append(vf)
        header.append(rid & 0xFF)
        header.append(vid & 0xFF)
        header += msgid.to_bytes(2, "little")
        header.append(frag)
        header.append(len(chunk))
        frames.append(bytes(header + chunk))
        if last:
            break
        off += len(chunk)
        idx += 1
    return frames

def parse_e2_msgid(payload: bytes) -> Optional[int]:
    # Layout: E2 | vf | rid | vid | msgid(le) | frag | len | ...
    if len(payload) < 9 or payload[:2] != E2.MAGIC:
        return None
    return int.from_bytes(payload[5:7], "little")

# ---------- TCP transport (length-prefixed multiplex) ----------

async def send_tcp_frames(
    settings: Settings,
    frames: List[bytes],
    read_timeout: float,
    *,
    expected: Optional[set[tuple[int, int]]] = None,
    expected_counts: Optional[dict[tuple[int, int], int]] = None,
    msgid: Optional[int] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> List[bytes]:
    host = host or settings.EDGE_TCP_HOST
    port = int(port or settings.EDGE_TCP_PORT)
    if not host:
        raise RuntimeError("TCP host not configured")

    def _send() -> List[bytes]:
        responses: List[bytes] = []
        matched_pairs: set[tuple[int, int]] = set()
        matched_counts: dict[tuple[int, int], int] = {}
        expect_counts = expected_counts or ({k: 1 for k in expected} if expected else {})
        with socket.create_connection((host, port), timeout=3) as sock:
            for fr in frames:
                sock.sendall(len(fr).to_bytes(2, "big") + fr)
            if read_timeout <= 0:
                return responses
            import time as _time  # local import to avoid module-level cost
            deadline = _time.monotonic() + read_timeout
            while True:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(max(remaining, 0))
                try:
                    header = _recv_exact(sock, 2)
                except socket.timeout:
                    continue
                if not header:
                    break
                length = int.from_bytes(header, "big")
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    payload = _recv_exact(sock, length) if length else b""
                except socket.timeout:
                    continue
                if payload is None:
                    break
                responses.append(payload)
                rid_vid, is_final = _frame_matches_expected(payload, expected or set(expect_counts.keys()), msgid)
                if rid_vid and is_final:
                    matched_pairs.add(rid_vid)
                    if expect_counts:
                        matched_counts[rid_vid] = matched_counts.get(rid_vid, 0) + 1
                        done = all(matched_counts.get(k, 0) >= expect_counts.get(k, 0) for k in expect_counts)
                        if done:
                            break
                    elif expected and expected.issubset(matched_pairs):
                        break
        if expect_counts:
            done = all(matched_counts.get(k, 0) >= expect_counts.get(k, 0) for k in expect_counts)
            if not done:
                raise TimeoutError("expected reply not received before TCP deadline")
        elif expected and msgid is not None and not expected.issubset(matched_pairs):
            raise TimeoutError("expected reply not received before TCP deadline")
        return responses

    return await asyncio.to_thread(_send)


# app/services/edge_link.py
def _frame_matches_expected(payload, expected, msgid):
    if not expected or msgid is None:
        return (None, False)
    if len(payload) < 9 or payload[:2] != b"E2":
        return (None, False)
    rid = payload[3]; vid = payload[4]
    if (rid, vid) not in expected:
        return (None, False)
    if int.from_bytes(payload[5:7], "little") != msgid:
        return (None, False)
    vf = payload[2]
    frag = payload[7]
    is_final = bool(vf & 0x02 or (frag & 0x80))
    return ((rid, vid), is_final)



def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

# ---------- LoRa downlink (ChirpStack gRPC) ----------

try:
    from chirpstack_api import api as chirp_api  # optional
except Exception:  # pragma: no cover
    chirp_api = None

log = get_logger(__name__)


async def send_lora_frame(settings: Settings, *, dev_eui: str, frame: bytes, fport: int, confirmed: bool) -> None:
    if chirp_api is None:
        raise RuntimeError("chirpstack_api not installed")
    server = settings.CS_GRPC_SERVER
    token = settings.CS_GRPC_TOKEN
    if not server or not token:
        raise RuntimeError("Configure CS_GRPC_SERVER and CS_GRPC_TOKEN")

    import grpc  # lazy import
    channel = grpc.insecure_channel(server)
    client = chirp_api.DeviceServiceStub(channel)
    metadata = [("authorization", f"Bearer {token}")]
    req = chirp_api.EnqueueDeviceQueueItemRequest()
    req.queue_item.dev_eui = dev_eui
    req.queue_item.f_port = int(fport)
    req.queue_item.confirmed = bool(confirmed)
    req.queue_item.data = frame
    msgid = int.from_bytes(frame[5:7], "little") if len(frame) >= 7 else None
    log.debug(
        "LoRa downlink dev_eui=%s fport=%s confirmed=%s msgid=%s frame=%s",
        dev_eui,
        fport,
        confirmed,
        f"0x{msgid:04x}" if msgid is not None else "n/a",
        frame.hex(),
    )
    await asyncio.to_thread(client.Enqueue, req, metadata=metadata)
