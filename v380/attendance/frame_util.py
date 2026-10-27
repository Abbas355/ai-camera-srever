"""Frame helpers for attendance live UI / matching."""

from __future__ import annotations


def normalize_attendance_frame(jpeg: bytes) -> bytes:
    """If the JPEG is a stacked dual-lens frame, keep the lower half.

    Some EZVIZ models deliver two landscape views in one tall image. Face check
    and the enroll preview then look "wrong" / empty. The lower pane is usually
    the eye-level view useful for faces.
    """
    if not jpeg or len(jpeg) < 100:
        return jpeg
    try:
        import cv2
        import numpy as np
    except ImportError:
        return jpeg
    try:
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return jpeg
        h, w = int(img.shape[0]), int(img.shape[1])
        if h < 240 or w < 240:
            return jpeg
        ratio = h / float(w)
        # Two ~16:9 panes stacked → height/width ≈ 1.125–1.3 (not phone portrait).
        if 1.05 <= ratio <= 1.50:
            half = h // 2
            img = img[half:h, 0:w]
            ok, buf = cv2.imencode(
                ".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok:
                return bytes(buf)
        return jpeg
    except Exception:
        return jpeg
