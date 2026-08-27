"""One-shot LAN probe: auth / stream login / first bytes after start."""

from __future__ import annotations

import socket
import struct
import sys
import time

from v380.client.v380_client import (
    _ru16,
    _ru32,
    _u16,
    _u32,
    encrypt_password,
    recv_exact,
)


def hexdump(data: bytes, n: int = 64) -> str:
    return data[:n].hex(" ")


def main() -> int:
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.7"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8800
    device_id = int(sys.argv[3]) if len(sys.argv) > 3 else 80342739
    user = sys.argv[4] if len(sys.argv) > 4 else "80342739"
    password = sys.argv[5] if len(sys.argv) > 5 else ""

    print(f"TCP {ip}:{port} id={device_id} user={user}")
    s = socket.create_connection((ip, port), timeout=5)
    print("auth socket: connected")
    pkt = bytearray(520)
    pkt[0:4] = _u32(1167)
    pkt[4:8] = _u32(120)
    pkt[8] = 31
    pkt[9:13] = _u32(1)
    pkt[13:17] = _u32(device_id)
    ub = user.encode("ascii")[:32]
    pkt[49 : 49 + len(ub)] = ub
    pw = encrypt_password(password)
    pkt[81 : 81 + len(pw)] = pw
    s.sendall(pkt)
    resp = recv_exact(s, 256, timeout=8)
    print(f"auth recv {len(resp)} bytes  {hexdump(resp)}")
    if len(resp) < 16:
        print("FAIL auth short")
        return 1
    print(f"auth cmd={_ru32(resp, 0)} result={_ru32(resp, 4)} ver={resp[12]} ticket={_ru32(resp, 13)}")
    if _ru32(resp, 4) != 1001:
        print("FAIL auth")
        return 1
    ticket = _ru32(resp, 13)
    s.close()
    print("auth socket: closed")

    s = socket.create_connection((ip, port), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("stream socket: connected")
    pkt = bytearray(256)
    pkt[0:4] = _u32(301)
    pkt[4:8] = _u32(device_id)
    pkt[8:12] = _u32(0)
    pkt[12:14] = _u16(20)
    pkt[14:18] = _u32(ticket)
    pkt[22:26] = _u32(4097)
    pkt[26:30] = _u32(1)
    s.sendall(pkt)

    s.settimeout(3)
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < 3:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            print(f"stream login closed after {len(buf)} bytes")
            break
        buf.extend(chunk)
        if len(buf) >= 8 and _ru32(buf, 0) == 401 and len(buf) >= 32:
            # keep a short extra window for leftover
            s.settimeout(0.3)
            try:
                extra = s.recv(4096)
                if extra:
                    buf.extend(extra)
            except socket.timeout:
                pass
            break
    print(f"stream login recv {len(buf)} bytes  {hexdump(buf)}")
    if len(buf) >= 8:
        print(
            f"stream cmd={_ru32(buf, 0)} result={struct.unpack_from('<i', buf, 4)[0]} "
            f"comm={_ru16(buf, 8) if len(buf) >= 10 else '?'} "
            f"wh={_ru32(buf, 10) if len(buf) >= 18 else '?'}x{_ru32(buf, 14) if len(buf) >= 18 else '?'}"
        )

    pkt = bytearray(256)
    pkt[0:4] = _u32(303)
    pkt[4:6] = _u16(0x3001)
    s.sendall(pkt)
    print("sent start 303")

    s.settimeout(5)
    got = bytearray()
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            chunk = s.recv(8192)
        except socket.timeout:
            print("start: recv timeout")
            break
        if not chunk:
            print(f"start: peer closed after {len(got)} extra bytes")
            break
        got.extend(chunk)
        if len(got) >= 64:
            break
    print(f"after start recv {len(got)} bytes  {hexdump(got, 80)}")
    if got:
        print(f"first byte=0x{got[0]:02x}  0x7F count={got.count(0x7F)}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
