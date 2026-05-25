import asyncio
import base64
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

try:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except Exception:
    pass


BACKEND_DIR = Path(__file__).resolve().parent
HUD_DATASET_DIR = Path(os.environ.get("HUD_DATASET_DIR", BACKEND_DIR / "hud_dataset"))
HUD_REVIEW_DIR = Path(os.environ.get("HUD_REVIEW_DIR", BACKEND_DIR / "hud_review_jobs"))
REVIEW_MODEL = os.environ.get("HUD_REVIEW_MODEL", os.environ.get("HUD_GROUNDING_MODEL", "gemini-2.5-flash"))
MAX_GEMINI_REVIEW_EXAMPLES = int(os.environ.get("HUD_REVIEW_MAX_EXAMPLES", "40"))


def _safe_id(value: str) -> str:
    return "".join(c if c.isalnum() or c in "_.-" else "_" for c in str(value or ""))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _job_root(session_id: str) -> Path:
    return HUD_REVIEW_DIR / _safe_id(session_id)


def _dataset_root(session_id: str) -> Path:
    return HUD_DATASET_DIR / _safe_id(session_id)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _load_job(session_id: str) -> Dict[str, Any]:
    path = _job_root(session_id) / "job.json"
    if not path.exists():
        return {"session_id": session_id, "status": "not_started"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"session_id": session_id, "status": "invalid_job_file"}


