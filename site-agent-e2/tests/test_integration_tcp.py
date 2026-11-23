import asyncio
import socket
import pytest

from app.settings import Settings
from app.services.dispatcher import Dispatcher
from app.services.translator.profile_loader import ProfileRepo
from app.services.e2_setup import RID_EXEC, RID_REPORT, VID_ACK, VID_DEL, VID_RUN, VID_SET

PROFILES_DIR = Settings().PROFILES_DIR

pytestmark = pytest.mark.asyncio


def _extract_exec_body(frame: bytes) -> bytes:
    chunk_len = frame[8]
    chunk = frame[9 : 9 + chunk_len]
    assert len(chunk) == chunk_len
    return chunk


def _build_ack_from_frame(frame: bytes, *, rid: int = RID_REPORT, vid: int = VID_ACK) -> bytes:
    msgid = int.from_bytes(frame[5:7], "little")
    ack = bytearray()
    ack += b"E2"
    ack.append(0x22)  # ack bit cleared, FINAL flag set
    ack.append(rid)
    ack.append(vid)
    ack += msgid.to_bytes(2, "little")
    ack.append(0x80)  # last fragment
    ack.append(0)     # empty body
    return bytes(ack)


async def _tcp_echo_server(host: str, port: int, captured: list[bytes], *, ack_rid: int = RID_REPORT, ack_vid: int = VID_ACK):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                hdr = await reader.readexactly(2)
                length = int.from_bytes(hdr, "big")
                payload = await reader.readexactly(length) if length else b""
                if payload:
                    captured.append(payload)
                    ack = _build_ack_from_frame(payload, rid=ack_rid, vid=ack_vid)
                    writer.write(len(ack).to_bytes(2, "big") + ack)
                    await writer.drain()
                break
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


async def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

async def test_set_hz_via_tcp():
    port = await _find_free_port()
    captured: list[bytes] = []
    server = await _tcp_echo_server("127.0.0.1", port, captured)

    try:
        settings = Settings(
            EDGE_TCP_HOST="127.0.0.1",
            EDGE_TCP_PORT=port,
            TCP_READ_TIMEOUT=0.5,
        )
        dispatcher = Dispatcher(settings)

        repo = ProfileRepo(PROFILES_DIR)
        profile = repo.load("huanyang-hy")
        tx_payload = repo.build(profile, "set_hz", {"hz": 40.0}, unit_id=1)

        route, rtt_ms, reply_frame = await dispatcher.exec_single(
            edge_id="edge-001",
            dev_eui=None,
            dev_id=1,
            code_id=1,
            tx=tx_payload,
            prefer="TCP",
            timeout_ms=1000,
        )

        assert route == "TCP"
        assert reply_frame.startswith(b"E2")
        assert reply_frame[8] == 0  # zero-length body
        assert len(captured) == 1
        frame = captured[0]
        exec_body = _extract_exec_body(frame)
        assert exec_body[0] == 1
        assert exec_body[1] == 1
        assert exec_body[2] == 1
        assert exec_body[3] == len(tx_payload)
        assert exec_body[4 : 4 + len(tx_payload)] == tx_payload
    finally:
        server.close()
        await server.wait_closed()


async def test_exec_set_persists_via_tcp():
    port = await _find_free_port()
    captured: list[bytes] = []
    server = await _tcp_echo_server("127.0.0.1", port, captured, ack_rid=RID_EXEC)

    try:
        settings = Settings(
            EDGE_TCP_HOST="127.0.0.1",
            EDGE_TCP_PORT=port,
            TCP_READ_TIMEOUT=0.5,
        )
        dispatcher = Dispatcher(settings)

        repo = ProfileRepo(PROFILES_DIR)
        profile = repo.load("huanyang-hy")
        tx_payload = repo.build(profile, "set_hz", {"hz": 33.0}, unit_id=1)

        route, _rtt_ms, reply_frame = await dispatcher.exec_single(
            edge_id="edge-001",
            dev_eui=None,
            dev_id=1,
            code_id=1,
            tx=tx_payload,
            exec_mode="set",
            prefer="TCP",
            timeout_ms=1000,
        )

        assert route == "TCP"
        assert reply_frame.startswith(b"E2")
        assert reply_frame[3] == RID_EXEC
        assert reply_frame[4] == VID_ACK
        assert len(captured) == 1

        frame = captured[0]
        assert frame[3] == RID_EXEC
        assert frame[4] == VID_SET
        exec_body = _extract_exec_body(frame)
        assert exec_body[0] == 1  # rows
        assert exec_body[1] == 1  # dev_id
        assert exec_body[2] == 1  # code_id
        assert exec_body[3] == len(tx_payload)
        assert exec_body[4 : 4 + len(tx_payload)] == tx_payload
    finally:
        server.close()
        await server.wait_closed()


async def test_exec_del_clears_via_tcp():
    port = await _find_free_port()
    captured: list[bytes] = []
    server = await _tcp_echo_server("127.0.0.1", port, captured, ack_rid=RID_EXEC)

    try:
        settings = Settings(
            EDGE_TCP_HOST="127.0.0.1",
            EDGE_TCP_PORT=port,
            TCP_READ_TIMEOUT=0.5,
        )
        dispatcher = Dispatcher(settings)

        route, _rtt_ms, reply_frame = await dispatcher.exec_single(
            edge_id="edge-001",
            dev_eui=None,
            dev_id=1,
            code_id=1,
            tx=None,
            exec_mode="del",
            prefer="TCP",
            timeout_ms=1000,
        )

        assert route == "TCP"
        assert reply_frame.startswith(b"E2")
        assert reply_frame[3] == RID_EXEC
        assert reply_frame[4] == VID_ACK

        assert len(captured) == 1
        frame = captured[0]
        assert frame[3] == RID_EXEC
        assert frame[4] == VID_DEL
        assert frame[8] == 0  # zero-length body
    finally:
        server.close()
        await server.wait_closed()
