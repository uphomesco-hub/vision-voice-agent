import asyncio
import base64
import collections
import io
import json
import os
import re
import threading
import time
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
from PIL import Image

try:
    import cv2
except Exception:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except Exception:
    pass

GROUNDING_MODEL = os.environ.get("HUD_GROUNDING_MODEL", "gemini-2.5-flash")


def _gemini_api_key() -> str:
    return os.environ.get("GOOGLE_API_KEY", "")


def _grounding_url() -> str:
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GROUNDING_MODEL}:generateContent"
        f"?key={_gemini_api_key()}"
    )

DEFAULT_EXPIRES_MS = 120000
MAX_MARKERS = 8
HUD_DATASET_DIR = Path(os.environ.get("HUD_DATASET_DIR", os.path.join(os.path.dirname(__file__), "hud_dataset")))
HUD_CAPTURE_FRAMES = os.environ.get("HUD_CAPTURE_FRAMES", "1") != "0"
SAM2_CHECKPOINT = os.environ.get(
    "HUD_SAM2_CHECKPOINT",
    os.path.join(os.path.dirname(__file__), "models", "sam2.1_hiera_tiny.pt"),
)
SAM2_CONFIG = os.environ.get("HUD_SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_t.yaml")
SAM2_ENABLED = os.environ.get("HUD_SAM2_ENABLED", "1") != "0"
SAM2_VOS_OPTIMIZED = os.environ.get("HUD_SAM2_VOS_OPTIMIZED", "0") == "1"
GROUNDING_BACKEND = os.environ.get("HUD_GROUNDING_BACKEND", "grounding_dino").lower()
GROUNDING_DINO_MODEL = os.environ.get("HUD_GROUNDING_DINO_MODEL", "IDEA-Research/grounding-dino-tiny")
FLORENCE2_MODEL = os.environ.get("HUD_FLORENCE2_MODEL", "microsoft/Florence-2-base-ft")
FLORENCE2_ENABLED = os.environ.get("HUD_FLORENCE2_ENABLED", "1") != "0"
FLORENCE2_FALLBACK = os.environ.get("HUD_FLORENCE2_FALLBACK", "1") != "0"
DINOX_ENABLED = os.environ.get("HUD_DINOX_ENABLED", "0") == "1"
DINOX_MODEL = os.environ.get("HUD_DINOX_MODEL", "DINO-X-1.0")
DINOX_API_TOKEN = os.environ.get("DINOX_API_TOKEN") or os.environ.get("DDS_API_TOKEN") or os.environ.get("DINOX_API_KEY", "")
GROUNDING_BOX_THRESHOLD = float(os.environ.get("HUD_GROUNDING_BOX_THRESHOLD", "0.28"))
GROUNDING_TEXT_THRESHOLD = float(os.environ.get("HUD_GROUNDING_TEXT_THRESHOLD", "0.22"))
HUD_TILED_GROUNDING = os.environ.get("HUD_TILED_GROUNDING", "1") != "0"
HUD_TILE_SIZE = int(os.environ.get("HUD_TILE_SIZE", "512"))
HUD_TILE_OVERLAP = float(os.environ.get("HUD_TILE_OVERLAP", "0.25"))
GROUNDED_SAM2_ENABLED = os.environ.get("HUD_GROUNDED_SAM2_ENABLED", "1") != "0"
GROUNDED_SAM2_MAX_CANDIDATES = int(os.environ.get("HUD_GROUNDED_SAM2_MAX_CANDIDATES", "4"))
REPAIR_DETECTOR_MODEL = os.environ.get("HUD_REPAIR_DETECTOR_MODEL", "")

_sam2_lock = threading.Lock()
_sam2_predictor = None
_sam2_video_predictor = None
_sam2_device = None
_sam2_error = None
_sam2_video_error = None
_grounding_lock = threading.Lock()
_grounding_processor = None
_grounding_model = None
_grounding_device = None
_grounding_error = None
_florence_processor = None
_florence_model = None
_florence_device = None
_florence_error = None
_dinox_error = None
_repair_detector = None
_repair_detector_error = None

HIGHLIGHT_DECL = {
    "name": "highlight",
    "description": (
        "Create or update a live heads-up display marker attached to a visible part, tool, label, "
        "screw, connector, seam, pull tab, latch, or motion path. Use before speaking when the user "
        "asks where something is, asks how to move/pull/open/unscrew it, or needs visual guidance."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "target_hint": {
                "type": "STRING",
                "description": "The exact visible target to mark, e.g. 'coin-slot end cap', 'left Torx screw', 'battery pull tab'.",
            },
            "label": {
                "type": "STRING",
                "description": "Short on-screen label, 1-4 words.",
            },
            "action_type": {
                "type": "STRING",
                "description": "One of inspect, press, pull, lift, slide, rotate_cw, rotate_ccw, unscrew, pry, hold_here, warning.",
            },
            "direction": {
                "type": "STRING",
                "description": "Short direction if relevant, e.g. left, right, up, down, clockwise, counterclockwise, toward you.",
            },
            "geometry": {
                "type": "OBJECT",
                "description": "Optional normalized geometry if the target is visually obvious.",
                "properties": {
                    "type": {"type": "STRING", "description": "box or point"},
                    "x": {"type": "NUMBER", "description": "Normalized left/x, 0-1"},
                    "y": {"type": "NUMBER", "description": "Normalized top/y, 0-1"},
                    "width": {"type": "NUMBER", "description": "Normalized width for box"},
                    "height": {"type": "NUMBER", "description": "Normalized height for box"},
                },
            },
            "marker_id": {
                "type": "STRING",
                "description": "Stable id when updating an existing marker.",
            },
            "expires_ms": {
                "type": "NUMBER",
                "description": "How long this marker should remain visible. Default 14000.",
            },
            "priority": {
                "type": "NUMBER",
                "description": "1 low, 5 urgent.",
            },
        },
        "required": ["target_hint"],
    },
}

HUD_ADJUST_DECL = {
    "name": "adjust_marker",
    "description": (
        "Update an existing HUD marker after the user says it is wrong, asks to lock it, move it, "
        "resize it, mark this instead, or remove background from it. Use this instead of repeating "
        "highlight when a marker already exists."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "marker_id": {"type": "STRING", "description": "Existing marker id. If omitted, use the highest-priority active marker."},
            "operation": {
                "type": "STRING",
                "description": "One of lock, unlock, move, resize, positive_point, negative_point, relock, remove.",
            },
            "point": {
                "type": "OBJECT",
                "description": "Normalized point for positive_point or negative_point.",
                "properties": {"x": {"type": "NUMBER"}, "y": {"type": "NUMBER"}},
            },
            "geometry": {
                "type": "OBJECT",
                "description": "Normalized geometry for move/resize/relock.",
                "properties": {
                    "type": {"type": "STRING"},
                    "x": {"type": "NUMBER"},
                    "y": {"type": "NUMBER"},
                    "width": {"type": "NUMBER"},
                    "height": {"type": "NUMBER"},
                },
            },
            "target_hint": {"type": "STRING", "description": "Optional target clarification when relocking."},
        },
        "required": ["operation"],
    },
}

HUD_VERIFY_DECL = {
    "name": "verify_marker",
    "description": (
        "Ask vision to judge whether a HUD marker is correctly placed on the requested object or part. "
        "Use when the user asks if the marker is right, says it is wrong, or asks you to mark again more accurately."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "marker_id": {"type": "STRING", "description": "Existing marker id. If omitted, verify the highest-priority marker."},
            "target_hint": {"type": "STRING", "description": "What the marker should be on."},
            "apply_suggestion": {"type": "BOOLEAN", "description": "Whether to apply a suggested corrected geometry if confidence is good."},
        },
        "required": [],
    },
}

INSPECT_FRAME_DECL = {
    "name": "inspect_current_frame",
    "description": (
        "Inspect the latest camera frame immediately. Use before answering visual questions like "
        "'what do you see', 'what is in my hand', 'can you see it now', 'what device is this', or "
        "before marking if the visible target is uncertain."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "The user's visual question or target to inspect."},
            "focus": {"type": "STRING", "description": "Optional focus area such as hand, device, label, screw, case."},
        },
        "required": [],
    },
}

_hud_runtime: Dict[str, Dict[str, Any]] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_state(session_id: str) -> Dict[str, Any]:
    if session_id not in _hud_runtime:
        _hud_runtime[session_id] = {
            "latest_frame": None,
            "latest_frame_at": 0,
            "frame_index": 0,
            "frame_buffer": [],
            "markers": {},
            "version": 0,
            "mask_refreshing": False,
            "next_object_id": 1,
            "sam2_streams": {},
        }
    return _hud_runtime[session_id]


def clear_hud_runtime(session_id: str) -> None:
    _hud_runtime.pop(session_id, None)


