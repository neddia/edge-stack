# app/services/e2_setup.py
from fastapi import HTTPException

# E2 routing IDs / verbs
RID_SYS   = 0
RID_EXEC  = 1
RID_POLLS = 2
RID_BUS   = 3
RID_SAFEHZ= 4
RID_STATUS= 5
RID_REPORT= 6
RID_INFO  = 7
RID_CTRL  = 8

VID_SET = 1
VID_GET = 2
VID_DEL = 3
VID_RUN = 4
VID_ACK = 5

def _ensure_byte(v:int, label:str) -> int:
    if not 0 <= v <= 0xFF: raise HTTPException(400, f"{label} must be 0..255")
    return v

def _ensure_u16(v:int, label:str) -> int:
    if not 0 <= v <= 0xFFFF: raise HTTPException(400, f"{label} must be 0..65535")
    return v

def _clean_hex(s: str) -> bytes:
    s = ''.join(ch for ch in s if ch.strip()).replace(' ', '')
    if len(s) % 2: s = '0' + s
    try: return bytes.fromhex(s) if s else b''
    except ValueError as exc: raise HTTPException(400, f"Invalid hex: {exc}")


def encode_bundle(items: list[tuple[int, int, bytes]]) -> bytes:
    """
    Bundle multiple E2 commands into a SYS/VID=1 envelope.
    Layout: [rid][vid][len][payload...] repeated, where len is u8 (<0xFF) or
    0xFF + u16 big-endian for longer payloads.
    """
    body = bytearray()
    for rid, vid, payload in items:
        _ensure_byte(rid, "rid")
        _ensure_byte(vid, "vid")
        if len(payload) >= 0xFF:
            if len(payload) > 0xFFFF:
                raise HTTPException(400, "Bundle item exceeds 65535 bytes")
            body.extend([rid, vid, 0xFF])
            body.extend(len(payload).to_bytes(2, "big"))
        else:
            body.extend([rid, vid, len(payload)])
        body.extend(payload)
    return bytes(body)

def encode_exec(rows: list[tuple[int,int,bytes]]) -> bytes:
    # rows: [(dev_id, code_id, tx_bytes), ...]
    if not rows: raise HTTPException(400, "Provide at least one exec row")
    if len(rows) > 255: raise HTTPException(400, "Exec rows exceed 255")
    dev_ids=bytearray(); code_ids=bytearray(); tx_len=bytearray(); tx_concat=bytearray()
    for dev, code, tx in rows:
        _ensure_byte(dev,"dev_id"); _ensure_byte(code,"code_id")
        if len(tx)>255: raise HTTPException(400, "tx payload over 255 bytes")
        dev_ids.append(dev); code_ids.append(code); tx_len.append(len(tx)); tx_concat.extend(tx)
    return bytes([len(rows)]) + bytes(dev_ids) + bytes(code_ids) + bytes(tx_len) + bytes(tx_concat)

def encode_polls(rows: list[tuple[int,int,int,bytes]]) -> bytes:
    # rows: [(dev_id, code_id, period_s, tx_bytes), ...]
    if len(rows) > 255: raise HTTPException(400, "Poll rows exceed 255")
    dev_ids=bytearray(); code_ids=bytearray(); periods=bytearray(); tx_len=bytearray(); tx_concat=bytearray()
    for dev, code, period_s, tx in rows:
        _ensure_byte(dev,"dev_id"); _ensure_byte(code,"code_id"); _ensure_u16(period_s,"period_s")
        if len(tx)>255: raise HTTPException(400, "tx payload over 255 bytes")
        dev_ids.append(dev); code_ids.append(code); periods += period_s.to_bytes(2,'big')
        tx_len.append(len(tx)); tx_concat.extend(tx)
    return bytes([len(rows)]) + bytes(dev_ids) + bytes(code_ids) + bytes(periods) + bytes(tx_len) + bytes(tx_concat)

def encode_safehz(interval_s:int, rows:list[tuple[int,bytes]]) -> bytes:
    _ensure_u16(interval_s,"safe_interval")
    if len(rows) > 255: raise HTTPException(400, "SafeHz rows exceed 255")
    dev_ids=bytearray(); tx_len=bytearray(); tx_concat=bytearray()
    for dev, tx in rows:
        _ensure_byte(dev,"dev_id")
        if len(tx)>255: raise HTTPException(400, "tx payload over 255 bytes")
        dev_ids.append(dev); tx_len.append(len(tx)); tx_concat.extend(tx)
    body = interval_s.to_bytes(2,'big') + bytes([len(rows)])
    if rows: body += bytes(dev_ids) + bytes(tx_len) + bytes(tx_concat)
    return body

def encode_bus(baud:int, parity:int, stop_bits:int, turnaround:int, timeout:int, retries:int) -> bytes:
    _ensure_u16(turnaround,"turnaround"); _ensure_u16(timeout,"timeout")
    _ensure_byte(parity,"parity"); _ensure_byte(stop_bits,"stop_bits"); _ensure_byte(retries,"retries")
    return (
        int(baud).to_bytes(4,'big') + bytes([parity]) + bytes([stop_bits]) +
        int(turnaround).to_bytes(2,'big') + int(timeout).to_bytes(2,'big') + bytes([retries])
    )

def encode_info_dict(dev_rows: list[tuple[int,str]], code_rows: list[tuple[int,str]]) -> bytes:
    if len(dev_rows)>255 or len(code_rows)>255: raise HTTPException(400, "Dictionary rows exceed 255")
    b=bytearray(); b.append(len(dev_rows))
    for dev_id,name in dev_rows:
        _ensure_byte(dev_id,"dev_id")
        s=name[:31].encode('utf-8'); b += bytes([dev_id, len(s)]) + s
    b.append(len(code_rows))
    for code_id,name in code_rows:
        _ensure_byte(code_id,"code_id")
        s=name[:31].encode('utf-8'); b += bytes([code_id, len(s)]) + s
    return bytes(b)

def encode_ctrl_mask(rows: list[tuple[int,int]]) -> bytes:
    """
    rows: list of (dev_id, flags). flags: bit0=ALLOW_BOOT, bit1=ALLOW_SAFEHZ (reserved high bits).
    Row-wise layout to match firmware: count | (dev, flags)*N
    """
    if len(rows) > 255:
        raise HTTPException(400, "CTRL mask rows exceed 255")
    b = bytearray()
    b.append(len(rows))
    for dev, flg in rows:
        _ensure_byte(dev, "dev_id")
        _ensure_byte(flg, "flags")
        b.append(dev)
        b.append(flg)
    return bytes(b)
