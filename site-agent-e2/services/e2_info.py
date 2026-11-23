from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["InfoAckPayload", "parse_info_body"]


@dataclass
class InfoAckPayload:
    """
    Structured view of the RID_INFO / VID_ACK payload that contains the
    Edge's DevEUI and Ethernet MAC fingerprint.
    """

    dev_len: int = 0
    mac_len: int = 0
    dev_bytes: Optional[bytes] = None
    mac_bytes: Optional[bytes] = None
    truncated: bool = False
    invalid_mac_len: bool = False

    @property
    def dev_hex(self) -> Optional[str]:
        return self.dev_bytes.hex() if self.dev_bytes else None

    @property
    def mac_hex(self) -> Optional[str]:
        return self.mac_bytes.hex() if self.mac_bytes else None

    @property
    def is_valid(self) -> bool:
        return not self.truncated and not self.invalid_mac_len


def parse_info_body(body: bytes) -> InfoAckPayload:
    """
    Decode the INFO/ACK payload layout:
        [len_dev_eui][dev_eui bytes][len_mac][mac bytes]
    Both lengths are unsigned bytes. DevEUI is optional (len=0). MAC must be
    either 0 (missing) or 6 (fixed Ethernet MAC).
    """

    idx = 0
    total = len(body)
    if total < 1:
        return InfoAckPayload(truncated=True)

    dev_len = body[idx]
    idx += 1
    if total < idx + dev_len:
        # Not enough bytes to satisfy claimed DevEUI length.
        dev_slice = body[idx:total]
        return InfoAckPayload(
            dev_len=dev_len,
            dev_bytes=dev_slice or None,
            truncated=True,
        )

    dev_bytes = body[idx : idx + dev_len] if dev_len else b""
    idx += dev_len

    if total < idx + 1:
        # Missing MAC length byte.
        return InfoAckPayload(
            dev_len=dev_len,
            dev_bytes=dev_bytes or None,
            truncated=True,
        )

    mac_len = body[idx]
    idx += 1
    if total < idx + mac_len:
        # Truncated MAC bytes.
        mac_slice = body[idx:total]
        return InfoAckPayload(
            dev_len=dev_len,
            mac_len=mac_len,
            dev_bytes=dev_bytes or None,
            mac_bytes=mac_slice or None,
            truncated=True,
        )

    mac_bytes = body[idx : idx + mac_len] if mac_len else b""
    invalid_mac_len = mac_len not in (0, 6)

    return InfoAckPayload(
        dev_len=dev_len,
        mac_len=mac_len,
        dev_bytes=dev_bytes or None,
        mac_bytes=None if invalid_mac_len else (mac_bytes or None),
        invalid_mac_len=invalid_mac_len,
        truncated=False,
    )