def _save_job(session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    existing = _load_job(session_id)
    existing.update(patch)
    existing["session_id"] = session_id
    existing["updated_at_ms"] = _now_ms()
    _write_json(_job_root(session_id) / "job.json", existing)
    return existing


def _normal_geometry(value: Any) -> Optional[Dict[str, float]]:
    if not isinstance(value, dict):
        return None
    try:
        x = max(0.0, min(1.0, float(value.get("x", 0))))
        y = max(0.0, min(1.0, float(value.get("y", 0))))
        w = max(0.001, min(1.0 - x, float(value.get("width", value.get("w", 0)))))
        h = max(0.001, min(1.0 - y, float(value.get("height", value.get("h", 0)))))
        return {"type": value.get("type", "box"), "x": x, "y": y, "width": w, "height": h}
    except Exception:
        return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _event_frame_path(dataset_root: Path, event: Dict[str, Any]) -> Optional[Path]:
    rel = event.get("frame_path")
    if not rel:
        return None
    path = dataset_root / str(rel)
    return path if path.exists() else None


def _marker_label(marker: Dict[str, Any], fallback: str = "") -> str:
    return str(marker.get("label") or marker.get("target_hint") or fallback or marker.get("marker_id") or "target").strip()


def _event_to_example(dataset_root: Path, event: Dict[str, Any], marker_labels: Dict[str, str]) -> Optional[Dict[str, Any]]:
    event_type = str(event.get("event_type") or "")
    frame_path = _event_frame_path(dataset_root, event)
    if not frame_path:
        return None

    source_rank = 10
    marker_id = str(event.get("marker_id") or "")
    marker: Dict[str, Any] = {}
    geometry = None
    label = ""
    contour = []
    human_corrected = False

    if event_type == "highlight":
        marker = event.get("marker") if isinstance(event.get("marker"), dict) else {}
        marker_id = str(marker.get("id") or marker_id)
        geometry = _normal_geometry(marker.get("geometry"))
        label = _marker_label(marker, event.get("args", {}).get("target_hint", "") if isinstance(event.get("args"), dict) else "")
        contour = marker.get("mask_contour") or []
        source_rank = 30
    elif event_type == "manual_update":
        geometry = _normal_geometry(event.get("geometry"))
        label = marker_labels.get(marker_id, marker_id or "manual target")
        human_corrected = True
        source_rank = 100
    elif event_type == "correction":
        geometry = _normal_geometry(event.get("geometry"))
        label = marker_labels.get(marker_id, marker_id or "corrected target")
        human_corrected = True
        source_rank = 110 if int((event.get("point") or {}).get("label", 1) or 1) > 0 else 90
    elif event_type == "ai_verify":
        before = event.get("marker_before") if isinstance(event.get("marker_before"), dict) else {}
        verification = event.get("verification") if isinstance(event.get("verification"), dict) else {}
        geometry = _normal_geometry(verification.get("suggested_geometry") if event.get("applied") else before.get("geometry"))
        marker_id = str(before.get("id") or marker_id)
        label = _marker_label(before, marker_labels.get(marker_id, "verified target"))
        source_rank = 60
    else:
        return None

    if not geometry:
        return None
    if marker_id and label:
        marker_labels[marker_id] = label

    return {
        "example_id": f"{event_type}-{event.get('frame_index', 0)}-{marker_id or 'target'}",
        "session_id": event.get("session_id"),
        "event_type": event_type,
        "frame_index": int(event.get("frame_index", 0) or 0),
        "frame_path": str(frame_path),
        "frame_rel": str(frame_path.relative_to(dataset_root)),
        "marker_id": marker_id,
        "label": label or "target",
        "geometry": geometry,
        "contour": contour if isinstance(contour, list) else [],
        "human_corrected": human_corrected,
        "source_rank": source_rank,
        "created_at_ms": event.get("at_ms"),
    }


def build_review_manifest(session_id: str) -> Dict[str, Any]:
    dataset_root = _dataset_root(session_id)
    events = _read_jsonl(dataset_root / "events.jsonl")
    frames = _read_jsonl(dataset_root / "frames.jsonl")
    marker_labels: Dict[str, str] = {}
    examples: List[Dict[str, Any]] = []

    for event in events:
        example = _event_to_example(dataset_root, event, marker_labels)
        if example:
            examples.append(example)

    # Prefer user-corrected examples for a marker/frame, but keep multiple frames over time.
    deduped: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for example in examples:
        key = (example.get("marker_id") or example["label"], int(example.get("frame_index") or 0), example.get("frame_rel") or "")
        prev = deduped.get(key)
        if not prev or int(example.get("source_rank", 0)) >= int(prev.get("source_rank", 0)):
            deduped[key] = example
    examples = sorted(deduped.values(), key=lambda item: (item.get("frame_index", 0), -item.get("source_rank", 0)))

    corrected = [item for item in examples if item.get("human_corrected")]
    accepted = corrected or [item for item in examples if item.get("event_type") in {"highlight", "ai_verify"}]
    manifest = {
        "session_id": session_id,
        "dataset_root": str(dataset_root),
        "created_at_ms": _now_ms(),
        "frame_count": len(frames),
        "event_count": len(events),
        "example_count": len(examples),
        "corrected_count": len(corrected),
        "accepted_count": len(accepted),
        "policy": {
            "live_session": "capture_only",
            "human_corrections": "gold",
            "gemini": "post_session_audit_only",
            "training": "manual_explicit_step",
        },
        "examples": examples,
        "accepted_examples": accepted,
    }
    root = _job_root(session_id)
    _write_json(root / "review_manifest.json", manifest)
    return manifest


def _write_training_export(session_id: str, accepted: List[Dict[str, Any]], review_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = _job_root(session_id) / "training_export"
    if root.exists():
        shutil.rmtree(root)
    image_dir = root / "images"
    label_dir = root / "labels"
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    names: List[str] = []
    review_by_id = {row.get("example_id"): row for row in review_rows}
    exported = 0

    for example in accepted:
        review = review_by_id.get(example.get("example_id"), {})
        if review and not bool(review.get("use_for_training", True)):
            continue
        src = Path(example["frame_path"])
        if not src.exists():
            continue
        label = str(review.get("label") or example.get("label") or "target").strip() or "target"
        if label not in names:
            names.append(label)
        class_id = names.index(label)
        dst_img = image_dir / f"{example['example_id']}.jpg"
        shutil.copyfile(src, dst_img)
        geom = example["geometry"]
        cx = geom["x"] + geom["width"] / 2
        cy = geom["y"] + geom["height"] / 2
        label_text = f"{class_id} {cx:.6f} {cy:.6f} {geom['width']:.6f} {geom['height']:.6f}\n"
        (label_dir / f"{example['example_id']}.txt").write_text(label_text, encoding="utf-8")
        _append_jsonl(root / "accepted_labels.jsonl", {
            **example,
            "review": review,
            "export_image": str(dst_img),
            "export_label": str(label_dir / f"{example['example_id']}.txt"),
            "class_id": class_id,
            "class_name": label,
        })
        exported += 1

    _write_json(root / "data.yaml", {
        "path": str(root),
        "train": "images",
        "val": "images",
        "names": {str(i): name for i, name in enumerate(names)},
    })
    return {"root": str(root), "exported": exported, "class_count": len(names), "classes": names}


def _gemini_url() -> str:
    key = os.environ.get("GOOGLE_API_KEY", "")
    return f"https://generativelanguage.googleapis.com/v1beta/models/{REVIEW_MODEL}:generateContent?key={key}"


async def _review_one_with_gemini(example: Dict[str, Any]) -> Dict[str, Any]:
    if not os.environ.get("GOOGLE_API_KEY"):
        return {"example_id": example.get("example_id"), "ok": False, "use_for_training": False, "reason": "GOOGLE_API_KEY missing"}
    image_path = Path(example["frame_path"])
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    prompt = (
        "Audit this HUD training label. Return only minified JSON with short keys. "
        f"target={example.get('label')}; source={example.get('event_type')}; "
        f"human_corrected={bool(example.get('human_corrected'))}; "
        f"box={json.dumps(example.get('geometry'), separators=(',', ':'))}. "
        "Rules: human_corrected examples are gold unless unusable; reject if target invisible, box is mostly background, or label is wrong. "
        "Small screws/ports/holes/tabs can be point labels. "
        'Return exactly these short keys: {"u":true,"q":0.9,"g":true,"l":"label","t":"point|contour","r":"reason"}.'
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0)) as client:
            res = await client.post(_gemini_url(), json=payload)
            res.raise_for_status()
            data = res.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        parsed = _extract_json(text)
        if parsed and any(key in parsed for key in ("u", "q", "g", "l", "t", "r")):
            parsed = {
                "ok": True,
                "use_for_training": bool(parsed.get("u")),
                "quality": float(parsed.get("q", 0) or 0),
                "geometry_ok": bool(parsed.get("g")),
                "label": str(parsed.get("l") or example.get("label") or "target"),
                "marker_type": str(parsed.get("t") or "contour"),
                "reason": str(parsed.get("r") or ""),
            }
        parsed = parsed or {
            "ok": False,
            "use_for_training": bool(example.get("human_corrected")),
            "reason": "parse failed",
            "raw": text[:500],
            "finish_reason": data.get("candidates", [{}])[0].get("finishReason") if data.get("candidates") else None,
        }
    except Exception as exc:
        parsed = {
            "ok": False,
            "use_for_training": bool(example.get("human_corrected")),
            "reason": str(exc),
        }
    return {"example_id": example.get("example_id"), **parsed}


async def run_gemini_review(session_id: str) -> Dict[str, Any]:
    manifest = build_review_manifest(session_id)
    examples = manifest.get("accepted_examples", [])[:MAX_GEMINI_REVIEW_EXAMPLES]
    review_file = _job_root(session_id) / "gemini_review.jsonl"
    if review_file.exists():
        review_file.unlink()
    _save_job(session_id, {
        "mode": "gemini",
        "status": "running",
        "started_at_ms": _now_ms(),
        "total_examples": len(examples),
        "reviewed_examples": 0,
        "error": None,
    })
    rows: List[Dict[str, Any]] = []
    try:
        for index, example in enumerate(examples, start=1):
            row = await _review_one_with_gemini(example)
            rows.append(row)
            _append_jsonl(_job_root(session_id) / "gemini_review.jsonl", row)
            _save_job(session_id, {"reviewed_examples": index})
            await asyncio.sleep(0)
        export = _write_training_export(session_id, examples, rows)
        job = _save_job(session_id, {
            "status": "complete",
            "completed_at_ms": _now_ms(),
            "reviewed_examples": len(rows),
            "export": export,
        })
        return job
    except Exception as exc:
        return _save_job(session_id, {"status": "failed", "error": str(exc), "completed_at_ms": _now_ms()})


def create_manual_review(session_id: str) -> Dict[str, Any]:
    manifest = build_review_manifest(session_id)
    export = _write_training_export(session_id, manifest.get("accepted_examples", []), [])
    return _save_job(session_id, {
        "mode": "manual",
        "status": "ready_for_manual_review",
        "created_at_ms": manifest.get("created_at_ms"),
        "total_examples": manifest.get("accepted_count", 0),
        "manifest_path": str(_job_root(session_id) / "review_manifest.json"),
        "export": export,
    })


def queue_gemini_review(session_id: str) -> Dict[str, Any]:
    manifest = build_review_manifest(session_id)
    return _save_job(session_id, {
        "mode": "gemini",
        "status": "queued",
        "queued_at_ms": _now_ms(),
        "total_examples": manifest.get("accepted_count", 0),
        "reviewed_examples": 0,
        "manifest_path": str(_job_root(session_id) / "review_manifest.json"),
        "started_at_ms": None,
        "completed_at_ms": None,
        "error": None,
        "export": None,
        "message": "Gemini review will run after the live session.",
    })


def get_review_status(session_id: str) -> Dict[str, Any]:
    job = _load_job(session_id)
    root = _job_root(session_id)
    manifest_path = root / "review_manifest.json"
    if manifest_path.exists():
        job["manifest_path"] = str(manifest_path)
    job["job_root"] = str(root)
    job["dataset_root"] = str(_dataset_root(session_id))
    return job
