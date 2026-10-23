"""Optional image preprocessing for Detect Lab (never mutates the original bytes)."""

from __future__ import annotations

from typing import Any

import numpy as np


def decode_image(data: bytes) -> tuple[np.ndarray | None, str | None]:
    """Decode JPEG/PNG bytes → RGB uint8 array."""
    if not data:
        return None, "empty image"
    try:
        import cv2
    except Exception as exc:
        return None, f"cv2 missing: {exc}"
    arr = np.frombuffer(data, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        return None, "unsupported or corrupt image"
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    return rgb, None


def encode_jpeg(rgb: np.ndarray, *, quality: int = 90) -> bytes:
    import cv2

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return b""
    return bytes(buf)


def apply_preprocess(
    rgb: np.ndarray,
    *,
    enabled: bool = False,
    brightness: float = 0.0,
    contrast: float = 1.0,
    crop_norm: list[float] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return processed RGB + meta. brightness ∈ [-1,1], contrast ∈ [0.2,3].

    crop_norm: [x1,y1,x2,y2] in 0..1 relative to original. Empty/invalid → no crop.
    """
    meta: dict[str, Any] = {
        "enabled": bool(enabled),
        "brightness": 0.0,
        "contrast": 1.0,
        "crop_norm": None,
        "out_w": int(rgb.shape[1]),
        "out_h": int(rgb.shape[0]),
    }
    if not enabled:
        return rgb, meta

    out = rgb
    # Crop first (ROI on original coordinates).
    if crop_norm and len(crop_norm) >= 4:
        try:
            x1, y1, x2, y2 = [float(v) for v in crop_norm[:4]]
            x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
            y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
            if x2 - x1 >= 0.05 and y2 - y1 >= 0.05:
                h, w = int(out.shape[0]), int(out.shape[1])
                px1 = int(x1 * w)
                py1 = int(y1 * h)
                px2 = max(px1 + 1, int(x2 * w))
                py2 = max(py1 + 1, int(y2 * h))
                out = out[py1:py2, px1:px2].copy()
                meta["crop_norm"] = [x1, y1, x2, y2]
        except (TypeError, ValueError):
            pass

    b = max(-1.0, min(1.0, float(brightness)))
    c = max(0.2, min(3.0, float(contrast)))
    meta["brightness"] = b
    meta["contrast"] = c
    if abs(b) > 1e-6 or abs(c - 1.0) > 1e-6:
        # contrast around mid-gray, then brightness offset in [-255,255] scale
        img = out.astype(np.float32)
        img = (img - 127.5) * c + 127.5 + (b * 127.5)
        out = np.clip(img, 0, 255).astype(np.uint8)

    meta["out_w"] = int(out.shape[1])
    meta["out_h"] = int(out.shape[0])
    return out, meta
