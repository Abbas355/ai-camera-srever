"""InsightFace (buffalo_l) wrapper. Lazy-loaded; optional dependency.

Works under pythonw.exe (no console): InsightFace/tqdm write download
progress to stdout, which is None under pythonw and crashes with
"'NoneType' object has no attribute 'write'".
"""

from __future__ import annotations

import io
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np


class FaceEngineError(RuntimeError):
    """Raised when the face engine cannot run (missing deps or bad input)."""


@dataclass
class FaceHit:
    """One detected face with embedding + pixel bbox (x1,y1,x2,y2)."""

    embedding: np.ndarray
    bbox: tuple[float, float, float, float]
    area: float
    det_score: float = 0.0
    yaw: float = 0.0  # degrees; 0 ≈ front-facing
    pitch: float = 0.0
    likely_print: bool = False  # wall poster / banner photo (not a live person)

    def is_clear_for_enroll(
        self,
        *,
        min_area: float = 2500.0,
        min_det: float = 0.55,
        max_yaw: float = 25.0,
        max_pitch: float = 25.0,
    ) -> bool:
        """True when the face is large, confident, and roughly frontal."""
        if self.likely_print:
            return False
        if self.area < float(min_area):
            return False
        if self.det_score < float(min_det):
            return False
        if abs(self.yaw) > float(max_yaw):
            return False
        if abs(self.pitch) > float(max_pitch):
            return False
        return True


