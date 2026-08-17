"""Grab one JPEG from the camera to verify the fixed client."""

import sys
from pathlib import Path

from v380_client import V380SnapshotClient, h264_to_jpeg


def main() -> int:
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.7"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8800
    device_id = int(sys.argv[3]) if len(sys.argv) > 3 else 80342739
    user = sys.argv[4] if len(sys.argv) > 4 else "80342739"
    password = sys.argv[5] if len(sys.argv) > 5 else ""
    out = Path(__file__).with_name("last_snapshot.jpg")

    c = V380SnapshotClient(ip, device_id, user, password, port)
    try:
        c.connect()
        print(f"login OK ticket={c.auth_ticket} ver={c.device_version} comm={c.comm_version} {c.frame_width}x{c.frame_height}")
        iframe = c.read_iframe(timeout=20)
        print(f"I-frame {len(iframe)} bytes start={iframe[:8].hex()}")
        jpeg = h264_to_jpeg(c.h264_for_decode(iframe))
        out.write_bytes(jpeg)
        print(f"JPEG {len(jpeg)} bytes -> {out}")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
