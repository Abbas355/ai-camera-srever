"""Connect to a camera, count fragment types, decode one live JPEG."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from v380.client.v380_client import V380SnapshotClient, h264_to_jpeg


def main() -> int:
    if len(sys.argv) < 6:
        print("usage: verify_stream.py IP PORT DEVICE_ID USERNAME PASSWORD [seconds]")
        return 2
    ip = sys.argv[1]
    port = int(sys.argv[2])
    device_id = int(sys.argv[3])
    user = sys.argv[4]
    password = sys.argv[5]
    seconds = float(sys.argv[6]) if len(sys.argv) > 6 else 6.0
    out = Path(__file__).with_name("last_snapshot.jpg")

    c = V380SnapshotClient(ip, device_id, user, password, port)
    videos = 0
    iframe = None
    deadline = time.time() + seconds
    try:
        c.connect()
        print(
            f"login OK ticket={c.auth_ticket} deviceVersion={c.device_version} "
            f"comm={c.comm_version} {c.frame_width}x{c.frame_height}"
        )
        for kind, is_iframe, payload in c.iter_video_frames():
            if kind != "video":
                continue
            videos += 1
            if is_iframe and iframe is None:
                iframe = payload
                print(
                    f"I-frame {len(payload)} bytes codec={c.video_codec} "
                    f"start={payload[:8].hex()}"
                )
            if iframe is not None and time.time() >= deadline:
                break
            if time.time() >= deadline and videos >= 3:
                break
        print("fragment counts:", {f"0x{k:02X}": v for k, v in sorted(c.frame_stats.items())})
        print(f"assembled video frames={videos} codec={c.video_codec}")
        if not iframe:
            print("FAIL: no I-frame")
            return 1
        jpeg = h264_to_jpeg(c.h264_for_decode(iframe))
        out.write_bytes(jpeg)
        print(f"OK JPEG {len(jpeg)} bytes -> {out}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
