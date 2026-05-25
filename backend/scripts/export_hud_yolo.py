#!/usr/bin/env python3
"""Export accepted HUD marker boxes to a YOLO-style dataset.

This is the bridge for the later fine-tuned repair-part detector. It uses
locked/corrected/highlighted markers as weak labels, then a human can review
the generated labels before training YOLO/RT-DETR.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "hud_dataset"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def class_id(label: str, classes: list[str]) -> int:
    key = (label or "target").lower().strip().replace(" ", "_")
    if key not in classes:
        classes.append(key)
    return classes.index(key)


def yolo_row(cls: int, geometry: dict) -> str:
    x = float(geometry.get("x") or 0)
    y = float(geometry.get("y") or 0)
    w = float(geometry.get("width") or 0)
    h = float(geometry.get("height") or 0)
    return f"{cls} {x + w / 2:.6f} {y + h / 2:.6f} {w:.6f} {h:.6f}"


def export(session_dir: Path, output: Path) -> dict:
    output_images = output / "images"
    output_labels = output / "labels"
    output_images.mkdir(parents=True, exist_ok=True)
    output_labels.mkdir(parents=True, exist_ok=True)
    events = load_jsonl(session_dir / "events.jsonl")
    classes: list[str] = []
    exported = 0
    skipped = 0

    for event in events:
        if event.get("event_type") not in {"highlight", "correction", "manual_update", "ai_relock"}:
            continue
        frame_path = event.get("frame_path")
        geometry = event.get("geometry") or (event.get("marker") or {}).get("geometry")
        marker = event.get("marker") or {}
        label = marker.get("target_hint") or marker.get("label") or event.get("marker_id") or "target"
        if not frame_path or not geometry:
            skipped += 1
            continue
        src = session_dir / frame_path
        if not src.exists():
            skipped += 1
            continue
        stem = f"{session_dir.name}_{src.stem}_{event.get('event_type')}_{exported:04d}"
        shutil.copyfile(src, output_images / f"{stem}.jpg")
        cls = class_id(label, classes)
        (output_labels / f"{stem}.txt").write_text(yolo_row(cls, geometry) + "\n", encoding="utf-8")
        exported += 1

    (output / "classes.txt").write_text("\n".join(classes) + "\n", encoding="utf-8")
    return {"session_id": session_dir.name, "exported": exported, "skipped": skipped, "classes": classes, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "hud_yolo_export"))
    args = parser.parse_args()
    print(json.dumps(export(ROOT / args.session_id, Path(args.output)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
