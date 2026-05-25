#!/usr/bin/env python3
"""Train a YOLO/RT-DETR repair-part detector from a reviewed dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def train(data_yaml: Path, model: str, epochs: int, imgsz: int, project: Path, name: str, device: str | None) -> dict:
    if not data_yaml.exists():
        raise FileNotFoundError(f"missing data yaml: {data_yaml}")
    from ultralytics import YOLO, RTDETR

    model_lower = model.lower()
    trainer = RTDETR(model) if "rtdetr" in model_lower or "rt-detr" in model_lower else YOLO(model)
    result = trainer.train(
        data=str(data_yaml),
        epochs=epochs,
        imgsz=imgsz,
        project=str(project),
        name=name,
        device=device,
    )
    save_dir = Path(getattr(result, "save_dir", project / name))
    weights = save_dir / "weights" / "best.pt"
    return {
        "ok": True,
        "model": model,
        "data": str(data_yaml),
        "save_dir": str(save_dir),
        "best_weights": str(weights),
        "use_in_app": f"HUD_REPAIR_DETECTOR_MODEL={weights}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_yaml", help="Path to YOLO data.yaml from build_repair_dataset.py")
    parser.add_argument("--model", default="yolo11n.pt", help="Ultralytics model, e.g. yolo11n.pt or rtdetr-l.pt")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--project", default=str(Path(__file__).resolve().parents[1] / "repair_detector_runs"))
    parser.add_argument("--name", default="repair_parts")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()
    print(json.dumps(train(Path(args.data_yaml), args.model, args.epochs, args.imgsz, Path(args.project), args.name, args.device), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