def _ensure_stdio() -> None:
    """Give InsightFace/tqdm a writable stream when running under pythonw."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    # Some libraries wrap stdout; ensure .write always exists.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "write"):
            setattr(sys, name, io.StringIO())


class FaceEngine:
    """Thread-safe InsightFace FaceAnalysis singleton."""

    _lock = threading.Lock()
    # Serialize onnxruntime get() — concurrent CPU runs thrash under load.
    _infer_lock = threading.Lock()
    _app: Any | None = None
    _init_error: str | None = None
    _ready: bool = False

    @classmethod
    def available(cls) -> tuple[bool, str]:
        """Package import check (does not load the buffalo_l model yet)."""
        try:
            import insightface  # noqa: F401
            import onnxruntime  # noqa: F401
        except ImportError as exc:
            return False, (
                "InsightFace is not installed. Run: "
                "pip install insightface onnxruntime opencv-python-headless"
                f" ({exc})"
            )
        if cls._init_error:
            return False, cls._init_error
        if cls._ready:
            return True, "ok"
        return True, "ready (model loads on first scan/enroll)"

    @classmethod
    def _ensure(cls) -> Any:
        with cls._lock:
            if cls._app is not None:
                return cls._app
            ok, msg = cls.available()
            if not ok and cls._app is None and "not installed" in msg:
                cls._init_error = msg
                raise FaceEngineError(msg)
            # Allow retry after a previous pythonw stdio failure.
            cls._init_error = None
            _ensure_stdio()
            try:
                from insightface.app import FaceAnalysis

                # Quiet onnxruntime spam under pythonw.
                os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")
                app = FaceAnalysis(
                    name="buffalo_l",
                    providers=["CPUExecutionProvider"],
                )
                # Larger det window finds smaller / farther faces on CCTV frames.
                app.prepare(ctx_id=0, det_size=(1280, 1280))
                cls._app = app
                cls._ready = True
                cls._init_error = None
                return cls._app
            except Exception as exc:
                cls._init_error = f"Failed to load InsightFace buffalo_l: {exc}"
                cls._ready = False
                raise FaceEngineError(cls._init_error) from exc

    @classmethod
    def warmup(cls) -> tuple[bool, str]:
        """Load the model now (used by status / server start). Safe to call often."""
        try:
            cls._ensure()
            return True, "ok"
        except FaceEngineError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._app = None
            cls._init_error = None
            cls._ready = False

    @staticmethod
    def _face_area(face: Any) -> float:
        b = getattr(face, "bbox", None)
        if b is None or len(b) < 4:
            return 0.0
        return float(max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]))

    @staticmethod
    def _norm_embedding(face: Any) -> np.ndarray | None:
        emb = getattr(face, "normed_embedding", None)
        if emb is None:
            emb = getattr(face, "embedding", None)
            if emb is None:
                return None
            emb = np.asarray(emb, dtype=np.float32)
            n = float(np.linalg.norm(emb))
            if n < 1e-6:
                return None
            emb = emb / n
        else:
            emb = np.asarray(emb, dtype=np.float32)
        if emb.ndim != 1 or emb.size < 64:
            return None
        return emb.astype(np.float32, copy=False)

    @classmethod
    def _decode_image(cls, jpeg: bytes) -> Any:
        try:
            import cv2
        except ImportError as exc:
            raise FaceEngineError(f"OpenCV required: {exc}") from exc
        if not jpeg:
            raise FaceEngineError("Empty image")
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            raise FaceEngineError("Could not decode JPEG")
        return img

    @classmethod
    def _detect_raw(
        cls,
        img: Any,
        *,
        upscale_small: bool = True,
        wait: bool = True,
    ) -> tuple[list[Any], float]:
        """Run InsightFace; optionally upscale small CCTV frames.

        Returns (faces, scale) where face bboxes are in *original* image coords
        when scale > 1 (detector ran on an upscaled copy).

        upscale_small=False is faster for live enroll/identify polls.
        wait=False skips when another detect is running (watcher under load).
        """
        try:
            import cv2
        except ImportError as exc:
            raise FaceEngineError(f"OpenCV required: {exc}") from exc
        scale = 1.0
        work = img
        h, w = int(img.shape[0]), int(img.shape[1])
        # Substream ~480p hides table-distance faces; 2× helps buffalo_l a lot.
        if upscale_small and max(h, w) < 900:
            scale = 2.0
            work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        acquired = cls._infer_lock.acquire(blocking=wait)
        if not acquired:
            raise FaceEngineError("face engine busy")
        try:
            faces = list(cls._ensure().get(work) or [])
        finally:
            cls._infer_lock.release()
        if scale == 1.0:
            return faces, 1.0
        # Map bboxes back to the original JPEG coordinate space.
        for face in faces:
            b = getattr(face, "bbox", None)
            if b is None or len(b) < 4:
                continue
            face.bbox = np.asarray(
                [float(b[0]) / scale, float(b[1]) / scale, float(b[2]) / scale, float(b[3]) / scale],
                dtype=np.float32,
            )
        return faces, scale

    @classmethod
    def _decode_faces(cls, jpeg: bytes) -> list[Any]:
        img = cls._decode_image(jpeg)
        faces, _ = cls._detect_raw(img)
        return faces

    @classmethod
    def embed_jpeg(cls, jpeg: bytes) -> np.ndarray:
        """Return L2-normalized embedding (float32) for the largest face in the JPEG."""
        faces = cls._decode_faces(jpeg)
        if not faces:
            raise FaceEngineError("No face detected — face the camera clearly and try again")
        face = max(faces, key=cls._face_area)
        emb = cls._norm_embedding(face)
        if emb is None:
            raise FaceEngineError("Face detected but no embedding produced")
        return emb

    @classmethod
    def embed_all_jpeg(cls, jpeg: bytes) -> list[np.ndarray]:
        """All face embeddings in a JPEG (largest first). Empty list if none."""
        return [h.embedding for h in cls.detect_jpeg(jpeg)]

    @classmethod
    def _looks_like_printed_face(
        cls,
        img: Any,
        bbox: tuple[float, float, float, float],
    ) -> bool:
        """True for wall-banner / poster faces (graphic-heavy surround).

        Live people sit in soft room context (desk, clothing, walls). Printed
        faces on office banners usually sit inside hard graphic edges / text.
        """
        try:
            import cv2
        except ImportError:
            return False
        try:
            h, w = int(img.shape[0]), int(img.shape[1])
            x1, y1, x2, y2 = [float(v) for v in bbox]
            bw = max(1.0, x2 - x1)
            bh = max(1.0, y2 - y1)
            ix1, iy1 = max(0, int(x1)), max(0, int(y1))
            ix2, iy2 = min(w, int(x2)), min(h, int(y2))
            if ix2 - ix1 < 18 or iy2 - iy1 < 18:
                return True
            face = img[iy1:iy2, ix1:ix2]
            pad = 0.8
            ox1 = max(0, int(x1 - bw * pad))
            oy1 = max(0, int(y1 - bh * pad))
            ox2 = min(w, int(x2 + bw * pad))
            oy2 = min(h, int(y2 + bh * pad))
            if ox2 - ox1 < 24 or oy2 - oy1 < 24:
                return False
            outer = img[oy1:oy2, ox1:ox2]

            face_g = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
            outer_g = cv2.cvtColor(outer, cv2.COLOR_BGR2GRAY)
            face_s = cv2.resize(face_g, (48, 48), interpolation=cv2.INTER_AREA)
            outer_s = cv2.resize(outer_g, (80, 80), interpolation=cv2.INTER_AREA)

            face_edges = cv2.Canny(face_s, 55, 130)
            outer_edges = cv2.Canny(outer_s, 55, 130)
            # Ring around the face (mask out center ≈ face).
            mask = np.ones_like(outer_edges, dtype=np.uint8)
            mh, mw = int(mask.shape[0]), int(mask.shape[1])
            mask[mh // 5 : (4 * mh) // 5, mw // 5 : (4 * mw) // 5] = 0
            ring_nz = int(np.count_nonzero(mask))
            if ring_nz < 50:
                return False
            ring_density = float(np.count_nonzero(outer_edges * mask)) / float(ring_nz)
            face_density = float(np.count_nonzero(face_edges)) / float(face_edges.size)

            # Banner/poster: dense structured edges (logos, text, borders) around face.
            if ring_density >= 0.12 and ring_density >= face_density * 1.10:
                return True

            # Extra: very flat chroma + low micro-detail often = printed photo paper.
            face_bgr = cv2.resize(face, (48, 48), interpolation=cv2.INTER_AREA)
            ycrcb = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2YCrCb)
            chroma = float(np.std(ycrcb[:, :, 1]) + np.std(ycrcb[:, :, 2]))
            blur = cv2.GaussianBlur(face_s, (0, 0), 1.1)
            detail = float(
                np.mean(np.abs(face_s.astype(np.float32) - blur.astype(np.float32)))
            )
            if chroma < 6.5 and detail < 3.2 and ring_density >= 0.08:
                return True
            return False
        except Exception:
            return False

    @classmethod
    def detect_jpeg(
        cls,
        jpeg: bytes,
        *,
        min_area: float = 0.0,
        min_det_score: float = 0.0,
        min_face_frac: float = 0.0,
        reject_prints: bool = True,
        upscale_small: bool = True,
        wait: bool = True,
    ) -> list[FaceHit]:
        """Detect faces with embeddings and bboxes (largest first).

        Low-confidence detections produce unstable embeddings and cause
        random Guest enrolls — filter them with min_det_score when set.

        min_face_frac is relative to the decoded frame (works for both
        substream ~480p and full HD). Absolute min_area is also applied.

        reject_prints drops wall-banner / poster faces (office graphics).
        upscale_small=False skips 2× resize (faster live UI polls).
        wait=False returns [] immediately if another detect is in progress.
        """
        try:
            img = cls._decode_image(jpeg)
        except FaceEngineError:
            return []
        except Exception:
            return []
        frame_px = float(img.shape[0] * img.shape[1])
        # Absolute floor + relative floor: take the *less* strict of the two
        # when both are set, so HD cameras are not blocked by a high % gate
        # and tiny substream noise is still dropped by min_area.
        abs_floor = max(0.0, float(min_area))
        frac_floor = max(0.0, float(min_face_frac)) * frame_px
        if abs_floor > 0 and frac_floor > 0:
            # Use the softer of the two so distant faces on substream OR HD pass.
            area_floor = min(abs_floor, frac_floor)
        else:
            area_floor = max(abs_floor, frac_floor)
        # Tiny noise floor (~17×17); real table-distance faces are usually larger.
        area_floor = max(280.0, area_floor)
        try:
            faces, _ = cls._detect_raw(img, upscale_small=upscale_small, wait=wait)
        except FaceEngineError as exc:
            if "busy" in str(exc).lower():
                raise
            return []
        except Exception:
            return []
        hits: list[FaceHit] = []
        for face in faces:
            emb = cls._norm_embedding(face)
            if emb is None:
                continue
            b = getattr(face, "bbox", None)
            if b is None or len(b) < 4:
                continue
            det = float(getattr(face, "det_score", 0.0) or 0.0)
            if det < float(min_det_score):
                continue
            bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            area = float(max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]))
            if area < area_floor:
                continue
            yaw = 0.0
            pitch = 0.0
            pose = getattr(face, "pose", None)
            if pose is not None:
                try:
                    yaw = float(pose[0])
                    pitch = float(pose[1]) if len(pose) > 1 else 0.0
                except Exception:
                    yaw, pitch = 0.0, 0.0
            printed = cls._looks_like_printed_face(img, bbox)
            if printed and reject_prints:
                continue
            hits.append(
                FaceHit(
                    embedding=emb,
                    bbox=bbox,
                    area=area,
                    det_score=det,
                    yaw=yaw,
                    pitch=pitch,
                    likely_print=printed,
                )
            )
        hits.sort(key=lambda h: (h.area * (0.55 + h.det_score), h.det_score), reverse=True)
        return hits

    @staticmethod
    def crop_face_jpeg(
        jpeg: bytes,
        bbox: tuple[float, float, float, float],
        *,
        pad: float = 0.30,
        min_side: int = 224,
    ) -> bytes:
        """Crop a face region (padded). Upscale tiny CCTV crops for clearer portraits."""
        try:
            import cv2
        except ImportError:
            return jpeg
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return jpeg
        h, w = img.shape[:2]
        x1, y1, x2, y2 = bbox
        bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
        px, py = bw * pad, bh * pad
        xa = max(0, int(x1 - px))
        ya = max(0, int(y1 - py))
        xb = min(w, int(x2 + px))
        yb = min(h, int(y2 + py))
        if xb <= xa or yb <= ya:
            return jpeg
        crop = img[ya:yb, xa:xb]
        ch, cw = crop.shape[:2]
        # Upscale small faces so gallery photos and re-embeds stay sharp.
        if min(ch, cw) < int(min_side) and min(ch, cw) > 0:
            scale = float(min_side) / float(min(ch, cw))
            scale = min(scale, 4.0)
            crop = cv2.resize(
                crop,
                (max(1, int(cw * scale)), max(1, int(ch * scale))),
                interpolation=cv2.INTER_CUBIC,
            )
        ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return buf.tobytes() if ok else jpeg

    @staticmethod
    def embedding_to_bytes(emb: np.ndarray) -> bytes:
        return np.asarray(emb, dtype=np.float32).tobytes()

    @staticmethod
    def bytes_to_embedding(data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32).copy()

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            return -1.0
        return float(np.dot(a, b))
