"""Minimal LevelDB access for Chromium's Local Storage (no dependencies).

Reading covers table files (optionally Snappy-compressed) and write-ahead logs.
Writing appends one batch to the newest log, which LevelDB replays on open, so
existing files are never rewritten. The database must not be open elsewhere.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

BLOCK = 32768


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def _put_varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _snappy(src: bytes) -> bytes:
    length, pos = _varint(src, 0)
    out = bytearray()
    while pos < len(src):
        tag = src[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(src[pos : pos + extra], "little")
                pos += extra
            out += src[pos : pos + n + 1]
            pos += n + 1
            continue
        if kind == 1:
            n, off = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | src[pos]
            pos += 1
        elif kind == 2:
            n, off = (tag >> 2) + 1, int.from_bytes(src[pos : pos + 2], "little")
            pos += 2
        else:
            n, off = (tag >> 2) + 1, int.from_bytes(src[pos : pos + 4], "little")
            pos += 4
        for _ in range(n):
            out.append(out[-off])
    if len(out) != length:
        raise ValueError("corrupt snappy block")
    return bytes(out)


def _block(data: bytes, offset: int, size: int) -> bytes:
    raw = data[offset : offset + size]
    return _snappy(raw) if data[offset + size] == 1 else raw


def _block_entries(block: bytes):
    restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(block, pos)
        non_shared, pos = _varint(block, pos)
        vlen, pos = _varint(block, pos)
        key = key[:shared] + block[pos : pos + non_shared]
        pos += non_shared
        yield key, block[pos : pos + vlen]
        pos += vlen


def _table_entries(path: Path):
    data = path.read_bytes()
    footer = data[-48:]
    _, p = _varint(footer, 0)
    _, p = _varint(footer, p)
    idx_off, p = _varint(footer, p)
    idx_size, _ = _varint(footer, p)
    for _, handle in _block_entries(_block(data, idx_off, idx_size)):
        off, q = _varint(handle, 0)
        size, _ = _varint(handle, q)
        for ikey, value in _block_entries(_block(data, off, size)):
            tag = int.from_bytes(ikey[-8:], "little")
            yield ikey[:-8], tag >> 8, tag & 0xFF, value


def _log_records(path: Path):
    data = path.read_bytes()
    pos, pending = 0, b""
    while pos + 7 <= len(data):
        left = BLOCK - pos % BLOCK
        if left < 7:
            pos += left
            continue
        length = int.from_bytes(data[pos + 4 : pos + 6], "little")
        rtype = data[pos + 6]
        payload = data[pos + 7 : pos + 7 + length]
        pos += 7 + length
        if rtype == 0:
            break
        if rtype == 1:
            yield payload
        elif rtype == 2:
            pending = payload
        elif rtype == 3:
            pending += payload
        elif rtype == 4:
            yield pending + payload
            pending = b""


def _batch_entries(batch: bytes):
    seq = int.from_bytes(batch[:8], "little")
    count = int.from_bytes(batch[8:12], "little")
    pos = 12
    for i in range(count):
        kind = batch[pos]
        klen, pos = _varint(batch, pos + 1)
        key = batch[pos : pos + klen]
        pos += klen
        value = b""
        if kind == 1:
            vlen, pos = _varint(batch, pos)
            value = batch[pos : pos + vlen]
            pos += vlen
        yield key, seq + i, kind, value


def _manifest_last_sequence(path: Path) -> int:
    last = 0
    for rec in _log_records(path):
        pos = 0
        while pos < len(rec):
            tag, pos = _varint(rec, pos)
            if tag == 1:
                n, pos = _varint(rec, pos)
                pos += n
            elif tag in (2, 3, 4, 9):
                v, pos = _varint(rec, pos)
                if tag == 4:
                    last = max(last, v)
            elif tag == 5:
                _, pos = _varint(rec, pos)
                n, pos = _varint(rec, pos)
                pos += n
            elif tag == 6:
                _, pos = _varint(rec, pos)
                _, pos = _varint(rec, pos)
            elif tag == 7:
                for _ in range(3):
                    _, pos = _varint(rec, pos)
                for _ in range(2):
                    n, pos = _varint(rec, pos)
                    pos += n
            else:
                raise ValueError(f"unknown manifest tag {tag}")
    return last


def load(db: Path) -> tuple[dict[bytes, bytes], int]:
    """Live key/value pairs and the highest sequence number in use."""
    best: dict[bytes, tuple[int, int, bytes]] = {}
    max_seq = 0

    def put(key: bytes, seq: int, kind: int, value: bytes) -> None:
        nonlocal max_seq
        max_seq = max(max_seq, seq)
        if key not in best or best[key][0] < seq:
            best[key] = (seq, kind, value)

    for f in sorted(db.glob("*.ldb")) + sorted(db.glob("*.sst")):
        for entry in _table_entries(f):
            put(*entry)
    for f in sorted(db.glob("*.log")):
        for batch in _log_records(f):
            for entry in _batch_entries(batch):
                put(*entry)
    current = db / "CURRENT"
    if current.exists():
        max_seq = max(max_seq, _manifest_last_sequence(db / current.read_text().strip()))
    return {k: v for k, (_, kind, v) in best.items() if kind == 1}, max_seq


_CRC = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x82F63B78 if _c & 1 else _c >> 1
    _CRC.append(_c)


def _crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _CRC[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _masked_crc(data: bytes) -> int:
    c = _crc32c(data)
    return (((c >> 15) | (c << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def put(db: Path, items: dict[bytes, bytes]) -> None:
    """Append one batch setting ``items``; LevelDB applies it on the next open."""
    _, max_seq = load(db)
    batch = bytearray((max_seq + 1).to_bytes(8, "little") + len(items).to_bytes(4, "little"))
    for key, value in items.items():
        batch += b"\x01" + _put_varint(len(key)) + key + _put_varint(len(value)) + value
    logs = sorted(db.glob("*.log"))
    if not logs:
        raise ValueError(f"{db}: no write-ahead log to append to")
    log = logs[-1]
    offset = log.stat().st_size
    out = bytearray()
    payload, first = bytes(batch), True
    while True:
        left = BLOCK - offset % BLOCK
        if left < 7:
            out += b"\x00" * left
            offset += left
            left = BLOCK
        chunk, payload = payload[: left - 7], payload[left - 7 :]
        last = not payload
        rtype = 1 if first and last else 2 if first else 4 if last else 3
        out += _masked_crc(bytes([rtype]) + chunk).to_bytes(4, "little")
        out += len(chunk).to_bytes(2, "little") + bytes([rtype]) + chunk
        offset += 7 + len(chunk)
        first = False
        if last:
            break
    with log.open("ab") as f:
        f.write(out)
        f.flush()
        os.fsync(f.fileno())


def in_use(db: Path) -> bool:
    """Whether another process holds the database lock (e.g. a running browser)."""
    lock = db / "LOCK"
    if not lock.exists():
        return False
    try:
        if os.name == "nt":
            import msvcrt

            with lock.open("r+b") as f:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with lock.open("r+b") as f:
                fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(f, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


# Chromium's Local Storage encoding -----------------------------------------------


def local_storage_key(origin: str, name: str) -> bytes:
    return b"_" + origin.encode() + b"\x00\x01" + name.encode("latin-1")


def decode_local_storage(value: bytes) -> str:
    return value[1:].decode("utf-16-le") if value[:1] == b"\x00" else value[1:].decode("latin-1")


def encode_local_storage(text: str) -> bytes:
    try:
        return b"\x01" + text.encode("latin-1")
    except UnicodeEncodeError:
        return b"\x00" + text.encode("utf-16-le")
