"""EZVIZ 360° panorama + tap-to-look (same idea as the phone app).

Uses pyezviz:
  create_panoramic / return_panoramic  → build & fetch 360 tiles
  ptz_control_coordinates(x, y)       → look at normalized 0..1 point

Tiles arrive as hikencodepicture blobs — decrypt with the camera
verification code (same password used for RTSP), then stitch left→right.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from v380.client import ezviz_session as ez
from v380.paths import DATA_DIR

PANO_CACHE_DIR = DATA_DIR / "panorama_cache"


@dataclass
class PanoramaResult:
    image: Image.Image | None
    urls: list[str]
    raw: Any
    message: str = ""


@dataclass
class StitchMeta:
    """How tiles were joined — used to map a map tap to EZVIZ aim coordinates."""

    tile_count: int
    tile_w: int
    overlap: int
    visual_w: int
    visual_h: int


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _visual_to_api(x: float, y: float, meta: StitchMeta | None) -> tuple[float, float]:
    """Map a point on our stitched strip to EZVIZ equal-tile panorama coordinates.

    Overlap stitching shortens the strip. Sending the raw tap x aims left of the
    clicked spot — convert through tile index so Go centers the live view on it.
    """
    x = _clamp01(x)
    y = _clamp01(y)
    if meta is None or meta.tile_count <= 1 or meta.visual_w <= 0 or meta.tile_w <= 0:
        return x, y
    n = int(meta.tile_count)
    w = int(meta.tile_w)
    ov = max(0, min(int(meta.overlap), w - 1))
    stride = max(1, w - ov)
    p = x * float(meta.visual_w)
    starts = [0.0]
    for k in range(1, n):
        starts.append(float(w - ov + (k - 1) * stride))
    tile = 0
    for i in range(n):
        if starts[i] <= p:
            tile = i
    if tile == 0:
        local = min(max(p, 0.0), float(w) - 1e-6)
    else:
        local = float(ov) + (p - starts[tile])
        local = min(max(local, 0.0), float(w) - 1e-6)
    api_x = (tile + local / float(w)) / float(n)
    return _clamp01(api_x), y


def _password_variants(password: str) -> list[str]:
    raw = (password or "").strip()
    out: list[str] = []
    for cand in (raw, raw.upper(), raw.lower()):
        if cand and cand not in out:
            out.append(cand)
    return out or [""]


def create_panorama(ip: str) -> dict:
    """Ask the camera to start building a panoramic image (can take ~30–90s)."""
    serial = ez.resolve_serial(ip)
    client = ez.get_client()
    return client.create_panoramic(serial)


def fetch_panorama(ip: str, *, password: str = "", wait_sec: float = 150.0) -> PanoramaResult:
    """Poll until panoramic tiles are ready, decrypt, and stitch into one image."""
    serial = ez.resolve_serial(ip)
    client = ez.get_client()
    deadline = time.time() + max(10.0, float(wait_sec))
    last: Any = None
    last_progress = -1

    while time.time() < deadline:
        try:
            last = client.return_panoramic(serial)
        except Exception as exc:
            return PanoramaResult(
                None, [], None, f"Panorama fetch failed: {ez._friendly_api_error(exc)}"
            )

        pics = last.get("picInfo") if isinstance(last, dict) else None
        if not isinstance(pics, dict):
            time.sleep(3.0)
            continue

        progress = int(pics.get("progress") or 0)
        tiles = pics.get("levelPicInfoList") or []
        if progress != last_progress:
            last_progress = progress

        # Wait until build completes AND tiles exist
        if progress < 100 or not isinstance(tiles, list) or not tiles:
            time.sleep(3.0)
            continue

        urls = [
            str(t.get("picUrl") or "").strip()
            for t in tiles
            if isinstance(t, dict) and str(t.get("picUrl") or "").startswith("http")
        ]
        img, err, meta = _download_and_stitch(tiles, password=password)
        if img is not None:
            _cache_image(serial, img, meta)
            return PanoramaResult(img, urls, last, "Panorama ready — click to look")
        # If decrypt failed, don't spin forever
        return PanoramaResult(
            None,
            urls,
            last,
            err
            or "Got panorama tiles but could not decrypt/open them. "
            "Check the camera verification code (password).",
        )

    # Try cache as last resort
    cached = _load_cache(serial)
    if cached is not None:
        return PanoramaResult(
            cached,
            [],
            last,
            f"Using cached 360 (live build progress {last_progress}%). Click Refresh for a new scan.",
        )
    return PanoramaResult(
        None,
        [],
        last,
        f"Timed out waiting for panorama (progress {last_progress}%). Try again — scan can take 1–2 minutes.",
    )


def create_and_fetch_panorama(
    ip: str, *, password: str = "", wait_sec: float = 150.0, force_new: bool = True
) -> PanoramaResult:
    """Create panoramic then wait for the stitched image."""
    serial = ""
    try:
        serial = ez.resolve_serial(ip)
    except Exception:
        pass

    if not force_new and serial:
        cached = _load_cache(serial)
        if cached is not None:
            return PanoramaResult(cached, [], None, "Cached 360 — click Refresh for a new scan")

    try:
        create_panorama(ip)
    except Exception as exc:
        msg = ez._friendly_api_error(exc)
        fetched = fetch_panorama(ip, password=password, wait_sec=min(45.0, wait_sec))
        if fetched.image is not None:
            return fetched
        if serial:
            cached = _load_cache(serial)
            if cached is not None:
                return PanoramaResult(
                    cached, [], None, f"Create failed ({msg}) — showing last cached 360"
                )
        return PanoramaResult(None, [], None, f"Create panoramic failed: {msg}")
    return fetch_panorama(ip, password=password, wait_sec=wait_sec)


def look_at(ip: str, x: float, y: float) -> tuple[bool, str]:
    """Move PTZ to a normalized point (0..1) on the panorama or live frame."""
    try:
        serial = ez.resolve_serial(ip)
        meta = _load_meta(serial)
        api_x, api_y = _visual_to_api(x, y, meta)
        client = ez.get_client()
        client.ptz_control_coordinates(serial, api_x, api_y)
        return True, f"Looking at ({api_x:.2f}, {api_y:.2f})"
    except Exception as exc:
        return False, ez._friendly_api_error(exc)


def _download_and_stitch(
    tiles: list[dict], *, password: str
) -> tuple[Image.Image | None, str, StitchMeta | None]:
    """Download tiles, decrypt, and stitch with overlap removal (smooth 360)."""
    ordered = sorted(
        [t for t in tiles if isinstance(t, dict) and t.get("picUrl")],
        key=lambda t: int(t.get("index") or 0),
    )
    if not ordered:
        return None, "No panorama tiles in response", None

    images: list[Image.Image] = []
    last_err = ""
    for item in ordered:
        url = str(item.get("picUrl") or "")
        raw = _http_get(url)
        if not raw:
            last_err = f"Download failed for tile {item.get('index')}"
            continue
        dec, err = _decrypt_tile(raw, password)
        if dec is None:
            last_err = err or "Decrypt failed"
            continue
        try:
            im = Image.open(io.BytesIO(dec)).convert("RGB")
        except Exception as exc:
            last_err = f"Tile is not a valid image after decrypt: {exc}"
            continue
        images.append(im)

    if not images:
        return None, last_err or "No tiles could be decoded", None

    # Crop shared overlap and blend seams so the strip looks like one 360 picture.
    img, meta = _stitch_overlap(images)
    return img, "", meta


def _estimate_overlap(a: Image.Image, b: Image.Image) -> int:
    """Estimate horizontal overlap (pixels) between consecutive tiles."""
    import numpy as np

    aa = np.asarray(a.convert("L"), dtype="float32")
    bb = np.asarray(b.convert("L"), dtype="float32")
    h, w = aa.shape
    band_a = aa[h // 3 : 2 * h // 3]
    band_b = bb[h // 3 : 2 * h // 3]
    best_ov = max(8, w // 3)
    best_score = float("inf")
    for ov in range(max(16, w // 5), int(w * 0.8), 4):
        left = band_a[:, w - ov :]
        right = band_b[:, :ov]
        score = float(np.mean((left - right) ** 2))
        if score < best_score:
            best_score = score
            best_ov = ov
    return int(best_ov)


def _stitch_overlap(images: list[Image.Image]) -> tuple[Image.Image, StitchMeta]:
    """Stitch left→right: crop shared overlap, feather-blend seams."""
    import numpy as np

    if len(images) == 1:
        im = images[0]
        meta = StitchMeta(1, im.width, 0, im.width, im.height)
        return im, meta

    overlaps = [_estimate_overlap(images[i], images[i + 1]) for i in range(len(images) - 1)]
    ov = int(sorted(overlaps)[len(overlaps) // 2]) if overlaps else images[0].width // 2
    # EZVIZ tiles commonly share ~45–55%. Prefer ~50% so seams stay smooth
    # and tap→aim mapping stays stable.
    w0 = int(sorted(im.width for im in images)[len(images) // 2])
    ov = max(int(w0 * 0.40), min(ov, int(w0 * 0.60)))
    ov = int(round(ov / 2) * 2) or ov
    blend = max(8, min(64, ov // 3))

    # Normalize tile widths so meta.tile_w matches what we paste.
    normed: list[Image.Image] = []
    h = max(im.height for im in images)
    for im in images:
        if im.width != w0 or im.height != h:
            normed.append(im.resize((w0, h), Image.Resampling.BILINEAR))
        else:
            normed.append(im)

    strides = [max(1, w0 - ov) for _ in normed[1:]]
    total_w = w0 + sum(strides)
    canvas = np.zeros((h, total_w, 3), dtype="float32")

    a0 = np.asarray(normed[0].convert("RGB"), dtype="float32")
    canvas[:, : a0.shape[1], :] = a0
    x = w0 - ov

    for im in normed[1:]:
        arr = np.asarray(im.convert("RGB"), dtype="float32")
        crop_start = ov
        piece = arr[:, crop_start:]
        pw = piece.shape[1]
        b = min(blend, pw, x)
        if b > 0:
            seam = x
            left_src = arr[:, ov - b : ov]
            right_dst = canvas[:, seam - b : seam, :]
            if left_src.shape[1] == b and right_dst.shape[1] == b:
                ramp = np.linspace(0.0, 1.0, b, dtype="float32")[None, :, None]
                canvas[:, seam - b : seam, :] = right_dst * (1.0 - ramp) + left_src * ramp
        canvas[:, x : x + pw, :] = piece
        x += pw

    out = Image.fromarray(canvas.clip(0, 255).astype("uint8"), mode="RGB")
    meta = StitchMeta(len(normed), w0, ov, out.width, out.height)
    return out, meta


def _http_get(url: str) -> bytes | None:
    try:
        resp = requests.get(
            url,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 Optiview/1.0"},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    try:
        client = ez.get_client()
        resp = client._session.get(url, timeout=30)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    return None


def _decrypt_tile(raw: bytes, password: str) -> tuple[bytes | None, str]:
    """Decrypt hikencodepicture; plain JPEG/PNG passes through."""
    if not raw:
        return None, "Empty tile"
    # Already a normal image?
    if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw, ""
    if not raw.startswith(b"hikencodepicture"):
        # Unknown wrapper — try as image anyway
        return raw, ""

    try:
        from pyezviz.utils import decrypt_image
    except Exception:
        return None, "pyezviz decrypt_image missing — update pyezviz"

    if not (password or "").strip():
        return None, "Camera password (verification code) required to decrypt 360 tiles"

    last = ""
    for pwd in _password_variants(password):
        try:
            return decrypt_image(raw, pwd), ""
        except Exception as exc:
            last = str(exc)
            continue
    return None, f"Could not decrypt 360 tile ({last}). Check camera verification code."


def _cache_path(serial: str) -> Path:
    PANO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in serial)[:40]
    return PANO_CACHE_DIR / f"{safe}.jpg"


def _meta_path(serial: str) -> Path:
    return _cache_path(serial).with_suffix(".json")


def _cache_image(serial: str, img: Image.Image, meta: StitchMeta | None = None) -> None:
    try:
        img.save(_cache_path(serial), format="JPEG", quality=88, optimize=True)
    except Exception:
        pass
    if meta is not None:
        try:
            _meta_path(serial).write_text(json.dumps(asdict(meta)), encoding="utf-8")
        except Exception:
            pass


def _load_meta(serial: str) -> StitchMeta | None:
    path = _meta_path(serial)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return StitchMeta(
            tile_count=int(raw.get("tile_count") or 0),
            tile_w=int(raw.get("tile_w") or 0),
            overlap=int(raw.get("overlap") or 0),
            visual_w=int(raw.get("visual_w") or 0),
            visual_h=int(raw.get("visual_h") or 0),
        )
    except Exception:
        return None


def _load_cache(serial: str) -> Image.Image | None:
    path = _cache_path(serial)
    if not path.is_file():
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def load_cached_panorama(serial: str) -> Image.Image | None:
    """Public: last stitched 360 for this camera serial, if any."""
    return _load_cache(serial)
