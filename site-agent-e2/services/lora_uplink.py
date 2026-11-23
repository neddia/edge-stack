# app/services/lora_uplink.py
import asyncio
import base64
import json
from binascii import Error as BinasciiError
from typing import Callable, Optional
from urllib.parse import urlparse

from app.util.logging import get_logger
from app.services.edge_link import update_edge_len_cap  # infer FRMPayload cap from uplink DR

log = get_logger(__name__)

try:
    import paho.mqtt.client as mqtt  # optional
except Exception:  # pragma: no cover
    mqtt = None

class LoRaUplink:
    def __init__(self, *, host: str, username: Optional[str], password: Optional[str], topics: list[str]):
        self.host, self.port = self._parse_host(host)
        self.username, self.password = username, password
        extra = [
            "application/+/device/+/event/log",
            "application/+/device/+/event/join",
        ]
        seen = []
        for t in (topics or []) + extra:
            if t and t not in seen:
                seen.append(t)
        self.topics = seen
        self.client = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._handler: Optional[Callable[[str, bytes], None]] = None
        self._event_handler: Optional[Callable[[str, dict], None]] = None

    async def start(self, handler: Callable[[str, bytes], None], *, event_handler: Optional[Callable[[str, dict], None]] = None):
        if mqtt is None:
            # If MQTT_HOST is configured but the dependency is missing, fail fast
            raise RuntimeError("paho-mqtt not installed; install paho-mqtt to enable LoRa uplink")
        self._handler = handler
        self._event_handler = event_handler
        await self._start_client()
        self._task = asyncio.create_task(self._consumer())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    async def _consumer(self):
        while True:
            item = await self.queue.get()
            try:
                topic = item["topic"]; payload = item["payload"]
                # Topic form: application/<app_id>/device/<dev_eui>/event/up
                dev_eui = self._extract_dev_eui(topic)
                parsed = self._maybe_parse_json(payload)
                if dev_eui and parsed is not None:
                    _update_cap_from_dr(dev_eui, parsed)
                if self._event_handler and isinstance(parsed, dict):
                    try:
                        self._event_handler(topic, parsed)
                    except Exception as exc:  # keep the consumer alive
                        log.warning("uplink event handler error: %s", exc)
                frame = self._decode_frame(payload, parsed=parsed)
                if dev_eui and frame and self._handler and topic.endswith("/event/up"):
                    self._handler(dev_eui, frame)  # push to dispatcher
            finally:
                self.queue.task_done()

    @staticmethod
    def _extract_dev_eui(topic: str) -> Optional[str]:
        parts = topic.split("/")
        try:
            i = parts.index("device")
            return parts[i+1]
        except Exception:
            return None

    @staticmethod
    def _parse_host(s: str) -> tuple[str,int]:
        if "://" in s:
            u = urlparse(s); return (u.hostname or "localhost", u.port or 1883)
        if ":" in s:
            h,p = s.rsplit(":",1); return (h, int(p))
        return (s, 1883)

    async def _start_client(self):
        client = mqtt.Client()
        if self.username:
            client.username_pw_set(self.username, self.password or "")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.host, self.port, 60)
        client.loop_start()
        self.client = client

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            for t in self.topics:
                if t: client.subscribe(t)
            log.info("LoRa uplink connected; topics=%s", ",".join(self.topics))
        else:
            log.error("LoRa uplink connect error rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        # Push raw uplink payload; consumer will decode frame
        try:
            self.queue.put_nowait({"topic": msg.topic, "payload": msg.payload or b""})
        except Exception as e:
            log.error("uplink queue err: %s", e)

    @staticmethod
    def _maybe_parse_json(payload: bytes) -> Optional[dict]:
        if not payload:
            return None
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _decode_frame(payload: bytes, *, parsed: Optional[dict] = None) -> Optional[bytes]:
        if not payload:
            return None
        parsed_json = parsed
        if parsed_json is None:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                # assume raw binary E2 frame already
                return payload

            try:
                parsed_json = json.loads(text)
            except json.JSONDecodeError:
                # Some setups dump hex directly
                return LoRaUplink._decode_hex_or_base64(text)

        if parsed_json is None:
            # assume raw binary E2 frame already
            return payload

        # ChirpStack exposes the decrypted application payload as `data`
        # (base64) when AppSKey is available. Prefer that if present.
        app_data = parsed_json.get("data") if isinstance(parsed_json, dict) else None
        if isinstance(app_data, str) and app_data:
            decoded = LoRaUplink._decode_hex_or_base64(app_data)
            if decoded:
                return decoded

        frm_payload = (
            parsed_json.get("phy_payload", {})
            .get("payload", {})
            .get("frm_payload")
        )
        if isinstance(frm_payload, str) and frm_payload:
            return LoRaUplink._decode_hex_or_base64(frm_payload)
        return None

    @staticmethod
    def _decode_hex_or_base64(s: str) -> Optional[bytes]:
        s = s.strip()
        if not s:
            return None
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass

        try:
            return base64.b64decode(s, validate=True)
        except (BinasciiError, ValueError):
            return None


def _update_cap_from_dr(dev_eui: str, msg: dict) -> None:
    """
    Infer FRMPayload cap from uplink DR and cache it so we chunk downlinks
    safely. Uses a coarse table provided by user (dr1=53, dr2=126, dr>=3=242).
    """
    try:
        dr = int(msg.get("dr"))
    except Exception:
        return
    if dr <= 1:
        cap = 53
    elif dr == 2:
        cap = 126
    else:
        cap = 242
    if dev_eui:
        update_edge_len_cap(dev_eui, cap)
