import asyncio
import pytest

from app.settings import Settings
from app.services import edge_link as edge_link_mod
from app.services.dispatcher import Dispatcher
from app.services.translator.profile_loader import ProfileRepo
from app.services.edge_link import parse_e2_msgid
from app.services.e2_setup import RID_REPORT, VID_ACK

pytestmark = pytest.mark.asyncio

PROFILES_DIR = Settings().PROFILES_DIR

def _extract_exec_body(frame: bytes) -> bytes:
    chunk_len = frame[8]
    chunk = frame[9 : 9 + chunk_len]
    assert len(chunk) == chunk_len
    return chunk


def _build_ack_from_frame(frame: bytes) -> bytes:
    msgid = int.from_bytes(frame[5:7], "little")
    ack = bytearray()
    ack += b"E2"
    ack.append(0x22)  # ack bit cleared, FINAL flag set
    ack.append(RID_REPORT)  # REPORT ACK
    ack.append(VID_ACK)
    ack += msgid.to_bytes(2, "little")
    ack.append(0x80)  # last fragment
    ack.append(0)     # zero-length body
    return bytes(ack)


async def test_set_hz_via_lora(monkeypatch):
    dev_eui = "0011223344556677"

    # deterministic msgid for assertions
    async def fake_next_msgid():
        return 0x2345

    monkeypatch.setattr(edge_link_mod, "next_msgid", fake_next_msgid)

    repo = ProfileRepo(PROFILES_DIR)
    profile = repo.load("huanyang-hy")
    tx_payload = repo.build(profile, "set_hz", {"hz": 30.0}, unit_id=1)

    dispatcher = Dispatcher(Settings(CS_FPORT=1))
    sent_frames: list[bytes] = []

    async def fake_send_lora_frame(settings, *, dev_eui: str, frame: bytes, fport: int, confirmed: bool) -> None:
        sent_frames.append(frame)
        # Simulate the uplink arriving shortly after downlink is queued.
        ack = _build_ack_from_frame(frame)
        loop = asyncio.get_running_loop()
        loop.call_soon(dispatcher.resolve_lora, dev_eui, ack)

    monkeypatch.setattr(edge_link_mod, "send_lora_frame", fake_send_lora_frame)

    route, rtt_ms, reply_frame = await dispatcher.exec_single(
        edge_id="edge-001",
        dev_eui=dev_eui,
        dev_id=1,
        code_id=1,
        tx=tx_payload,
        prefer="LORA",
        timeout_ms=1000,
    )

    assert route == "LORA"
    assert reply_frame.startswith(b"E2")
    assert reply_frame[8] == 0  # zero-length body in ack
    assert len(sent_frames) == 1

    frame = sent_frames[0]
    exec_body = _extract_exec_body(frame)
    assert exec_body[0] == 1  # one row
    assert exec_body[1] == 1  # dev_id
    assert exec_body[2] == 1  # code_id
    assert exec_body[3] == len(tx_payload)
    assert exec_body[4 : 4 + len(tx_payload)] == tx_payload

    # sanity: ack carried matching msgid
    mid = parse_e2_msgid(reply_frame)
    assert mid == 0x2345
