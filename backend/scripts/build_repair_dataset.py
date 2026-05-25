#!/usr/bin/env python3
"""Build and validate a repair-part detector dataset from reviewed HUD exports.

Expected input layout:
  reviewed_root/
    images/*.jpg
    labels/*.txt
    classes.txt

The script creates a YOLO-compatible train/val split with data.yaml. Labels
must already be human-reviewed; raw HUD exports are intentionally not treated
as production labels.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path


def read_classes(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"missing classes file: {path}")
    classes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not classes:
        raise ValueError(f"no classes in {path}")
    return classes


def validate_label(path: Path, class_count: int) -> list[str]:
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            errors.append(f"{path.name}:{line_no}: expected 5 columns")
            continue
        try:
            cls = int(parts[0])
            vals = [float(v) for v in parts[1:]]
        except ValueError:
            errors.append(f"{path.name}:{line_no}: non-numeric label")
            continue
        if cls < 0 or cls >= class_count:
            errors.append(f"{path.name}:{line_no}: class id {cls} out of range")
        if any(v < 0 or v > 1 for v in vals):
            errors.append(f"{path.name}:{line_no}: normalized coords outside 0..1")
        if vals[2] <= 0 or vals[3] <= 0:
            errors.append(f"{path.name}:{line_no}: width/height must be positive")
    return errors


def build_dataset(reviewed_root: Path, output: Path, val_ratio: float, seed: int) -> dict:
    classes = read_classes(reviewed_root / "classes.txt")
    image_dir = reviewed_root / "images"
    label_dir = reviewed_root / "labels"
    if not image_dir.exists() or not label_dir.exists():
        raise FileNotFoundError("reviewed root must contain images/ and labels/")

    image_paths = sorted(p for p in image_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    pairs = []
    errors = []
    for image in image_paths:
        label = label_dir / f"{image.stem}.txt"
        if not label.exists():
            errors.append(f"{image.name}: missing label")
            continue
        label_errors = validate_label(label, len(classes))
        if label_errors:
            errors.extend(label_errors)
            continue
        pairs.append((image, label))

    if errors:
        return {"ok": False, "errors": errors[:200], "error_count": len(errors)}
    if len(pairs) < 2:
        return {"ok": False, "errors": ["need at least 2 labeled images"]}

    random.Random(seed).shuffle(pairs)
    val_count = max(1, int(round(len(pairs) * val_ratio)))
    val_pairs = pairs[:val_count]
    train_pairs = pairs[val_count:] or pairs[:1]

    if output.exists():
        shutil.rmtree(output)
    for split in ("train", "val"):
        (output / "images" / split).mkdir(parents=True, exist_ok=True)
        (output / "labels" / split).mkdir(parents=True, exist_ok=True)

    def copy_pairs(rows: list[tuple[Path, Path]], split: str) -> None:
        for image, label in rows:
            shutil.copyfile(image, output / "images" / split / image.name)
            shutil.copyfile(label, output / "labels" / split / label.name)

    copy_pairs(train_pairs, "train")
    copy_pairs(val_pairs, "val")

    yaml_text = "\n".join([
        f"path: {output}",
        "train: images/train",
        "val: images/val",
        "names:",
        *[f"  {i}: {name}" for i, name in enumerate(classes)],
        "",
    ])
    (output / "data.yaml").write_text(yaml_text, encoding="utf-8")

    return {
        "ok": True,
        "output": str(output),
        "classes": classes,
        "train_images": len(train_pairs),
        "val_images": len(val_pairs),
        "data_yaml": str(output / "data.yaml"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reviewed_root", help="Reviewed YOLO export root with images/, labels/, classes.txt")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "repair_detector_dataset"))
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    result = build_dataset(Path(args.reviewed_root), Path(args.output), args.val_ratio, args.seed)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