def _dataset_dir(session_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id)
    path = HUD_DATASET_DIR / safe_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "frames").mkdir(exist_ok=True)
    return path


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _log_hud_event(session_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    entry = {
        "at_ms": _now_ms(),
        "session_id": session_id,
        "event_type": event_type,
        **payload,
    }
    _append_jsonl(_dataset_dir(session_id) / "events.jsonl", entry)


def _store_frame_sample(session_id: str, frame_index: int, raw_b64: str, reason: str = "frame") -> Optional[str]:
    if not HUD_CAPTURE_FRAMES or not raw_b64:
        return None
    try:
        data = base64.b64decode(raw_b64)
        rel = f"frames/{int(frame_index):06d}.jpg"
        path = _dataset_dir(session_id) / rel
        if not path.exists():
            path.write_bytes(data)
        _append_jsonl(_dataset_dir(session_id) / "frames.jsonl", {
            "at_ms": _now_ms(),
            "session_id": session_id,
            "frame_index": int(frame_index),
            "reason": reason,
            "path": rel,
            "bytes": len(data),
        })
        return rel
    except Exception:
        return None


def _next_object_id(state: Dict[str, Any]) -> int:
    oid = int(state.get("next_object_id", 1) or 1)
    state["next_object_id"] = oid + 1
    return oid


def set_latest_frame(session_id: str, raw_b64: str) -> None:
    state = _session_state(session_id)
    state["frame_index"] = int(state.get("frame_index", 0) or 0) + 1
    state["latest_frame"] = raw_b64
    state["latest_frame_at"] = _now_ms()
    state["frame_buffer"].append({
        "index": state["frame_index"],
        "data": raw_b64,
        "at_ms": state["latest_frame_at"],
    })
    if len(state["frame_buffer"]) > 18:
        state["frame_buffer"] = state["frame_buffer"][-18:]
    _store_frame_sample(session_id, state["frame_index"], raw_b64, reason="camera_tick")


def _clamp01(value: Any, fallback: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def _normal_geometry(geometry: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not isinstance(geometry, dict):
        return None
    gtype = str(geometry.get("type") or "box").lower()
    x = _clamp01(geometry.get("x"), 0.45)
    y = _clamp01(geometry.get("y"), 0.45)
    if gtype == "point":
        return {"type": "point", "x": x, "y": y, "width": 0.035, "height": 0.035}
    width = max(0.025, min(0.7, _clamp01(geometry.get("width"), 0.14)))
    height = max(0.025, min(0.7, _clamp01(geometry.get("height"), 0.12)))
    return {
        "type": "box",
        "x": min(x, 1.0 - width),
        "y": min(y, 1.0 - height),
        "width": width,
        "height": height,
    }


def _default_geometry(target_hint: str) -> Dict[str, float]:
    hint = (target_hint or "").lower()
    if any(word in hint for word in ("left", "hinge", "latch")):
        return {"type": "box", "x": 0.2, "y": 0.35, "width": 0.18, "height": 0.16}
    if any(word in hint for word in ("right", "pull", "tab", "handle")):
        return {"type": "box", "x": 0.62, "y": 0.35, "width": 0.18, "height": 0.16}
    return {"type": "box", "x": 0.41, "y": 0.36, "width": 0.18, "height": 0.16}


def _anchor_from_geometry(geometry: Dict[str, float], candidate: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    if isinstance(candidate, dict):
        return {"x": _clamp01(candidate.get("x"), geometry["x"] + geometry["width"] / 2), "y": _clamp01(candidate.get("y"), geometry["y"] + geometry["height"] / 2)}
    return {"x": geometry["x"] + geometry["width"] / 2, "y": geometry["y"] + geometry["height"] / 2}


def _rectangle_contour(geometry: Dict[str, float]) -> List[Dict[str, float]]:
    x, y = geometry["x"], geometry["y"]
    w, h = geometry["width"], geometry["height"]
    return [
        {"x": x, "y": y},
        {"x": x + w, "y": y},
        {"x": x + w, "y": y + h},
        {"x": x, "y": y + h},
    ]


def _decode_frame(raw_b64: str) -> Optional[np.ndarray]:
    try:
        data = base64.b64decode(raw_b64)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return np.array(img)
    except Exception:
        return None


def _normalize_contour(points: np.ndarray, width: int, height: int, max_points: int = 96) -> List[Dict[str, float]]:
    if points is None or len(points) < 3:
        return []
    pts = points.reshape(-1, 2)
    if len(pts) > max_points:
        step = max(1, int(np.ceil(len(pts) / max_points)))
        pts = pts[::step]
    return [
        {"x": round(float(np.clip(x / width, 0, 1)), 5), "y": round(float(np.clip(y / height, 0, 1)), 5)}
        for x, y in pts
    ]


def _bbox_from_contour(contour: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not contour:
        return None
    xs = [float(p.get("x", 0)) for p in contour]
    ys = [float(p.get("y", 0)) for p in contour]
    min_x, max_x = max(0.0, min(xs)), min(1.0, max(xs))
    min_y, max_y = max(0.0, min(ys)), min(1.0, max(ys))
    width = max(0.015, max_x - min_x)
    height = max(0.015, max_y - min_y)
    return {"type": "box", "x": min_x, "y": min_y, "width": width, "height": height}


def _geometry_area(geometry: Optional[Dict[str, float]]) -> float:
    g = _normal_geometry(geometry)
    if not g:
        return 0.0
    return max(0.0, float(g["width"]) * float(g["height"]))


def _center_distance(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]]) -> float:
    ga = _normal_geometry(a)
    gb = _normal_geometry(b)
    if not ga or not gb:
        return 1.0
    ax, ay = ga["x"] + ga["width"] / 2, ga["y"] + ga["height"] / 2
    bx, by = gb["x"] + gb["width"] / 2, gb["y"] + gb["height"] / 2
    return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


def _geometry_iou(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]]) -> float:
    ga = _normal_geometry(a)
    gb = _normal_geometry(b)
    if not ga or not gb:
        return 0.0
    ax1, ay1 = ga["x"], ga["y"]
    ax2, ay2 = ga["x"] + ga["width"], ga["y"] + ga["height"]
    bx1, by1 = gb["x"], gb["y"]
    bx2, by2 = gb["x"] + gb["width"], gb["y"] + gb["height"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = _geometry_area(ga) + _geometry_area(gb) - inter
    return float(inter / union) if union > 0 else 0.0


def _is_tiny_target(label: str, target_hint: str) -> bool:
    text = f"{label or ''} {target_hint or ''}".lower()
    return any(word in text for word in (
        "screw", "bolt", "pin", "hole", "port", "connector", "plug", "clip",
        "tab", "notch", "latch", "dot", "button", "contact", "terminal",
    ))


def _marker_type_for(label: str, target_hint: str, geometry: Optional[Dict[str, float]]) -> str:
    g = _normal_geometry(geometry)
    if _is_tiny_target(label, target_hint):
        return "point"
    if g and _geometry_area(g) < 0.006:
        return "point"
    return "contour"


def _point_geometry_from_box(geometry: Dict[str, float]) -> Dict[str, float]:
    g = _normal_geometry(geometry) or _default_geometry("")
    size = max(0.018, min(0.055, max(g["width"], g["height"]) * 0.55))
    cx = g["x"] + g["width"] / 2
    cy = g["y"] + g["height"] / 2
    return {"type": "point", "x": max(0.0, min(1.0 - size, cx - size / 2)), "y": max(0.0, min(1.0 - size, cy - size / 2)), "width": size, "height": size}


def _negative_prompt_ring(geometry: Dict[str, float], positives: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Add background points around a box so a positive-only correction does not balloon."""
    if not positives:
        return []
    g = _normal_geometry(geometry)
    if not g:
        return []
    pad_x = max(0.018, g["width"] * 0.22)
    pad_y = max(0.018, g["height"] * 0.22)
    cx = g["x"] + g["width"] / 2
    cy = g["y"] + g["height"] / 2
    candidates = [
        {"x": g["x"] - pad_x, "y": cy},
        {"x": g["x"] + g["width"] + pad_x, "y": cy},
        {"x": cx, "y": g["y"] - pad_y},
        {"x": cx, "y": g["y"] + g["height"] + pad_y},
    ]
    return [{"x": _clamp01(p["x"], cx), "y": _clamp01(p["y"], cy), "label": 0} for p in candidates]


def _initial_prompt_points(geometry: Dict[str, float], label: str = "", target_hint: str = "") -> List[Dict[str, float]]:
    g = _normal_geometry(geometry)
    if not g:
        return []
    cx = g["x"] + g["width"] / 2
    cy = g["y"] + g["height"] / 2
    positives = [{"x": cx, "y": cy, "label": 1}]
    if not _is_tiny_target(label, target_hint) and _geometry_area(g) >= 0.008:
        positives.extend([
            {"x": g["x"] + g["width"] * 0.34, "y": cy, "label": 1},
            {"x": g["x"] + g["width"] * 0.66, "y": cy, "label": 1},
        ])
    return positives + _negative_prompt_ring(g, positives)


def _public_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    public = {}
    for key, value in candidate.items():
        if key == "segmentation":
            seg = value if isinstance(value, dict) else {}
            public[key] = {k: v for k, v in seg.items() if k != "contour"}
        elif key not in ("mask_contour", "contour"):
            public[key] = value
    return public


def _refresh_is_safe(marker: Dict[str, Any], new_geometry: Dict[str, float], segmentation: Dict[str, Any]):
    old_geometry = _normal_geometry(marker.get("geometry"))
    seed_geometry = _normal_geometry(marker.get("seed_geometry") or marker.get("model_geometry"))
    if not old_geometry:
        return True, "no_prior"
    old_area = max(_geometry_area(old_geometry), 1e-6)
    new_area = max(_geometry_area(new_geometry), 1e-6)
    area_ratio = new_area / old_area
    center_jump = _center_distance(old_geometry, new_geometry)
    seed_jump = _center_distance(seed_geometry, new_geometry) if seed_geometry else 0.0
    iou = _geometry_iou(old_geometry, new_geometry)
    provider = str(segmentation.get("provider") or "")

    if area_ratio > 1.85:
        return False, f"area_growth_{area_ratio:.2f}"
    if area_ratio < 0.34 and provider == "sam2_video":
        return False, f"area_shrink_{area_ratio:.2f}"
    if center_jump > 0.14:
        return False, f"center_jump_{center_jump:.3f}"
    if seed_geometry and seed_jump > 0.22:
        return False, f"seed_jump_{seed_jump:.3f}"
    if iou < 0.18 and center_jump > 0.035 and provider in ("sam2", "sam2_video", "sam2_stream"):
        return False, f"low_iou_{iou:.2f}"
    return True, "accepted"


def _segment_contour_from_box(raw_b64: Optional[str], geometry: Dict[str, float]) -> Dict[str, Any]:
    sam2_result = _segment_contour_sam2(raw_b64, geometry)
    if sam2_result.get("contour"):
        return sam2_result
    if not raw_b64 or cv2 is None:
        return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}
    img = _decode_frame(raw_b64)
    if img is None:
        return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}

    height, width = img.shape[:2]
    x = int(np.clip(geometry["x"] * width, 0, width - 2))
    y = int(np.clip(geometry["y"] * height, 0, height - 2))
    w = int(np.clip(geometry["width"] * width, 8, width - x))
    h = int(np.clip(geometry["height"] * height, 8, height - y))

    pad_x = max(6, int(w * 0.16))
    pad_y = max(6, int(h * 0.16))
    rx = max(0, x - pad_x)
    ry = max(0, y - pad_y)
    rw = min(width - rx, w + pad_x * 2)
    rh = min(height - ry, h + pad_y * 2)
    if rw < 12 or rh < 12:
        return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}

    mask = np.zeros((height, width), np.uint8)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(img, mask, (rx, ry, rw, rh), bgd_model, fgd_model, 4, cv2.GC_INIT_WITH_RECT)
        binary = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype("uint8")
        kernel = np.ones((3, 3), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}
        cx = x + w / 2
        cy = y + h / 2
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 18:
                continue
            m = cv2.moments(contour)
            if m["m00"]:
                ccx = m["m10"] / m["m00"]
                ccy = m["m01"] / m["m00"]
            else:
                bx, by, bw, bh = cv2.boundingRect(contour)
                ccx, ccy = bx + bw / 2, by + bh / 2
            dist = np.hypot(ccx - cx, ccy - cy)
            candidates.append((area - dist * 1.8, area, contour))
        if not candidates:
            return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}
        _, area, contour = max(candidates, key=lambda item: item[0])
        epsilon = max(1.2, 0.006 * cv2.arcLength(contour, True))
        approx = cv2.approxPolyDP(contour, epsilon, True)
        contour_points = _normalize_contour(approx, width, height)
        box_area = max(1, w * h)
        fill_ratio = float(np.clip(area / box_area, 0.05, 1.0))
        return {
            "provider": "opencv_grabcut",
            "contour": contour_points or _rectangle_contour(geometry),
            "confidence": round(0.45 + fill_ratio * 0.35, 3),
            "fill_ratio": round(fill_ratio, 3),
        }
    except Exception:
        return {"provider": "rectangle_fallback", "contour": _rectangle_contour(geometry), "confidence": 0.25}


def _get_grounding_dino():
    global _grounding_processor, _grounding_model, _grounding_device, _grounding_error
    if _grounding_error:
        return None, None
    if _grounding_processor is not None and _grounding_model is not None:
        return _grounding_processor, _grounding_model
    with _grounding_lock:
        if _grounding_processor is not None and _grounding_model is not None:
            return _grounding_processor, _grounding_model
        try:
            import torch
            from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

            _grounding_device = "mps" if torch.backends.mps.is_available() else "cpu"
            _grounding_processor = AutoProcessor.from_pretrained(GROUNDING_DINO_MODEL)
            _grounding_model = AutoModelForZeroShotObjectDetection.from_pretrained(GROUNDING_DINO_MODEL).to(_grounding_device)
            _grounding_model.eval()
            return _grounding_processor, _grounding_model
        except Exception as exc:
            _grounding_error = str(exc)
            return None, None


def _get_florence2():
    global _florence_processor, _florence_model, _florence_device, _florence_error
    if not FLORENCE2_ENABLED or _florence_error:
        return None, None
    if _florence_processor is not None and _florence_model is not None:
        return _florence_processor, _florence_model
    with _grounding_lock:
        if _florence_processor is not None and _florence_model is not None:
            return _florence_processor, _florence_model
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            _florence_device = "mps" if torch.backends.mps.is_available() else "cpu"
            _florence_processor = AutoProcessor.from_pretrained(FLORENCE2_MODEL, trust_remote_code=True)
            _florence_model = AutoModelForCausalLM.from_pretrained(
                FLORENCE2_MODEL,
                trust_remote_code=True,
            ).to(_florence_device)
            _florence_model.eval()
            return _florence_processor, _florence_model
        except Exception as exc:
            _florence_error = str(exc)
            return None, None


def _get_repair_detector():
    global _repair_detector, _repair_detector_error
    if not REPAIR_DETECTOR_MODEL or _repair_detector_error:
        return None
    if _repair_detector is not None:
        return _repair_detector
    try:
        from ultralytics import YOLO

        _repair_detector = YOLO(REPAIR_DETECTOR_MODEL)
        return _repair_detector
    except Exception as exc:
        _repair_detector_error = str(exc)
        return None


def _candidate_from_box(box, label: str, score: float, image_width: int, image_height: int, source: str) -> Dict[str, Any]:
    x1, y1, x2, y2 = [float(v) for v in box]
    x1, y1 = max(0.0, min(image_width - 1, x1)), max(0.0, min(image_height - 1, y1))
    x2, y2 = max(x1 + 1.0, min(image_width, x2)), max(y1 + 1.0, min(image_height, y2))
    return {
        "label": str(label or "").strip()[:48],
        "score": round(float(score), 4),
        "source": source,
        "geometry": {
            "type": "box",
            "x": round(x1 / image_width, 5),
            "y": round(y1 / image_height, 5),
            "width": round((x2 - x1) / image_width, 5),
            "height": round((y2 - y1) / image_height, 5),
        },
    }


def _dedupe_candidates(candidates: List[Dict[str, Any]], max_count: int = 8) -> List[Dict[str, Any]]:
    ordered = sorted(candidates, key=lambda c: float(c.get("score", 0) or 0), reverse=True)
    kept: List[Dict[str, Any]] = []
    for cand in ordered:
        geom = _normal_geometry(cand.get("geometry"))
        if not geom:
            continue
        if any(_geometry_iou(geom, existing.get("geometry")) > 0.72 for existing in kept):
            continue
        kept.append({**cand, "geometry": geom})
        if len(kept) >= max_count:
            break
    return kept


def _run_grounding_dino_on_image(img: Image.Image, text: str, source: str = "grounding_dino") -> List[Dict[str, Any]]:
    processor, model = _get_grounding_dino()
    if processor is None or model is None:
        return []
    try:
        import torch

        query = (text or "object").strip().lower()
        if not query.endswith("."):
            query += "."
        inputs = processor(images=img, text=query, return_tensors="pt").to(_grounding_device)
        with torch.inference_mode():
            outputs = model(**inputs)
        results = processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            box_threshold=GROUNDING_BOX_THRESHOLD,
            text_threshold=GROUNDING_TEXT_THRESHOLD,
            target_sizes=[img.size[::-1]],
        )[0]
        boxes = results.get("boxes", [])
        scores = results.get("scores", [])
        labels = results.get("labels", [])
        return [
            _candidate_from_box(box, label, score, img.width, img.height, source)
            for box, score, label in zip(boxes, scores, labels)
        ]
    except Exception as exc:
        return [{"source": source, "error": str(exc), "score": 0.0}]


def _run_florence2_on_image(img: Image.Image, text: str, source: str = "florence2") -> List[Dict[str, Any]]:
    processor, model = _get_florence2()
    if processor is None or model is None:
        return []
    try:
        import torch

        query = (text or "object").strip()

        def run_task(task_prompt: str, text_input: str = "") -> Dict[str, Any]:
            prompt = task_prompt + text_input
            inputs = processor(text=prompt, images=img, return_tensors="pt").to(_florence_device)
            with torch.inference_mode():
                generated_ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=768,
                    num_beams=3,
                    do_sample=False,
                )
            generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
            parsed = processor.post_process_generation(generated_text, task=task_prompt, image_size=(img.width, img.height))
            return parsed.get(task_prompt, {}) if isinstance(parsed, dict) else {}

        candidates: List[Dict[str, Any]] = []
        for task_prompt, text_input, base_score in (
            ("<CAPTION_TO_PHRASE_GROUNDING>", query, 0.66),
            ("<OD>", "", 0.54),
            ("<DENSE_REGION_CAPTION>", "", 0.5),
        ):
            result = run_task(task_prompt, text_input)
            boxes = result.get("bboxes", []) or []
            labels = result.get("labels", []) or []
            for idx, box in enumerate(boxes):
                label = str(labels[idx] if idx < len(labels) else query).strip() or query
                query_words = set(query.lower().replace("-", " ").split())
                label_words = set(label.lower().replace("-", " ").split())
                overlap = len(query_words & label_words)
                score = base_score + min(0.28, overlap * 0.09)
                if task_prompt != "<CAPTION_TO_PHRASE_GROUNDING>" and query_words and overlap == 0:
                    score -= 0.18
                candidates.append(_candidate_from_box(box, label, score, img.width, img.height, source))
            if candidates and task_prompt == "<CAPTION_TO_PHRASE_GROUNDING>":
                break
        return _dedupe_candidates(candidates, max_count=8)
    except Exception as exc:
        return [{"source": source, "error": str(exc), "score": 0.0}]


def _run_dinox_on_image(img: Image.Image, text: str, source: str = "dinox") -> List[Dict[str, Any]]:
    global _dinox_error
    if not DINOX_ENABLED or not DINOX_API_TOKEN:
        return []
    try:
        from dds_cloudapi_sdk import Client, Config
        from dds_cloudapi_sdk.tasks.v2_task import V2Task

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=92)
        image_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        prompt_text = (text or "object").strip()
        if prompt_text and not prompt_text.endswith("."):
            prompt_text += " ."
        task = V2Task(
            api_path="/v2/task/dinox/detection",
            api_body={
                "model": DINOX_MODEL,
                "image": image_b64,
                "prompt": {"type": "text", "text": prompt_text},
                "mask_format": "coco_rle",
                "targets": ["bbox", "mask"],
                "bbox_threshold": max(0.2, GROUNDING_BOX_THRESHOLD),
                "iou_threshold": 0.8,
            },
        )
        client = Client(Config(DINOX_API_TOKEN))
        client.run_task(task)
        objects = (task.result or {}).get("objects", []) if isinstance(task.result, dict) else []
        candidates = []
        for obj in objects:
            box = obj.get("bbox")
            if not box:
                continue
            candidates.append(_candidate_from_box(
                box,
                obj.get("category") or text or "object",
                float(obj.get("score", 0.0) or 0.0),
                img.width,
                img.height,
                source,
            ))
        return candidates
    except Exception as exc:
        _dinox_error = str(exc)
        return [{"source": source, "error": str(exc), "score": 0.0}]


def _run_tiled_grounding(img: Image.Image, text: str) -> List[Dict[str, Any]]:
    if not HUD_TILED_GROUNDING:
        return []
    width, height = img.size
    if width <= HUD_TILE_SIZE and height <= HUD_TILE_SIZE:
        return []
    step = max(96, int(HUD_TILE_SIZE * (1.0 - HUD_TILE_OVERLAP)))
    candidates: List[Dict[str, Any]] = []
    for top in range(0, max(1, height), step):
        if top >= height:
            break
        for left in range(0, max(1, width), step):
            if left >= width:
                break
            right = min(width, left + HUD_TILE_SIZE)
            bottom = min(height, top + HUD_TILE_SIZE)
            if right - left < 96 or bottom - top < 96:
                continue
            tile = img.crop((left, top, right, bottom))
            for cand in _run_grounding_dino_on_image(tile, text, source="grounding_dino_sahi_tile"):
                geom = _normal_geometry(cand.get("geometry"))
                if not geom:
                    continue
                cand["geometry"] = {
                    "type": "box",
                    "x": round((left + geom["x"] * tile.width) / width, 5),
                    "y": round((top + geom["y"] * tile.height) / height, 5),
                    "width": round((geom["width"] * tile.width) / width, 5),
                    "height": round((geom["height"] * tile.height) / height, 5),
                }
                candidates.append(cand)
    return candidates


def _run_repair_detector(img: Image.Image, target_hint: str) -> List[Dict[str, Any]]:
    detector = _get_repair_detector()
    if detector is None:
        return []
    try:
        result = detector.predict(np.array(img), verbose=False)[0]
        names = getattr(result, "names", {}) or {}
        target_words = set((target_hint or "").lower().replace("-", " ").split())
        candidates = []
        for box in result.boxes:
            cls_id = int(box.cls[0])
            label = str(names.get(cls_id, cls_id))
            if target_words and not any(word in label.lower() for word in target_words):
                continue
            xyxy = box.xyxy[0].tolist()
            candidates.append(_candidate_from_box(xyxy, label, float(box.conf[0]), img.width, img.height, "repair_detector"))
        return candidates
    except Exception as exc:
        return [{"source": "repair_detector", "error": str(exc), "score": 0.0}]


def _grounded_sam2_candidate_score(
    candidate: Dict[str, Any],
    segmentation: Dict[str, Any],
    target_hint: str,
) -> float:
    geom = _normal_geometry(candidate.get("geometry"))
    contour = segmentation.get("contour") or []
    mask_box = _bbox_from_contour(contour)
    detector_score = float(candidate.get("score", 0) or 0)
    mask_score = float(segmentation.get("confidence", 0) or 0)
    iou = _geometry_iou(geom, mask_box)
    fill = float(segmentation.get("fill_ratio", 0.5) or 0.5)
    score = detector_score * 0.55 + mask_score * 0.32 + min(1.0, iou) * 0.13
    if fill > 2.1:
        score -= min(0.24, (fill - 2.1) * 0.08)
    if _is_tiny_target(candidate.get("label", ""), target_hint):
        area = _geometry_area(mask_box or geom)
        if area > 0.04:
            score -= 0.22
    return round(max(0.0, min(0.99, score)), 4)


def _refine_candidates_with_grounded_sam2(
    raw_b64: str,
    candidates: List[Dict[str, Any]],
    target_hint: str,
) -> List[Dict[str, Any]]:
    if not GROUNDED_SAM2_ENABLED or not raw_b64 or not candidates:
        return candidates
    refined: List[Dict[str, Any]] = []
    for cand in candidates[:max(1, GROUNDED_SAM2_MAX_CANDIDATES)]:
        geom = _normal_geometry(cand.get("geometry"))
        if not geom:
            continue
        seg = _segment_contour_sam2(
            raw_b64,
            geom,
            target_hint=target_hint,
            label=str(cand.get("label") or ""),
        )
        contour = seg.get("contour") or []
        refined_geom = _bbox_from_contour(contour) or geom
        if contour:
            cand = {
                **cand,
                "geometry": refined_geom,
                "detector_geometry": geom,
                "mask_contour": contour,
                "segmentation": {k: v for k, v in seg.items() if k != "contour"},
                "score": _grounded_sam2_candidate_score(cand, seg, target_hint),
                "source": f"{cand.get('source', 'detector')}+sam2",
            }
        else:
            cand = {
                **cand,
                "segmentation": {k: v for k, v in seg.items() if k != "contour"},
                "score": round(float(cand.get("score", 0) or 0) * 0.82, 4),
            }
        refined.append(cand)
    if len(candidates) > len(refined):
        refined.extend(candidates[len(refined):])
    return _dedupe_candidates(refined, max_count=8)


async def _ground_target_with_detector(raw_b64: str, args: Dict[str, Any]) -> Dict[str, Any]:
    img_arr = _decode_frame(raw_b64)
    if img_arr is None:
        return {"found": False, "source": "detector", "error": "decode failed"}
    img = Image.fromarray(img_arr)
    target = (args.get("target_hint") or args.get("label") or "object").strip()
    candidates: List[Dict[str, Any]] = []

    candidates.extend(await asyncio.to_thread(_run_repair_detector, img, target))
    if GROUNDING_BACKEND in ("dinox", "dino_x", "auto", "grounded"):
        candidates.extend(await asyncio.to_thread(_run_dinox_on_image, img, target))
    if GROUNDING_BACKEND in ("grounding_dino", "auto", "grounded", "dino"):
        candidates.extend(await asyncio.to_thread(_run_grounding_dino_on_image, img, target, "grounding_dino"))
        if _is_tiny_target(args.get("label", ""), target):
            candidates.extend(await asyncio.to_thread(_run_tiled_grounding, img, target))
    if GROUNDING_BACKEND in ("florence2", "florence", "auto", "grounded"):
        candidates.extend(await asyncio.to_thread(_run_florence2_on_image, img, target, "florence2"))
    elif FLORENCE2_FALLBACK and not any(c.get("geometry") and not c.get("error") for c in candidates):
        candidates.extend(await asyncio.to_thread(_run_florence2_on_image, img, target, "florence2_fallback"))

    candidates = [c for c in candidates if c.get("geometry") and not c.get("error")]
    candidates = _dedupe_candidates(candidates)
    candidates = await asyncio.to_thread(_refine_candidates_with_grounded_sam2, raw_b64, candidates, target)
    if not candidates:
        return {
            "found": False,
            "source": "detector",
            "candidates": [],
            "error": _grounding_error or _florence_error or _dinox_error or _repair_detector_error,
        }
    best = candidates[0]
    ambiguous = len(candidates) > 1 and abs(float(candidates[0].get("score", 0)) - float(candidates[1].get("score", 0))) < 0.08
    segmentation = best.get("segmentation") if isinstance(best.get("segmentation"), dict) else {}
    return {
        "found": True,
        "source": best.get("source", "detector"),
        "label": best.get("label") or target,
        "geometry": best.get("geometry"),
        "detector_geometry": best.get("detector_geometry"),
        "mask_contour": best.get("mask_contour") or [],
        "segmentation": {
            **segmentation,
            "provider": "grounded_sam2",
            "grounding_source": best.get("source", "detector"),
        } if segmentation else {},
        "anchor_point": _anchor_from_geometry(best.get("geometry")),
        "confidence": float(best.get("score", 0.45) or 0.45),
        "notes": "detector grounding",
        "candidates": [_public_candidate(c) for c in candidates],
        "ambiguous": ambiguous,
    }


def _get_sam2_predictor():
    global _sam2_predictor, _sam2_device, _sam2_error
    if not SAM2_ENABLED:
        return None
    if _sam2_error:
        return None
    if _sam2_predictor is not None:
        return _sam2_predictor
    if not os.path.exists(SAM2_CHECKPOINT):
        _sam2_error = f"checkpoint missing: {SAM2_CHECKPOINT}"
        return None
    with _sam2_lock:
        if _sam2_predictor is not None:
            return _sam2_predictor
        try:
            import torch
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor

            _sam2_device = "mps" if torch.backends.mps.is_available() else "cpu"
            model = build_sam2(SAM2_CONFIG, SAM2_CHECKPOINT, device=_sam2_device)
            _sam2_predictor = SAM2ImagePredictor(model)
            return _sam2_predictor
        except Exception as exc:
            _sam2_error = str(exc)
            return None


def _get_sam2_video_predictor():
    global _sam2_video_predictor, _sam2_device, _sam2_video_error
    if not SAM2_ENABLED:
        return None
    if _sam2_video_error:
        return None
    if _sam2_video_predictor is not None:
        return _sam2_video_predictor
    if not os.path.exists(SAM2_CHECKPOINT):
        _sam2_video_error = f"checkpoint missing: {SAM2_CHECKPOINT}"
        return None
    with _sam2_lock:
        if _sam2_video_predictor is not None:
            return _sam2_video_predictor
        try:
            import torch
            from sam2.build_sam import build_sam2_video_predictor

            _sam2_device = "mps" if torch.backends.mps.is_available() else "cpu"
            try:
                _sam2_video_predictor = build_sam2_video_predictor(
                    SAM2_CONFIG,
                    SAM2_CHECKPOINT,
                    device=_sam2_device,
                    vos_optimized=SAM2_VOS_OPTIMIZED,
                )
            except TypeError:
                _sam2_video_predictor = build_sam2_video_predictor(SAM2_CONFIG, SAM2_CHECKPOINT, device=_sam2_device)
            return _sam2_video_predictor
        except Exception as exc:
            _sam2_video_error = str(exc)
            return None


def _mask_to_contour(mask: np.ndarray, width: int, height: int, max_points: int = 140) -> List[Dict[str, float]]:
    if mask is None:
        return []
    mask = np.squeeze(mask)
    binary = (mask > 0).astype("uint8") * 255
    if cv2 is None:
        ys, xs = np.where(binary > 0)
        if len(xs) < 3:
            return []
        return _rectangle_contour({
            "type": "box",
            "x": float(xs.min() / width),
            "y": float(ys.min() / height),
            "width": float((xs.max() - xs.min()) / width),
            "height": float((ys.max() - ys.min()) / height),
        })
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    epsilon = max(1.0, 0.0045 * cv2.arcLength(contour, True))
    approx = cv2.approxPolyDP(contour, epsilon, True)
    return _normalize_contour(approx, width, height, max_points=max_points)


def _contour_to_mask(contour: List[Dict[str, float]], width: int, height: int) -> Optional[np.ndarray]:
    if cv2 is None or not contour or len(contour) < 3:
        return None
    pts = np.array([
        [int(np.clip(float(p.get("x", 0)) * width, 0, width - 1)), int(np.clip(float(p.get("y", 0)) * height, 0, height - 1))]
        for p in contour
    ], dtype=np.int32)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 1)
    return mask


def _sample_positive_points_from_contour(contour: List[Dict[str, float]], geometry: Dict[str, float], count: int = 3) -> List[Dict[str, float]]:
    bbox = _bbox_from_contour(contour) or geometry
    points = [{"x": bbox["x"] + bbox["width"] / 2, "y": bbox["y"] + bbox["height"] / 2}]
    if len(contour) >= 6:
        for p in contour[::max(1, len(contour) // max(1, count - 1))][: count - 1]:
            points.append({"x": _clamp01(p.get("x"), points[0]["x"]), "y": _clamp01(p.get("y"), points[0]["y"])})
    return points[:count]


def _segment_contour_sam2_video(
    frame_items: List[Dict[str, Any]],
    seed_geometry: Dict[str, float],
    seed_contour: Optional[List[Dict[str, float]]] = None,
    object_id: int = 1,
) -> Dict[str, Any]:
    predictor = _get_sam2_video_predictor()
    if predictor is None:
        return {"provider": "sam2_video_unavailable", "error": _sam2_video_error}
    if len(frame_items) < 2:
        return {"provider": "sam2_video_unavailable", "error": "need at least 2 frames"}
    try:
        import torch

        with tempfile.TemporaryDirectory(prefix="hud_sam2_") as tmpdir:
            first_img = None
            for i, item in enumerate(frame_items):
                img = _decode_frame(item["data"])
                if img is None:
                    continue
                if first_img is None:
                    first_img = img
                Image.fromarray(img).save(os.path.join(tmpdir, f"{i:05d}.jpg"), quality=92)
            if first_img is None:
                return {"provider": "sam2_video_unavailable", "error": "decode failed"}

            height, width = first_img.shape[:2]
            box = np.array([
                max(0, int(seed_geometry["x"] * width)),
                max(0, int(seed_geometry["y"] * height)),
                min(width - 1, int((seed_geometry["x"] + seed_geometry["width"]) * width)),
                min(height - 1, int((seed_geometry["y"] + seed_geometry["height"]) * height)),
            ], dtype=np.float32)
            center = np.array([[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]], dtype=np.float32)
            labels = np.array([1], dtype=np.int32)
            seed_mask = _contour_to_mask(seed_contour or [], width, height)
            with _sam2_lock:
                with torch.inference_mode():
                    state = predictor.init_state(tmpdir, offload_video_to_cpu=True, offload_state_to_cpu=True)
                    if seed_mask is not None and seed_mask.any():
                        predictor.add_new_mask(state, frame_idx=0, obj_id=object_id, mask=seed_mask)
                    else:
                        predictor.add_new_points_or_box(state, frame_idx=0, obj_id=object_id, points=center, labels=labels, box=box)
                    last_frame_idx = None
                    last_masks = None
                    last_obj_ids = None
                    for frame_idx, obj_ids, masks in predictor.propagate_in_video(state, start_frame_idx=0, max_frame_num_to_track=len(frame_items)):
                        last_frame_idx = frame_idx
                        last_obj_ids = obj_ids
                        last_masks = masks
            if last_masks is None:
                return {"provider": "sam2_video_empty"}
            final_img = _decode_frame(frame_items[min(int(last_frame_idx or 0), len(frame_items) - 1)]["data"])
            if final_img is None:
                final_img = first_img
            final_h, final_w = final_img.shape[:2]
            mask_arr = (last_masks[0] > 0).detach().cpu().numpy().astype("uint8")
            contour = _mask_to_contour(mask_arr, final_w, final_h)
            if len(contour) < 3:
                return {"provider": "sam2_video_empty", "device": _sam2_device}
            return {
                "provider": "sam2_video",
                "device": _sam2_device,
                "contour": contour,
                "confidence": 0.88,
                "contour_points": len(contour),
                "video_frames": len(frame_items),
                "last_frame_idx": int(last_frame_idx or 0),
                "prompt_type": "mask" if seed_mask is not None else "box_point",
                "object_ids": [int(x) for x in list(last_obj_ids or [])],
            }
    except Exception as exc:
        return {"provider": "sam2_video_error", "error": str(exc)}


def _segment_contour_sam2(
    raw_b64: Optional[str],
    geometry: Dict[str, float],
    point_coords: Optional[List[Dict[str, float]]] = None,
    point_labels: Optional[List[int]] = None,
    target_hint: str = "",
    label: str = "",
) -> Dict[str, Any]:
    if not raw_b64:
        return {"provider": "sam2_unavailable", "error": "no frame"}
    predictor = _get_sam2_predictor()
    if predictor is None:
        return {"provider": "sam2_unavailable", "error": _sam2_error}
    img = _decode_frame(raw_b64)
    if img is None:
        return {"provider": "sam2_unavailable", "error": "decode failed"}

    height, width = img.shape[:2]
    x1 = int(np.clip(geometry["x"] * width, 0, width - 2))
    y1 = int(np.clip(geometry["y"] * height, 0, height - 2))
    x2 = int(np.clip((geometry["x"] + geometry["width"]) * width, x1 + 2, width - 1))
    y2 = int(np.clip((geometry["y"] + geometry["height"]) * height, y1 + 2, height - 1))
    pad_x = max(4, int((x2 - x1) * 0.08))
    pad_y = max(4, int((y2 - y1) * 0.08))
    box = np.array([
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(width - 1, x2 + pad_x),
        min(height - 1, y2 + pad_y),
    ])
    try:
        import torch
        points_np = None
        labels_np = None
        prompt_points = point_coords
        prompt_labels = point_labels
        if not prompt_points:
            initial = _initial_prompt_points(geometry, label, target_hint)
            prompt_points = [{"x": p["x"], "y": p["y"]} for p in initial]
            prompt_labels = [int(p.get("label", 1)) for p in initial]
        if point_coords:
            points_np = np.array([[float(p["x"]) * width, float(p["y"]) * height] for p in prompt_points], dtype=np.float32)
            labels_np = np.array(prompt_labels or [1] * len(prompt_points), dtype=np.int32)
        elif prompt_points:
            points_np = np.array([[float(p["x"]) * width, float(p["y"]) * height] for p in prompt_points], dtype=np.float32)
            labels_np = np.array(prompt_labels or [1] * len(prompt_points), dtype=np.int32)

        with _sam2_lock:
            with torch.inference_mode():
                predictor.set_image(img)
                masks, scores, _ = predictor.predict(
                    point_coords=points_np,
                    point_labels=labels_np,
                    box=box,
                    multimask_output=True,
                )
        if masks is None or len(masks) == 0:
            return {"provider": "sam2_empty"}
        best_idx = int(np.argmax(scores))
        contour_points = _mask_to_contour(masks[best_idx], width, height)
        if not contour_points:
            return {"provider": "sam2_empty", "score": float(scores[best_idx])}
        mask_pixels = float(np.asarray(masks[best_idx] > 0).sum())
        box_pixels = max(1.0, float((box[2] - box[0]) * (box[3] - box[1])))
        fill_ratio = float(np.clip(mask_pixels / box_pixels, 0.0, 4.0))
        return {
            "provider": "sam2",
            "device": _sam2_device,
            "contour": contour_points,
            "confidence": round(float(scores[best_idx]), 3),
            "contour_points": len(contour_points),
            "prompt_points": len(prompt_points or []),
            "prompt_type": "box_points_negative_ring" if prompt_points else "box",
            "fill_ratio": round(fill_ratio, 3),
        }
    except Exception as exc:
        return {"provider": "sam2_error", "error": str(exc)}


def _sam2_preprocess_frame(img: np.ndarray):
    import torch
    import torch.nn.functional as F

    predictor = _get_sam2_video_predictor()
    if predictor is None:
        return None
    image_size = int(getattr(predictor, "image_size", 1024))
    tensor = torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
    tensor = tensor.unsqueeze(0)
    tensor = F.interpolate(tensor, size=(image_size, image_size), mode="bilinear", align_corners=False)
    mean = torch.tensor([0.485, 0.456, 0.406], dtype=tensor.dtype).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225], dtype=tensor.dtype).view(1, 3, 1, 1)
    tensor = (tensor - mean) / std
    return tensor.to(_sam2_device)


def _sam2_stream_init(raw_b64: str, marker: Dict[str, Any]) -> Dict[str, Any]:
    predictor = _get_sam2_video_predictor()
    if predictor is None:
        return {"provider": "sam2_stream_unavailable", "error": _sam2_video_error}
    img = _decode_frame(raw_b64)
    if img is None:
        return {"provider": "sam2_stream_unavailable", "error": "decode failed"}
    contour = marker.get("model_contour") or marker.get("mask_contour") or []
    geometry = _normal_geometry(marker.get("seed_geometry") or marker.get("model_geometry") or marker.get("geometry"))
    if not geometry:
        return {"provider": "sam2_stream_unavailable", "error": "missing geometry"}
    height, width = img.shape[:2]
    mask = _contour_to_mask(contour, width, height)
    if mask is None:
        mask = _contour_to_mask(_rectangle_contour(geometry), width, height)
    if mask is None:
        return {"provider": "sam2_stream_unavailable", "error": "missing mask"}

    try:
        import torch
        import torch.nn.functional as F

        image = _sam2_preprocess_frame(img)
        if image is None:
            return {"provider": "sam2_stream_unavailable", "error": _sam2_video_error}
        image_size = int(getattr(predictor, "image_size", 1024))
        mask_tensor = torch.from_numpy(mask).float().view(1, 1, height, width)
        mask_tensor = F.interpolate(mask_tensor, size=(image_size, image_size), mode="bilinear", align_corners=False)
        mask_tensor = mask_tensor.to(_sam2_device)
        with _sam2_lock:
            with torch.inference_mode():
                feats = predictor.forward_image(image)
                _, vision_feats, vision_pos_embeds, feat_sizes = predictor._prepare_backbone_features(feats)
                if getattr(predictor, "directly_add_no_mem_embed", False):
                    vision_feats[-1] = vision_feats[-1] + predictor.no_mem_embed
                current_out = predictor.track_step(
                    frame_idx=0,
                    is_init_cond_frame=True,
                    current_vision_feats=vision_feats,
                    current_vision_pos_embeds=vision_pos_embeds,
                    feat_sizes=feat_sizes,
                    point_inputs=None,
                    mask_inputs=mask_tensor,
                    output_dict={},
                    num_frames=1,
                    run_mem_encoder=True,
                )
        return {
            "provider": "sam2_stream",
            "device": _sam2_device,
            "frame_idx": 0,
            "outputs": {
                "cond_frame_outputs": {0: current_out},
                "non_cond_frame_outputs": collections.OrderedDict(),
            },
        }
    except Exception as exc:
        return {"provider": "sam2_stream_error", "error": str(exc)}


def _sam2_stream_track(raw_b64: str, stream: Dict[str, Any]) -> Dict[str, Any]:
    predictor = _get_sam2_video_predictor()
    if predictor is None:
        return {"provider": "sam2_stream_unavailable", "error": _sam2_video_error}
    img = _decode_frame(raw_b64)
    if img is None:
        return {"provider": "sam2_stream_unavailable", "error": "decode failed"}
    try:
        import torch

        image = _sam2_preprocess_frame(img)
        if image is None:
            return {"provider": "sam2_stream_unavailable", "error": _sam2_video_error}
        frame_idx = int(stream.get("frame_idx", 0) or 0) + 1
        outputs = stream.get("outputs") or {"cond_frame_outputs": {}, "non_cond_frame_outputs": collections.OrderedDict()}
        with _sam2_lock:
            with torch.inference_mode():
                feats = predictor.forward_image(image)
                _, vision_feats, vision_pos_embeds, feat_sizes = predictor._prepare_backbone_features(feats)
                if getattr(predictor, "directly_add_no_mem_embed", False):
                    vision_feats[-1] = vision_feats[-1] + predictor.no_mem_embed
                current_out = predictor.track_step(
                    frame_idx=frame_idx,
                    is_init_cond_frame=False,
                    current_vision_feats=vision_feats,
                    current_vision_pos_embeds=vision_pos_embeds,
                    feat_sizes=feat_sizes,
                    point_inputs=None,
                    mask_inputs=None,
                    output_dict=outputs,
                    num_frames=frame_idx + 1,
                    run_mem_encoder=True,
                )
        non_cond = outputs.setdefault("non_cond_frame_outputs", collections.OrderedDict())
        non_cond[frame_idx] = current_out
        while len(non_cond) > int(getattr(predictor, "num_maskmem", 7) or 7):
            non_cond.popitem(last=False)
        stream["frame_idx"] = frame_idx
        stream["outputs"] = outputs
        mask_logits = current_out.get("pred_masks")
        if mask_logits is None:
            return {"provider": "sam2_stream_empty", "device": _sam2_device}
        contour = _mask_to_contour((mask_logits[0] > 0).detach().cpu().numpy().astype("uint8"), img.shape[1], img.shape[0])
        if len(contour) < 3:
            return {"provider": "sam2_stream_empty", "device": _sam2_device}
        return {
            "provider": "sam2_stream",
            "device": _sam2_device,
            "contour": contour,
            "confidence": 0.9,
            "contour_points": len(contour),
            "frame_idx": frame_idx,
            "prompt_type": "persistent_mask_memory",
        }
    except Exception as exc:
        return {"provider": "sam2_stream_error", "error": str(exc)}


def _vector_for(action_type: str, direction: str, candidate: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    if isinstance(candidate, dict):
        return {"dx": max(-0.35, min(0.35, float(candidate.get("dx", 0) or 0))), "dy": max(-0.35, min(0.35, float(candidate.get("dy", 0) or 0)))}
    action = (action_type or "").lower()
    direction = (direction or "").lower()
    if "left" in direction:
        return {"dx": -0.16, "dy": 0}
    if "right" in direction or "pull" in action:
        return {"dx": 0.16, "dy": 0}
    if "up" in direction or action in ("lift", "pry"):
        return {"dx": 0, "dy": -0.14}
    if "down" in direction:
        return {"dx": 0, "dy": 0.14}
    return {"dx": 0.12 if action in ("slide", "pull") else 0, "dy": 0}


def _style_for(action_type: str, priority: int) -> Dict[str, Any]:
    action = (action_type or "inspect").lower()
    tone = "danger" if action == "warning" or priority >= 5 else "action" if action not in ("inspect", "") else "inspect"
    return {
        "tone": tone,
        "shape": "arc" if action in ("rotate_cw", "rotate_ccw", "unscrew") else "arrow" if action in ("pull", "slide", "lift", "pry", "press") else "ring",
        "pulse": priority >= 4,
    }


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = match.group(1) if match else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _ground_target_with_model(raw_b64: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if not _gemini_api_key() or not raw_b64:
        return {"found": False}

    prompt = f"""
You are a visual grounding module for a repair-assistant HUD. Return JSON only.
Find the requested target in the camera frame and produce normalized image coordinates.

Target: {args.get("target_hint", "")}
Action: {args.get("action_type", "inspect")}
Direction: {args.get("direction", "")}

Rules:
- Return a tight box around the physical object or part itself, not the hand, arm, face, bed, wall, or background.
- If the target says "device in my hand", find the non-hand object being held and box only that object.
- Prefer the most visually distinctive object matching the target. Include enough of the object for local tracking, but keep the box tight.
- If there are multiple candidates, choose the one closest to the center of the visible hand-held object.

Return exactly:
{{
  "found": true,
  "label": "short label",
  "geometry": {{"type":"box","x":0.0,"y":0.0,"width":0.0,"height":0.0}},
  "anchor_point": {{"x":0.0,"y":0.0}},
  "action_vector": {{"dx":0.0,"dy":0.0}},
  "confidence": 0.0,
  "notes": "brief"
}}

If the target is not visible, set found false and give your best approximate area only if useful.
Coordinates are relative to the full image, not the displayed mirrored preview.
"""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": raw_b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.05,
            "responseMimeType": "application/json",
            "maxOutputTokens": 512,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.5)) as client:
            res = await client.post(_grounding_url(), json=payload)
            res.raise_for_status()
            data = res.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        return _extract_json(text) or {"found": False}
    except Exception:
        return {"found": False}


async def inspect_current_frame(session_id: str, question: str = "", focus: str = "") -> Dict[str, Any]:
    state = _session_state(session_id)
    raw_b64 = state.get("latest_frame")
    if not raw_b64:
        return {"ok": False, "error": "no current camera frame"}
    if not _gemini_api_key():
        return {"ok": False, "error": "GOOGLE_API_KEY missing"}

    prompt = f"""
Return compact JSON only. Inspect THIS single current camera frame, not memory.
Question: {question or "What is visible?"}
Focus: {focus or "hands and visible devices/parts"}

Schema:
{{
  "ok": true,
  "direct_answer": "short answer",
  "hand_visible": true,
  "objects_in_hand": ["short names"],
  "visible_devices": ["short names"],
  "visible_parts": ["short names"],
  "best_target": {{
    "label": "short target name",
    "description": "short visual description",
    "geometry": {{"type":"box","x":0.0,"y":0.0,"width":0.0,"height":0.0}},
    "confidence": 0.0
  }},
  "scene_summary": "short literal summary",
  "confidence": 0.0,
  "needs_user_adjustment": false
}}

Rules:
- If a hand holds an object, name the held object, even if generic.
- If exact category is uncertain, describe appearance; do not deny a clear held object.
- best_target.geometry must tightly cover the held object/target only, not the hand, face, hair, bed, wall, or background.
- Keep all strings brief.
"""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": raw_b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 768,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            res = await client.post(_grounding_url(), json=payload)
            res.raise_for_status()
            data = res.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        parsed = _extract_json(text) or {"ok": False, "error": "parse failed", "raw": text[:300]}
    except Exception as exc:
        parsed = {"ok": False, "error": str(exc)}

    target = parsed.get("best_target") if isinstance(parsed.get("best_target"), dict) else {}
    if target:
        target["geometry"] = _normal_geometry(target.get("geometry")) or target.get("geometry")
    _log_hud_event(session_id, "inspect_current_frame", {
        "frame_index": int(state.get("frame_index", 0) or 0),
        "frame_path": _store_frame_sample(session_id, int(state.get("frame_index", 0) or 0), raw_b64, reason="inspect_current_frame"),
        "question": question,
        "focus": focus,
        "result": parsed,
    })
    state["last_inspection"] = parsed
    return parsed


async def run_highlight_tool(session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    state = _session_state(session_id)
    target_hint = (args.get("target_hint") or args.get("label") or "target").strip()
    label = (args.get("label") or target_hint or "Target").strip()[:36]
    action_type = (args.get("action_type") or "inspect").strip()
    direction = (args.get("direction") or "").strip()
    priority = int(max(1, min(5, float(args.get("priority", 3) or 3))))
    marker_id = (args.get("marker_id") or f"hud-{uuid.uuid4().hex[:8]}").strip()
    expires_ms = int(max(2500, min(60000, float(args.get("expires_ms", DEFAULT_EXPIRES_MS) or DEFAULT_EXPIRES_MS))))

    supplied_geometry = _normal_geometry(args.get("geometry"))
    grounded = {}
    raw_b64 = state.get("latest_frame")
    if supplied_geometry:
        geometry = supplied_geometry
        confidence = 0.72
    else:
        grounded = await _ground_target_with_detector(raw_b64, args) if raw_b64 else {"found": False}
        if not grounded.get("found") and raw_b64 and GROUNDING_BACKEND in ("auto", "gemini_fallback", "grounding_dino"):
            inspection = await inspect_current_frame(session_id, args.get("target_hint", ""), "hands and target object")
            best_target = inspection.get("best_target") if isinstance(inspection.get("best_target"), dict) else {}
            inspected_geometry = _normal_geometry(best_target.get("geometry"))
            if inspected_geometry and float(best_target.get("confidence", 0) or inspection.get("confidence", 0) or 0) >= 0.35:
                grounded = {
                    "found": True,
                    "source": "inspect_current_frame",
                    "label": best_target.get("label") or args.get("target_hint", "target"),
                    "geometry": inspected_geometry,
                    "anchor_point": _anchor_from_geometry(inspected_geometry),
                    "confidence": float(best_target.get("confidence", 0) or inspection.get("confidence", 0) or 0.45),
                    "notes": best_target.get("description") or inspection.get("direct_answer", ""),
                    "candidates": [],
                    "ambiguous": bool(inspection.get("needs_user_adjustment")),
                }
            else:
                grounded = await _ground_target_with_model(raw_b64, args)
        geometry = _normal_geometry(grounded.get("geometry")) or _default_geometry(target_hint)
        confidence = float(grounded.get("confidence", 0.38) or 0.38)
        if grounded.get("label"):
            label = str(grounded["label"])[:36]

    grounded_segmentation = grounded.get("segmentation") if isinstance(grounded.get("segmentation"), dict) else {}
    grounded_contour = grounded.get("mask_contour") if isinstance(grounded.get("mask_contour"), list) else []
    if grounded_contour:
        segmentation = {**grounded_segmentation, "provider": grounded_segmentation.get("provider") or "grounded_sam2", "contour": grounded_contour}
    else:
        segmentation = await asyncio.to_thread(_segment_contour_from_box, raw_b64, geometry)
    contour = segmentation.get("contour") or _rectangle_contour(geometry)
    contour_bbox = _bbox_from_contour(contour)
    if contour_bbox and segmentation.get("provider") in ("sam2", "grounded_sam2", "opencv_grabcut"):
        geometry = contour_bbox
    confidence = max(confidence, float(segmentation.get("confidence", 0) or 0))
    marker_type = _marker_type_for(label, target_hint, geometry)
    if marker_type == "point":
        geometry = _point_geometry_from_box(geometry)
        contour = _rectangle_contour(geometry)
    anchor = _anchor_from_geometry(geometry, grounded.get("anchor_point"))
    vector = _vector_for(action_type, direction, grounded.get("action_vector"))
    now = _now_ms()
    object_id = _next_object_id(state)
    marker = {
        "id": marker_id,
        "object_id": object_id,
        "label": label,
        "target_hint": target_hint,
        "action_type": action_type,
        "marker_type": marker_type,
        "direction": direction,
        "priority": priority,
        "geometry": geometry,
        "model_geometry": dict(geometry),
        "seed_geometry": dict(geometry),
        "seed_frame_index": int(state.get("frame_index", 0) or 0),
        "mask_contour": contour,
        "model_contour": list(contour),
        "anchor_point": anchor,
        "action_vector": vector,
        "confidence": round(max(0.05, min(0.99, confidence)), 3),
        "tracking_mode": "template_lock",
        "tracking_status": "candidate" if grounded.get("ambiguous") or confidence < 0.42 else "seeded",
        "visual_anchor": grounded.get("notes", ""),
        "debug": {
            "source": grounded.get("source") or ("model_grounding" if grounded else "tool_geometry"),
            "grounding_found": bool(grounded.get("found")) if grounded else bool(supplied_geometry),
            "grounding_confidence": round(max(0.05, min(0.99, confidence)), 3),
            "candidate_count": len(grounded.get("candidates") or []),
            "ambiguous": bool(grounded.get("ambiguous")),
            "grounding_error": grounded.get("error"),
            "grounded_sam2_enabled": GROUNDED_SAM2_ENABLED,
            "detector_geometry": grounded.get("detector_geometry"),
            "segmentation_provider": segmentation.get("provider"),
            "segmentation_device": segmentation.get("device"),
            "segmentation_confidence": segmentation.get("confidence"),
            "segmentation_fill_ratio": segmentation.get("fill_ratio"),
            "segmentation_prompt_type": segmentation.get("prompt_type"),
            "contour_points": len(contour),
            "geometry_refined_from_mask": bool(contour_bbox),
        },
        "style": _style_for(action_type, priority),
        "candidates": grounded.get("candidates", [])[:5],
        "created_at_ms": now,
        "updated_at_ms": now,
        "expires_at_ms": now + expires_ms,
    }
    state["markers"][marker_id] = marker
    state["sam2_streams"].pop(marker_id, None)
    if len(state["markers"]) > MAX_MARKERS:
        oldest = sorted(state["markers"].values(), key=lambda m: (m.get("priority", 1), m.get("updated_at_ms", 0)))[0]
        state["markers"].pop(oldest["id"], None)
    state["version"] += 1
    frame_path = _store_frame_sample(session_id, int(state.get("frame_index", 0) or 0), raw_b64, reason="highlight") if raw_b64 else None
    _log_hud_event(session_id, "highlight", {
        "frame_index": int(state.get("frame_index", 0) or 0),
        "frame_path": frame_path,
        "args": args,
        "marker": marker,
        "grounded": grounded,
        "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
    })
    return {"marker": marker, "snapshot": build_hud_snapshot(session_id)}


def apply_local_tracks(session_id: str, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = _session_state(session_id)
    changed = False
    now = _now_ms()
    for track in tracks or []:
        marker_id = track.get("id")
        marker = state["markers"].get(marker_id)
        if not marker:
            continue
        geom = _normal_geometry(track.get("geometry"))
        if geom:
            marker["geometry"] = geom
            marker["anchor_point"] = _anchor_from_geometry(geom)
        if "confidence" in track:
            marker["confidence"] = round(max(0.0, min(0.99, float(track.get("confidence") or 0))), 3)
        marker["tracking_status"] = str(track.get("tracking_status") or "tracking")
        if isinstance(track.get("debug"), dict):
            marker["debug"] = {**(marker.get("debug") or {}), **track["debug"]}
        marker["updated_at_ms"] = now
        if marker["tracking_status"] in ("locked", "tracking"):
            marker["expires_at_ms"] = max(int(marker.get("expires_at_ms", 0) or 0), now + 45000)
        changed = True
    if changed:
        state["version"] += 1
    return build_hud_snapshot(session_id)


def _select_marker(state: Dict[str, Any], marker_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if marker_id and marker_id in state["markers"]:
        return state["markers"][marker_id]
    active = list(state["markers"].values())
    if not active:
        return None
    return sorted(active, key=lambda m: (-int(m.get("priority", 1)), -int(m.get("updated_at_ms", 0) or 0)))[0]


async def update_hud_marker(session_id: str, marker_id: Optional[str], geometry: Dict[str, Any], locked: bool = True, refine: bool = False) -> Dict[str, Any]:
    state = _session_state(session_id)
    marker = _select_marker(state, marker_id) if marker_id else None
    geom = _normal_geometry(geometry)
    if marker is None or not geom:
        return build_hud_snapshot(session_id)

    now = _now_ms()
    marker["geometry"] = geom
    marker["model_geometry"] = dict(geom)
    marker["seed_geometry"] = dict(geom)
    marker["seed_frame_index"] = int(state.get("frame_index", 0) or 0)
    marker["anchor_point"] = _anchor_from_geometry(geom)
    marker["manual_locked"] = bool(locked)
    marker["tracking_status"] = "manual_locked" if locked else "corrected"
    marker["marker_type"] = _marker_type_for(marker.get("label", ""), marker.get("target_hint", ""), geom)
    marker["updated_at_ms"] = now
    marker["expires_at_ms"] = max(int(marker.get("expires_at_ms", 0) or 0), now + 90000)
    marker["debug"] = {
        **(marker.get("debug") or {}),
        "manual_update_at_ms": now,
        "manual_locked": bool(locked),
        "manual_refine_requested": bool(refine),
    }

    if refine and state.get("latest_frame"):
        segmentation = await asyncio.to_thread(_segment_contour_from_box, state.get("latest_frame"), geom)
        contour = segmentation.get("contour") or []
        bbox = _bbox_from_contour(contour)
        if bbox:
            safe, reason = _refresh_is_safe(marker, bbox, segmentation)
            if safe:
                marker["geometry"] = bbox
                marker["model_geometry"] = dict(bbox)
                marker["seed_geometry"] = dict(bbox)
                marker["mask_contour"] = contour
                marker["model_contour"] = list(contour)
                state["sam2_streams"].pop(marker["id"], None)
            marker["debug"] = {
                **(marker.get("debug") or {}),
                "segmentation_provider": segmentation.get("provider"),
                "segmentation_device": segmentation.get("device"),
                "segmentation_confidence": segmentation.get("confidence"),
                "contour_points": len(contour),
                "manual_refine_result": reason,
            }
    state["version"] += 1
    _log_hud_event(session_id, "manual_update", {
        "frame_index": int(state.get("frame_index", 0) or 0),
        "frame_path": _store_frame_sample(session_id, int(state.get("frame_index", 0) or 0), state.get("latest_frame"), reason="manual_update") if state.get("latest_frame") else None,
        "marker_id": marker.get("id"),
        "geometry": marker.get("geometry"),
        "locked": bool(locked),
        "refine": bool(refine),
        "debug": marker.get("debug", {}),
    })
    return build_hud_snapshot(session_id)


async def correct_hud_marker(session_id: str, marker_id: Optional[str], point: Dict[str, Any], label: int = 1) -> Dict[str, Any]:
    state = _session_state(session_id)
    raw_b64 = state.get("latest_frame")
    if not raw_b64:
        return build_hud_snapshot(session_id)

    px = _clamp01(point.get("x"), 0.5)
    py = _clamp01(point.get("y"), 0.5)
    marker = _select_marker(state, marker_id)
    if marker is None and state["markers"]:
        def _dist(m):
            g = _normal_geometry(m.get("geometry")) or _default_geometry("")
            return (g["x"] + g["width"] / 2 - px) ** 2 + (g["y"] + g["height"] / 2 - py) ** 2
        marker = min(state["markers"].values(), key=_dist)
    if marker is None:
        return build_hud_snapshot(session_id)

    geometry = _normal_geometry(marker.get("geometry")) or _default_geometry("")
    correction_points = list((marker.get("debug") or {}).get("correction_points") or [])
    correction_points.append({"x": px, "y": py, "label": int(label)})
    correction_points = correction_points[-8:]
    auto_negatives = _negative_prompt_ring(geometry, [p for p in correction_points if int(p.get("label", 1)) == 1])
    prompt_points = correction_points + auto_negatives
    point_coords = [{"x": p["x"], "y": p["y"]} for p in prompt_points]
    point_labels = [int(p.get("label", 1)) for p in prompt_points]

    segmentation = await asyncio.to_thread(_segment_contour_sam2, raw_b64, geometry, point_coords, point_labels)
    if not segmentation.get("contour"):
        segmentation = await asyncio.to_thread(_segment_contour_from_box, raw_b64, geometry)
    contour = segmentation.get("contour") or marker.get("mask_contour") or []
    bbox = _bbox_from_contour(contour)
    now = _now_ms()
    if bbox:
        safe, reason = _refresh_is_safe(marker, bbox, segmentation)
        area_ratio = _geometry_area(bbox) / max(_geometry_area(geometry), 1e-6)
        correction_accept = safe or (int(label) == 1 and area_ratio <= 2.4)
        if correction_accept:
            geometry = bbox
            marker["geometry"] = geometry
            marker["model_geometry"] = dict(geometry)
            marker["seed_geometry"] = dict(geometry)
            marker["seed_frame_index"] = int(state.get("frame_index", 0) or 0)
            state["sam2_streams"].pop(marker["id"], None)
        marker.setdefault("debug", {})["correction_refine_result"] = reason
    if contour:
        marker["mask_contour"] = contour
        marker["model_contour"] = list(contour)
        state["sam2_streams"].pop(marker["id"], None)
    marker["confidence"] = round(max(float(marker.get("confidence", 0) or 0), float(segmentation.get("confidence", 0) or 0)), 3)
    marker["tracking_status"] = "corrected"
    marker["manual_locked"] = False
    marker["updated_at_ms"] = now
    marker["expires_at_ms"] = max(int(marker.get("expires_at_ms", 0) or 0), now + 60000)
    marker["debug"] = {
        **(marker.get("debug") or {}),
        "segmentation_provider": segmentation.get("provider"),
        "segmentation_device": segmentation.get("device"),
        "segmentation_confidence": segmentation.get("confidence"),
        "contour_points": len(contour),
        "correction_points": correction_points,
        "auto_negative_points": len(auto_negatives),
        "last_correction_at_ms": now,
        "prompt_points": segmentation.get("prompt_points", len(prompt_points)),
    }
    state["version"] += 1
    _log_hud_event(session_id, "correction", {
        "frame_index": int(state.get("frame_index", 0) or 0),
        "frame_path": _store_frame_sample(session_id, int(state.get("frame_index", 0) or 0), raw_b64, reason="correction"),
        "marker_id": marker.get("id"),
        "point": {"x": px, "y": py, "label": int(label)},
        "geometry": marker.get("geometry"),
        "contour_points": len(contour),
        "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
        "accepted": marker.get("debug", {}).get("correction_refine_result"),
    })
    return build_hud_snapshot(session_id)


async def adjust_hud_marker(session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    state = _session_state(session_id)
    operation = str(args.get("operation") or "lock").lower().strip()
    marker = _select_marker(state, args.get("marker_id"))

    if operation == "relock":
        relock_args = {
            "target_hint": args.get("target_hint") or (marker or {}).get("target_hint") or (marker or {}).get("label") or "target",
            "label": (marker or {}).get("label") or args.get("target_hint") or "Target",
            "action_type": (marker or {}).get("action_type") or "inspect",
            "marker_id": (marker or {}).get("id") or args.get("marker_id"),
            "priority": (marker or {}).get("priority", 3),
            "expires_ms": 90000,
        }
        if args.get("geometry"):
            relock_args["geometry"] = args.get("geometry")
        result = await run_highlight_tool(session_id, relock_args)
        _log_hud_event(session_id, "ai_relock", {"args": args, "marker": result.get("marker", {})})
        return {"ok": True, "operation": operation, "marker": result.get("marker"), "snapshot": result.get("snapshot")}

    if marker is None:
        return {"ok": False, "error": "no active marker", "snapshot": build_hud_snapshot(session_id)}

    if operation == "remove":
        removed = state["markers"].pop(marker["id"], None)
        state["version"] += 1
        _log_hud_event(session_id, "ai_remove", {"args": args, "marker": removed})
        return {"ok": True, "operation": operation, "marker_id": marker["id"], "snapshot": build_hud_snapshot(session_id)}

    if operation in ("positive_point", "negative_point"):
        snapshot = await correct_hud_marker(session_id, marker.get("id"), args.get("point") or {}, 0 if operation == "negative_point" else 1)
        return {"ok": True, "operation": operation, "marker_id": marker.get("id"), "snapshot": snapshot}

    if operation in ("move", "resize"):
        snapshot = await update_hud_marker(session_id, marker.get("id"), args.get("geometry") or marker.get("geometry") or {}, locked=True, refine=True)
        return {"ok": True, "operation": operation, "marker_id": marker.get("id"), "snapshot": snapshot}

    if operation in ("lock", "unlock"):
        marker["manual_locked"] = operation == "lock"
        marker["tracking_status"] = "manual_locked" if operation == "lock" else "corrected"
        marker["updated_at_ms"] = _now_ms()
        marker["expires_at_ms"] = max(int(marker.get("expires_at_ms", 0) or 0), _now_ms() + 90000)
        marker["debug"] = {**(marker.get("debug") or {}), "ai_operation": operation, "ai_operation_at_ms": _now_ms()}
        state["version"] += 1
        _log_hud_event(session_id, f"ai_{operation}", {"args": args, "marker": marker})
        return {"ok": True, "operation": operation, "marker_id": marker.get("id"), "snapshot": build_hud_snapshot(session_id)}

    return {"ok": False, "error": f"unknown operation: {operation}", "snapshot": build_hud_snapshot(session_id)}


async def verify_hud_marker(session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    state = _session_state(session_id)
    marker = _select_marker(state, args.get("marker_id"))
    raw_b64 = state.get("latest_frame")
    if marker is None or not raw_b64:
        return {"ok": False, "error": "missing marker or frame", "snapshot": build_hud_snapshot(session_id)}
    if not _gemini_api_key():
        return {"ok": False, "error": "GOOGLE_API_KEY missing", "snapshot": build_hud_snapshot(session_id)}

    target = args.get("target_hint") or marker.get("target_hint") or marker.get("label") or "target"
    prompt = f"""
You are auditing a repair-assistant HUD marker. Return JSON only.

Target that should be marked: {target}
Current marker:
{json.dumps({
    "id": marker.get("id"),
    "label": marker.get("label"),
    "marker_type": marker.get("marker_type"),
    "geometry": marker.get("geometry"),
    "mask_contour_points": len(marker.get("mask_contour") or []),
}, ensure_ascii=False)}

Judge whether the marker geometry tightly covers or points to the requested target in the image.
Rules:
- For screws, holes, ports, pins, clips, tabs, buttons: a precise center point/ring is correct; do not require a full mask.
- For larger objects/panels/cases: the box/mask should cover only the physical target, not hands, desk, wall, or background.
- If wrong, suggest a corrected normalized geometry.

Return exactly:
{{
  "ok": true,
  "confidence": 0.0,
  "issue": "brief reason or empty",
  "suggested_geometry": {{"type":"box","x":0.0,"y":0.0,"width":0.0,"height":0.0}},
  "suggested_marker_type": "point or contour"
}}
"""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": raw_b64}},
            ],
        }],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json", "maxOutputTokens": 512},
    }
    verification: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.5)) as client:
            res = await client.post(_grounding_url(), json=payload)
            res.raise_for_status()
            data = res.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        verification = _extract_json(text) or {"ok": False, "confidence": 0.0, "issue": "verification parse failed"}
    except Exception as exc:
        verification = {"ok": False, "confidence": 0.0, "issue": str(exc)}

    applied = False
    suggested = _normal_geometry(verification.get("suggested_geometry"))
    if args.get("apply_suggestion") and suggested and not bool(verification.get("ok")) and float(verification.get("confidence", 0) or 0) >= 0.55:
        await update_hud_marker(session_id, marker.get("id"), suggested, locked=False, refine=True)
        applied = True

    _log_hud_event(session_id, "ai_verify", {
        "frame_index": int(state.get("frame_index", 0) or 0),
        "frame_path": _store_frame_sample(session_id, int(state.get("frame_index", 0) or 0), raw_b64, reason="ai_verify"),
        "args": args,
        "marker_before": marker,
        "verification": verification,
        "applied": applied,
    })
    return {
        "ok": True,
        "marker_id": marker.get("id"),
        "verification": verification,
        "applied": applied,
        "snapshot": build_hud_snapshot(session_id),
    }


async def propagate_stream_masks(session_id: str) -> Optional[Dict[str, Any]]:
    state = _session_state(session_id)
    raw_b64 = state.get("latest_frame")
    if not raw_b64 or state.get("stream_refreshing"):
        return None
    markers = [
        marker for marker in state["markers"].values()
        if marker.get("tracking_status") in ("seeded", "locked", "tracking", "corrected", "manual_locked")
        and marker.get("marker_type") != "point"
        and float(marker.get("confidence", 0) or 0) >= 0.35
    ]
    if not markers:
        return None
    state["stream_refreshing"] = True
    changed = False
    try:
        for marker in markers[:2]:
            marker_id = marker.get("id")
            if not marker_id:
                continue
            stream = state["sam2_streams"].get(marker_id)
            if stream is None:
                init_result = await asyncio.to_thread(_sam2_stream_init, raw_b64, marker)
                if init_result.get("outputs"):
                    state["sam2_streams"][marker_id] = init_result
                    marker["debug"] = {
                        **(marker.get("debug") or {}),
                        "sam2_stream_provider": init_result.get("provider"),
                        "sam2_stream_device": init_result.get("device"),
                        "sam2_stream_frame_idx": init_result.get("frame_idx"),
                    }
                    changed = True
                else:
                    marker["debug"] = {**(marker.get("debug") or {}), "sam2_stream_error": init_result.get("error") or init_result.get("provider")}
                continue
            segmentation = await asyncio.to_thread(_sam2_stream_track, raw_b64, stream)
            contour = segmentation.get("contour") or []
            bbox = _bbox_from_contour(contour)
            if not bbox:
                continue
            safe, reason = _refresh_is_safe(marker, bbox, segmentation)
            if not safe:
                marker["debug"] = {
                    **(marker.get("debug") or {}),
                    "sam2_stream_provider": segmentation.get("provider"),
                    "sam2_stream_rejected": reason,
                    "sam2_stream_frame_idx": segmentation.get("frame_idx"),
                }
                _log_hud_event(session_id, "sam2_stream_rejected", {
                    "frame_index": int(state.get("frame_index", 0) or 0),
                    "marker_id": marker_id,
                    "object_id": marker.get("object_id"),
                    "reason": reason,
                    "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
                })
                changed = True
                continue
            marker["geometry"] = bbox
            marker["mask_contour"] = contour
            marker["model_geometry"] = dict(bbox)
            marker["model_contour"] = list(contour)
            marker["tracking_status"] = "locked"
            marker["confidence"] = round(max(float(marker.get("confidence", 0) or 0), float(segmentation.get("confidence", 0) or 0)), 3)
            marker["updated_at_ms"] = _now_ms()
            marker["debug"] = {
                **(marker.get("debug") or {}),
                "segmentation_provider": segmentation.get("provider"),
                "segmentation_device": segmentation.get("device"),
                "contour_points": len(contour),
                "sam2_stream_frame_idx": segmentation.get("frame_idx"),
                "sam2_stream_result": reason,
                "prompt_type": segmentation.get("prompt_type"),
            }
            _log_hud_event(session_id, "sam2_stream", {
                "frame_index": int(state.get("frame_index", 0) or 0),
                "marker_id": marker_id,
                "object_id": marker.get("object_id"),
                "geometry": marker.get("geometry"),
                "contour_points": len(contour),
                "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
            })
            changed = True
        if changed:
            state["version"] += 1
            return build_hud_snapshot(session_id)
        return None
    finally:
        state["stream_refreshing"] = False


async def refresh_stale_masks(session_id: str, min_interval_ms: int = 1500) -> Optional[Dict[str, Any]]:
    state = _session_state(session_id)
    if state.get("mask_refreshing") or not state.get("latest_frame"):
        return None
    now = _now_ms()
    candidates = []
    for marker in state["markers"].values():
        if marker.get("tracking_status") not in ("seeded", "locked", "tracking", "corrected", "manual_locked"):
            continue
        if float(marker.get("confidence", 0) or 0) < 0.35:
            continue
        debug = marker.get("debug") or {}
        last_refresh = int(debug.get("mask_refreshed_at_ms") or marker.get("created_at_ms") or 0)
        movement_px = float(debug.get("movement_px", 0) or 0)
        confidence = float(marker.get("confidence", 0) or 0)
        adaptive_interval = min_interval_ms
        if confidence < 0.58 or movement_px > 18 or marker.get("tracking_status") in ("corrected", "low_confidence", "reacquiring"):
            adaptive_interval = min(650, min_interval_ms)
        if now - last_refresh >= adaptive_interval:
            candidates.append(marker)
    if not candidates:
        return None

    state["mask_refreshing"] = True
    changed = False
    try:
        raw_b64 = state.get("latest_frame")
        for marker in candidates[:2]:
            geometry = _normal_geometry(marker.get("geometry"))
            if not geometry:
                continue
            seed_index = int(marker.get("seed_frame_index") or 0)
            seed_geometry = _normal_geometry(marker.get("seed_geometry") or marker.get("model_geometry") or marker.get("geometry")) or geometry
            seed_contour = marker.get("model_contour") or marker.get("mask_contour") or []
            object_id = int(marker.get("object_id") or 1)
            frames = [f for f in state.get("frame_buffer", []) if int(f.get("index", 0)) >= seed_index]
            if len(frames) > 10:
                frames = [frames[0]] + frames[-9:]
            if len(frames) >= 2:
                segmentation = await asyncio.to_thread(_segment_contour_sam2_video, frames, seed_geometry, seed_contour, object_id)
                if not segmentation.get("contour"):
                    segmentation = await asyncio.to_thread(_segment_contour_from_box, raw_b64, geometry)
            else:
                segmentation = await asyncio.to_thread(_segment_contour_from_box, raw_b64, geometry)
            contour = segmentation.get("contour") or []
            if len(contour) < 3:
                continue
            bbox = _bbox_from_contour(contour)
            if bbox:
                safe, reason = _refresh_is_safe(marker, bbox, segmentation)
                if not safe:
                    marker["debug"] = {
                        **(marker.get("debug") or {}),
                        "segmentation_provider": segmentation.get("provider"),
                        "segmentation_device": segmentation.get("device"),
                        "contour_points": len(contour),
                        "video_frames": segmentation.get("video_frames"),
                        "mask_refreshed_at_ms": _now_ms(),
                        "mask_refresh_rejected": reason,
                        "prompt_type": segmentation.get("prompt_type"),
                    }
                    marker["updated_at_ms"] = _now_ms()
                    _log_hud_event(session_id, "mask_refresh_rejected", {
                        "frame_index": int(state.get("frame_index", 0) or 0),
                        "marker_id": marker.get("id"),
                        "object_id": marker.get("object_id"),
                        "reason": reason,
                        "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
                    })
                    changed = True
                    continue
                geometry = bbox
                marker["geometry"] = geometry
            else:
                reason = "no_bbox"
            marker["mask_contour"] = contour
            marker["model_contour"] = list(contour)
            marker["model_geometry"] = dict(geometry)
            marker["debug"] = {
                **(marker.get("debug") or {}),
                "segmentation_provider": segmentation.get("provider"),
                "segmentation_device": segmentation.get("device"),
                "segmentation_confidence": segmentation.get("confidence"),
                "segmentation_fill_ratio": segmentation.get("fill_ratio"),
                "contour_points": len(contour),
                "video_frames": segmentation.get("video_frames"),
                "mask_refreshed_at_ms": _now_ms(),
                "mask_refresh_result": reason,
                "prompt_type": segmentation.get("prompt_type"),
            }
            marker["updated_at_ms"] = _now_ms()
            _log_hud_event(session_id, "mask_refresh", {
                "frame_index": int(state.get("frame_index", 0) or 0),
                "marker_id": marker.get("id"),
                "object_id": marker.get("object_id"),
                "geometry": marker.get("geometry"),
                "contour_points": len(contour),
                "result": reason,
                "segmentation": {k: v for k, v in segmentation.items() if k != "contour"},
            })
            changed = True
        if changed:
            state["version"] += 1
            return build_hud_snapshot(session_id)
        return None
    finally:
        state["mask_refreshing"] = False


def build_hud_snapshot(session_id: str) -> Dict[str, Any]:
    state = _session_state(session_id)
    now = _now_ms()
    active = []
    expired = []
    for marker_id, marker in list(state["markers"].items()):
        if marker.get("expires_at_ms", 0) < now:
            expired.append(marker_id)
        else:
            active.append(marker)
    for marker_id in expired:
        state["markers"].pop(marker_id, None)
    if expired:
        state["version"] += 1
    active.sort(key=lambda m: (-int(m.get("priority", 1)), m.get("created_at_ms", 0)))
    return {
        "version": state["version"],
        "generated_at_ms": now,
        "frame_age_ms": now - int(state.get("latest_frame_at") or now),
        "markers": active,
    }


async def send_hud_snapshot(client_ws: Any, session_id: str, reason: str = "snapshot") -> None:
    try:
        await client_ws.send_json({"type": "hud.state", "reason": reason, "data": build_hud_snapshot(session_id)})
    except Exception:
        pass
