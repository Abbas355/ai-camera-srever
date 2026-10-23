"""Shared YOLO weights config (Floor occupancy + future detectors)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from v380.paths import ROOT


@dataclass
class PersonDetectConfig:
    conf: float = 0.45
    iou: float = 0.45
    infer_width: int = 640
    weights_name: str = "yolov8n.pt"
    models_dir: Path = ROOT / "v380" / "ai" / "models"
    device: str = "cpu"

    def weights_path(self) -> Path:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        return self.models_dir / self.weights_name


@dataclass
class WorldDetectConfig:
    """Open-vocab YOLO-World for office desks / tables (Floor Tables button)."""

    # Strict floor — walls/posters/shelves were flooding Floor overlays.
    conf: float = 0.40
    iou: float = 0.50
    infer_width: int = 960
    weights_name: str = "yolov8s-world.pt"
    models_dir: Path = ROOT / "v380" / "ai" / "models"
    device: str = "cpu"
    # Only desk-like prompts (bare "table" matches doors / shelves / panels).
    class_prompts: list[str] = field(
        default_factory=lambda: [
            "office desk",
            "computer desk",
            "work desk",
        ]
    )

    def weights_path(self) -> Path:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        return self.models_dir / self.weights_name


def default_config() -> PersonDetectConfig:
    return PersonDetectConfig()
