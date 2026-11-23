import asyncio
import json
from pathlib import Path

import pytest

from app.settings import Settings
from app.services.dispatcher import Dispatcher
from app.services.e2_setup import RID_STATUS, VID_GET

pytestmark = pytest.mark.asyncio

EDGE_IPS_PATH = Path("storage/edge-ips.json")


def _load_edge_target(edge_id: str) -> tuple[str, int]:
    try:
        raw = json.loads(EDGE_IPS_PATH.read_text())
    except FileNotFoundError:
        pytest.skip(f"{EDGE_IPS_PATH} missing; cannot resolve TCP target for {edge_id}")
    except json.JSONDecodeError as exc:
        pytest.skip(f"{EDGE_IPS_PATH} invalid JSON: {exc}")
    record = raw.get(edge_id)
    if not isinstance(record, dict):
        pytest.skip(f"No record for {edge_id} in {EDGE_IPS_PATH}")
    host = (record.get("ip") or "").strip()
    port = int(record.get("port") or 0)
    if not host or port <= 0:
        pytest.skip(f"{edge_id} missing ip/port in {EDGE_IPS_PATH}")
    return host, port


@pytest.mark.live
async def test_live_tcp_status_ack():
    """
    Sends a small E2 STATUS/GET with ack over TCP to your Edge101 bridge.
    Expects an E2 reply frame (any payload) within TCP_READ_TIMEOUT.
    """
    host, port = _load_edge_target("edge-001")

    # Skip early if the target isn't reachable (keeps CI/local runs from failing)
    import socket
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError as exc:
        pytest.skip(f"edge TCP target {host}:{port} unreachable: {exc}")

    settings = Settings(EDGE_TCP_HOST=host, EDGE_TCP_PORT=port)
    d = Dispatcher(settings)

    route, rtt_ms, replies = await d.send_raw(
        edge_id="edge-001",
        dev_eui=None,
        rid=RID_STATUS,        # safe, read-only
        vid=VID_GET,
        body=b"",              # no body for status
        ack=True,              # request ack so Edge responds even if no bus
        prefer="TCP",
        timeout_ms=int((settings.TCP_READ_TIMEOUT + 0.5) * 1000),
        cap=settings.LORA_CHUNK_CAP,
        read_timeout=settings.TCP_READ_TIMEOUT,
    )

    assert route == "TCP"
    assert len(replies) >= 1
    assert replies[0].startswith(b"E2")  # E2 header present
