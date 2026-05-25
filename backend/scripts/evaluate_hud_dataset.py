#!/usr/bin/env python3
"""Evaluate captured HUD marker sessions.

This reads backend/hud_dataset/<session_id>/events.jsonl and reports coarse
tracking metrics that are useful before we have hand-labeled masks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean


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


def area(geometry: dict | None) -> float:
    if not geometry:
        return 0.0
    return float(geometry.get("width") or 0) * float(geometry.get("height") or 0)


def center(geometry: dict | None) -> tuple[float, float]:
    if not geometry:
        return (0.0, 0.0)
    return (
        float(geometry.get("x") or 0) + float(geometry.get("width") or 0) / 2,
        float(geometry.get("y") or 0) + float(geometry.get("height") or 0) / 2,
    )


def distance(a: dict | None, b: dict | None) -> float:
    ax, ay = center(a)
    bx, by = center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def evaluate_session(session_dir: Path) -> dict:
    events = load_jsonl(session_dir / "events.jsonl")
    by_marker: dict[str, list[dict]] = {}
    for event in events:
        marker_id = event.get("marker_id") or (event.get("marker") or {}).get("id")
        if marker_id:
            by_marker.setdefault(marker_id, []).append(event)

    corrections = [e for e in events if e.get("event_type") == "correction"]
    rejections = [e for e in events if "rejected" in str(e.get("event_type", ""))]
    verifies = [e for e in events if e.get("event_type") == "ai_verify"]
    stream_events = [e for e in events if e.get("event_type") == "sam2_stream"]

    marker_summaries = []
    for marker_id, rows in by_marker.items():
        geometries = []
        for row in rows:
            geom = row.get("geometry") or (row.get("marker") or {}).get("geometry")
            if geom:
                geometries.append(geom)
        jumps = [distance(a, b) for a, b in zip(geometries, geometries[1:])]
        areas = [area(g) for g in geometries]
        marker_summaries.append({
            "marker_id": marker_id,
            "events": len(rows),
            "updates": len(geometries),
            "avg_center_jump": round(mean(jumps), 5) if jumps else 0.0,
            "max_center_jump": round(max(jumps), 5) if jumps else 0.0,
            "avg_area": round(mean(areas), 6) if areas else 0.0,
            "max_area_growth": round(max((b / max(a, 1e-6)) for a, b in zip(areas, areas[1:])), 3) if len(areas) > 1 else 1.0,
        })

    return {
        "session_id": session_dir.name,
        "event_count": len(events),
        "markers": marker_summaries,
        "corrections": len(corrections),
        "rejections": len(rejections),
        "ai_verifications": len(verifies),
        "sam2_stream_updates": len(stream_events),
        "needs_review": bool(corrections or rejections or any(m["max_center_jump"] > 0.08 for m in marker_summaries)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id", nargs="?", help="Session id under backend/hud_dataset")
    args = parser.parse_args()

    sessions = [ROOT / args.session_id] if args.session_id else sorted(p for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []
    report = [evaluate_session(path) for path in sessions if path.exists()]
    print(json.dumps(report[0] if args.session_id and report else report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
