import json
import logging
import os
import re
import uuid
import base64
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from db import SessionToolRun

logger = logging.getLogger(__name__)

GROUNDING_MODEL = "gemini-2.5-flash"
GROUNDING_MAX_ATTEMPTS = 3

_runtime: Dict[str, Dict[str, Any]] = {}
PERSON_LABEL_TOKENS = {
    "person",
    "human",
    "user",
    "foreground person",
    "speaker",
    "face",
    "head",
    "body",
    "torso",
    "hand",
    "arm",
}
HELD_OBJECT_LABEL_TOKENS = {
    "hand",
    "holding",
    "held",
    "grip",
    "fingers",
    "thumb",
    "palm",
    "case",
    "earbud",
    "airpods",
    "screw",
    "tool",
    "object",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return text or "feature"


def _normalize_hint(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _is_person_self_reference(value: str) -> bool:
    hint = _normalize_hint(value)
    if not hint:
        return False
    phrases = {
        "me",
        "mark me",
        "highlight me",
        "circle me",
        "point at me",
        "myself",
        "user",
        "the user",
        "my face",
        "my head",
        "my body",
        "my hand",
        "my arm",
        "my finger",
        "my fingers",
    }
    return hint in phrases


def _is_held_object_reference(value: str) -> bool:
    hint = _normalize_hint(value)
    if not hint:
        return False
    patterns = [
        r"\bwhat i am holding\b",
        r"\bwhat i'm holding\b",
        r"\bwhat im holding\b",
        r"\bthe thing i am holding\b",
        r"\bthe thing i'm holding\b",
        r"\bobject in my hand\b",
        r"\bwhat is in my hand\b",
        r"\bwhat i have in my hand\b",
        r"\bthe thing in my hand\b",
        r"\bwhat i'm holding now\b",
        r"\bmark what i am holding\b",
    ]
    return any(re.search(pattern, hint) for pattern in patterns)


def _build_runtime_target(feature_id: str, target_hint: str) -> Dict[str, Any]:
    hint = (target_hint or "").strip()
    if _is_person_self_reference(hint):
        return {
            "feature_id": feature_id,
            "label": "Foreground person",
            "description": "The visible person in the foreground who is speaking to the assistant.",
            "visual_cues": [
                "person",
                "human",
                "foreground person",
                "speaker",
                "face",
                "upper body",
            ],
            "category": "person",
            "target_kind": "person",
            "original_hint": hint,
        }
    if _is_held_object_reference(hint):
        return {
            "feature_id": feature_id,
            "label": "Held object",
            "description": "The object currently being held in the visible user's hand.",
            "visual_cues": [
                "held object",
                "object in hand",
                "held by user",
                "near fingers",
                "foreground object",
            ],
            "category": "runtime",
            "target_kind": "held_object",
            "relation": "held_by_user",
            "original_hint": hint,
        }
    return {
        "feature_id": feature_id,
        "label": hint,
        "description": hint,
        "visual_cues": [hint],
        "category": "runtime",
        "target_kind": "object",
        "original_hint": hint,
    }


def _geometry_area(geometry: Dict[str, Any]) -> float:
    width = _normalize_value(geometry.get("width")) or 0.0
    height = _normalize_value(geometry.get("height")) or 0.0
    return width * height


def _looks_like_person_grounding(target: Dict[str, Any], grounded: Dict[str, Any]) -> bool:
    if target.get("target_kind") != "person":
        return True
    label = _normalize_hint(grounded.get("label", ""))
    notes = _normalize_hint(grounded.get("notes", ""))
    haystack = f"{label} {notes}".strip()
    if any(token in haystack for token in PERSON_LABEL_TOKENS):
        return True
    geometry = grounded.get("geometry") or {}
    gtype = geometry.get("type")
    if gtype == "box" and _geometry_area(geometry) >= 0.08:
        return True
    return False


def _looks_like_held_object_grounding(target: Dict[str, Any], grounded: Dict[str, Any]) -> bool:
    if target.get("target_kind") != "held_object":
        return True
    label = _normalize_hint(grounded.get("label", ""))
    notes = _normalize_hint(grounded.get("notes", ""))
    haystack = f"{label} {notes}".strip()
    if any(token in haystack for token in ("wall", "fixture", "hook", "curtain", "bed", "background")):
        return False
    if any(token in haystack for token in HELD_OBJECT_LABEL_TOKENS):
        return True
    geometry = grounded.get("geometry") or {}
    if geometry.get("type") == "box" and _geometry_area(geometry) >= 0.02:
        return True
    return False


def _candidate_matches_target_heuristics(target: Dict[str, Any], grounded: Dict[str, Any]) -> bool:
    return _looks_like_person_grounding(target, grounded) and _looks_like_held_object_grounding(target, grounded)


def _get_runtime(session_id: str) -> Dict[str, Any]:
    if session_id not in _runtime:
        _runtime[session_id] = {
            "latest_frame_b64": None,
            "latest_frame_at": None,
            "last_perception": None,
            "feature_catalog": {},
            "step_targets": [],
            "completion_checks": {},
            "manual_id": None,
            "manual_summary": None,
            "markers": {},
            "pending_confirmation": None,
            "runtime_feature_counter": 0,
            "manual_step_highlighted": None,
        }
    return _runtime[session_id]


def clear_runtime(session_id: str):
    _runtime.pop(session_id, None)


def set_latest_frame(session_id: str, frame_b64: str):
    runtime = _get_runtime(session_id)
    runtime["latest_frame_b64"] = frame_b64
    runtime["latest_frame_at"] = _now().isoformat()


def set_last_perception(session_id: str, observation: Optional[Dict[str, Any]]):
    _get_runtime(session_id)["last_perception"] = observation


def get_runtime_state(session_id: str) -> Dict[str, Any]:
    return _get_runtime(session_id)


def build_manual_hud_bundle(manual: Dict[str, Any]) -> Dict[str, Any]:
    feature_catalog: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}

    def add_feature(feature_id: str, label: str, description: str = "", cues: Optional[List[str]] = None, category: str = "component"):
        item = {
            "feature_id": feature_id,
            "label": label,
            "description": description,
            "visual_cues": [c for c in (cues or []) if c],
            "category": category,
        }
        feature_catalog[feature_id] = item
        aliases[_slug(label)] = feature_id
        for cue in item["visual_cues"]:
            aliases[_slug(cue)] = feature_id
        return item

    vision_cues = manual.get("visual_recognition_cues", [])
    for component in manual.get("components", []):
        label = component.get("name", "")
        if not label:
            continue
        feature_id = f"manual:{_slug(label)}"
        add_feature(
            feature_id,
            label,
            component.get("description", ""),
            cues=[label, component.get("description", "")] + vision_cues[:2],
            category="component",
        )

    screws = manual.get("screws", {}) or {}
    for location in screws.get("locations", []):
        location_id = location.get("location_id") or _slug(location.get("details", "") or location.get("position", "screw"))
        feature_id = f"manual:{location_id}"
        label = location.get("details") or f"{location.get('position', '').strip()} screw".strip()
        add_feature(
            feature_id,
            label,
            location.get("details", ""),
            cues=[
                label,
                location.get("details", ""),
                f"{location.get('count', 1)} screw",
                location.get("visibility", ""),
                location.get("position", ""),
            ],
            category="fastener",
        )

    hidden_clips = manual.get("hidden_clips", {}) or {}
    for clip in hidden_clips.get("locations", []):
        side = clip.get("side", "").strip().lower()
        existing_id = aliases.get(_slug(f"{side} side seam"))
        feature_id = existing_id or f"manual:{side}_side_seam"
        add_feature(
            feature_id,
            f"{side.title()} side seam",
            clip.get("release_method", ""),
            cues=[f"{side} side seam", "snap clip", clip.get("release_method", "")],
            category="seam",
        )

    step_targets: List[Dict[str, Any]] = []
    completion_checks: Dict[int, Dict[str, Any]] = {}
    for step in (manual.get("teardown", {}) or {}).get("steps", []):
        step_num = int(step.get("step", len(step_targets) + 1))
        step_info = _infer_step_target(step, feature_catalog)
        step_targets.append({
            "step": step_num,
            "title": step.get("title", ""),
            "instruction": step.get("instruction", ""),
            **step_info,
        })
        completion_checks[step_num] = _infer_completion_check(step, step_info)

    return {
        "hud_features": list(feature_catalog.values()),
        "step_targets": step_targets,
        "completion_checks": completion_checks,
    }


def _infer_step_target(step: Dict[str, Any], feature_catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    title = (step.get("title", "") + " " + step.get("instruction", "")).lower()
    feature_ids = list(feature_catalog.keys())
    primary_feature_id = None
    support_feature_ids: List[str] = []
    action_type = "inspect"

    feature_aliases = {fid: (feature_catalog[fid]["label"] + " " + feature_catalog[fid]["description"]).lower() for fid in feature_ids}

    def has_text(*needles: str) -> bool:
        return any(needle in title for needle in needles)

    for fid, haystack in feature_aliases.items():
        if primary_feature_id:
            break
        if "rear" in title and "rear" in haystack and "screw" in haystack:
            primary_feature_id = fid
        elif "front" in title and "hood" in title and "hood" in haystack:
            primary_feature_id = fid
        elif "hidden" in title and "screw" in title and ("hidden" in haystack or "front" in haystack):
            primary_feature_id = fid
        elif "blade head" in title and "blade head" in haystack:
            primary_feature_id = fid

    if has_text("side clips", "side seams", "release side clips", "seam"):
        seams = [fid for fid, item in feature_catalog.items() if "side seam" in item["label"].lower()]
        if seams:
            primary_feature_id = seams[0]
            support_feature_ids = seams[1:]

    if not primary_feature_id:
        for fid, haystack in feature_aliases.items():
            if any(token for token in title.split() if len(token) > 3 and token in haystack):
                primary_feature_id = fid
                break

    if has_text("remove", "unscrew"):
        action_type = "unscrew_ccw" if "screw" in title else "pull"
    elif has_text("lift", "hood"):
        action_type = "lift"
    elif has_text("release", "unclip", "pry"):
        action_type = "pry"
    elif has_text("slide"):
        action_type = "slide"
    elif has_text("hold"):
        action_type = "hold_here"

    return {
        "primary_feature_id": primary_feature_id or "manual:current_step",
        "support_feature_ids": support_feature_ids,
        "action_type": action_type,
        "reason": "manual_step",
    }


def _infer_completion_check(step: Dict[str, Any], step_target: Dict[str, Any]) -> Dict[str, Any]:
    step_num = int(step.get("step", 0))
    title = (step.get("title", "") + " " + step.get("instruction", "")).lower()
    primary = step_target.get("primary_feature_id")
    verified_any: List[str] = []
    ambiguous_any: List[str] = []
    fallback_prompt = "Show me that area clearly so I can confirm the step."

    if step_num == 2 or "blade head" in title:
        verified_any = ["blade head removed", "flat surface at the top", "exposed mounting area", "no blades visible"]
        ambiguous_any = ["top partially visible", "blurry top section"]
        fallback_prompt = "Show me the top of the device so I can confirm the blade head is off."
    elif step_num == 3 or ("rear" in title and "screw" in title and "locate" in title):
        verified_any = ["rear screw visible", "single visible screw at the rear", "back screw in view"]
        ambiguous_any = ["back partially visible", "rear area obscured"]
        fallback_prompt = "Center the rear screw area so I can confirm I’m marking the right fastener."
    elif step_num == 4 or ("rear" in title and "remove" in title):
        verified_any = ["rear screw hole empty", "one screw hole empty", "rear screw no longer visible"]
        ambiguous_any = ["rear housing partially visible", "back still obscured"]
        fallback_prompt = "Show me the back screw hole so I can verify the rear screw is out."
    elif step_num == 5 or "front plastic hood" in title:
        verified_any = ["hidden screws visible", "front hood removed", "front cavity exposed"]
        ambiguous_any = ["front section obscured", "front hood partially lifted"]
        fallback_prompt = "Show me the front hood area so I can confirm the hidden screws are exposed."
    elif step_num == 6 or ("hidden" in title and "screw" in title and "expose" in title):
        verified_any = ["two hidden screws visible", "hidden front screws visible", "front screw heads exposed"]
        ambiguous_any = ["front cavity visible", "one hidden screw visible"]
        fallback_prompt = "Hold the front opening steady so I can verify both hidden screws are visible."
    elif step_num == 7 or ("hidden" in title and "remove" in title):
        verified_any = ["front screw holes empty", "hidden screws no longer visible", "two screw holes empty"]
        ambiguous_any = ["one hidden screw still visible", "front opening partially obscured"]
        fallback_prompt = "Show me the front screw area so I can verify both hidden screws are removed."
    elif step_num == 8 or "side clips" in title:
        verified_any = ["side seam open", "clip released", "shell gap visible", "left and right seams open"]
        ambiguous_any = ["slight seam gap", "one seam still closed"]
        fallback_prompt = "Show me the side seams so I can confirm both clips are released."
    elif step_num == 9 or "separate shell" in title:
        verified_any = ["internals visible", "shell halves separated", "wiring visible inside", "housing open"]
        ambiguous_any = ["small shell gap", "shell partially separated"]
        fallback_prompt = "Hold the opened shell toward the camera so I can confirm the housing is separated."
    elif "inspect" in title:
        verified_any = [primary.replace("manual:", "").replace("_", " ")] if primary else []
        ambiguous_any = ["partially visible", "blurry"]

    return {
        "verified_any": verified_any,
        "ambiguous_any": ambiguous_any,
        "fallback_prompt": fallback_prompt,
    }


def sync_manual_bundle(session_id: str, manual: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    runtime = _get_runtime(session_id)
    if not manual:
        runtime["feature_catalog"] = {}
        runtime["step_targets"] = []
        runtime["completion_checks"] = {}
        runtime["manual_id"] = None
        runtime["manual_summary"] = None
        return {"hud_features": [], "step_targets": [], "completion_checks": {}}

    manual_id = manual.get("manual_id") or manual.get("id")
    if runtime["manual_id"] == manual_id and runtime["feature_catalog"]:
        return {
            "hud_features": list(runtime["feature_catalog"].values()),
            "step_targets": runtime["step_targets"],
            "completion_checks": runtime["completion_checks"],
        }

    bundle = build_manual_hud_bundle(manual)
    runtime["feature_catalog"] = {item["feature_id"]: item for item in bundle["hud_features"]}
    runtime["step_targets"] = bundle["step_targets"]
    runtime["completion_checks"] = bundle["completion_checks"]
    runtime["manual_id"] = manual_id
    runtime["manual_summary"] = f"{manual.get('brand', '')} {manual.get('model', '')} — {manual.get('title', '')}".strip()
    runtime["manual_step_highlighted"] = None
    return bundle


def get_feature_catalog(session_id: str) -> Dict[str, Dict[str, Any]]:
    return _get_runtime(session_id)["feature_catalog"]


def get_step_target(session_id: str, step_index: int) -> Optional[Dict[str, Any]]:
    runtime = _get_runtime(session_id)
    for item in runtime["step_targets"]:
        if int(item.get("step", -1)) == int(step_index) + 1:
            return item
    return None


def get_current_step_target(session_id: str, current_step_index: int) -> Optional[Dict[str, Any]]:
    return get_step_target(session_id, current_step_index)


def _cleanup_markers(runtime: Dict[str, Any]):
    now = _now()
    expired = [marker_id for marker_id, marker in runtime["markers"].items() if marker.get("expires_at") and datetime.fromisoformat(marker["expires_at"]) <= now]
    for marker_id in expired:
        runtime["markers"].pop(marker_id, None)


def _trim_marker_density(runtime: Dict[str, Any]):
    primaries = []
    secondaries = []
    for marker in runtime["markers"].values():
        if marker.get("priority") == "primary":
            primaries.append(marker)
        else:
            secondaries.append(marker)
    primaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    secondaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    keep = {item["marker_id"] for item in primaries[:1] + secondaries[:3]}
    for marker_id in list(runtime["markers"].keys()):
        if marker_id not in keep:
            runtime["markers"].pop(marker_id, None)


def build_hud_snapshot(session_id: str, current_step_index: int = 0) -> Dict[str, Any]:
    runtime = _get_runtime(session_id)
    _cleanup_markers(runtime)
    current_step_target = get_current_step_target(session_id, current_step_index)
    return {
        "type": "hud.state",
        "markers": list(runtime["markers"].values()),
        "feature_catalog": list(runtime["feature_catalog"].values()),
        "current_step_target": current_step_target,
        "pending_confirmation": runtime["pending_confirmation"],
    }


def _combined_observation_text(observation: Dict[str, Any]) -> str:
    parts = []
    for key in ("device_state", "focus_area", "safety_concern"):
        value = observation.get(key)
        if value:
            parts.append(str(value))
    for key in ("objects", "visible_features", "changed_vs_prior"):
        parts.extend(str(item) for item in observation.get(key, []) if item)
    return " ".join(parts).lower()


def evaluate_step_completion(session_id: str, current_step_index: int, observation: Optional[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not observation:
        return "none", None
    runtime = _get_runtime(session_id)
    step_target = get_current_step_target(session_id, current_step_index)
    if not step_target:
        return "none", None
    checks = runtime["completion_checks"].get(int(current_step_index) + 1) or {}
    haystack = _combined_observation_text(observation)
    verified = any(term and term.lower() in haystack for term in checks.get("verified_any", []))
    ambiguous = any(term and term.lower() in haystack for term in checks.get("ambiguous_any", []))
    if verified:
        return "verified", checks
    if ambiguous:
        return "ambiguous", checks
    return "none", checks


def maybe_track_pending_confirmation(session_id: str, current_step_index: int, checks: Optional[Dict[str, Any]]) -> bool:
    runtime = _get_runtime(session_id)
    target_step = int(current_step_index) + 1
    if runtime["pending_confirmation"] == target_step:
        return False
    runtime["pending_confirmation"] = target_step
    runtime["pending_confirmation_prompt"] = (checks or {}).get("fallback_prompt", "Can you show me that area clearly so I can confirm the step?")
    return True


def clear_pending_confirmation(session_id: str):
    runtime = _get_runtime(session_id)
    runtime["pending_confirmation"] = None
    runtime.pop("pending_confirmation_prompt", None)


def get_pending_confirmation_prompt(session_id: str) -> Optional[str]:
    return _get_runtime(session_id).get("pending_confirmation_prompt")


def user_confirms_step(session_id: str, text: str) -> bool:
    runtime = _get_runtime(session_id)
    if not runtime.get("pending_confirmation"):
        return False
    normalized = (text or "").lower()
    cues = ["yes", "done", "removed", "opened", "it is out", "i did", "finished", "yep", "yeah", "confirmed"]
    return any(cue in normalized for cue in cues)


def should_highlight_current_step(session_id: str, current_step_index: int) -> Optional[Dict[str, Any]]:
    runtime = _get_runtime(session_id)
    target = get_current_step_target(session_id, current_step_index)
    if not target:
        return None
    step_no = int(target.get("step", 0))
    if runtime.get("manual_step_highlighted") == step_no:
        return None
    runtime["manual_step_highlighted"] = step_no
    return target


def _register_runtime_feature(session_id: str, label: str, description: str = "") -> str:
    runtime = _get_runtime(session_id)
    runtime["runtime_feature_counter"] += 1
    feature_id = f"runtime:{_slug(label)}_{runtime['runtime_feature_counter']}"
    runtime["feature_catalog"][feature_id] = {
        "feature_id": feature_id,
        "label": label,
        "description": description,
        "visual_cues": [label, description],
        "category": "runtime",
    }
    return feature_id


def _grounding_url() -> str:
    key = os.environ.get("GOOGLE_API_KEY", "")
    return f"https://generativelanguage.googleapis.com/v1beta/models/{GROUNDING_MODEL}:generateContent?key={key}"


async def _generate_json_with_images(prompt: str, image_payloads: List[str]) -> Dict[str, Any]:
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}] + [
                {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
                for image_b64 in image_payloads
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(_grounding_url(), json=payload)
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)


def _decode_frame_image(frame_b64: str) -> Optional[Image.Image]:
    try:
        return Image.open(io.BytesIO(base64.b64decode(frame_b64))).convert("RGB")
    except Exception:
        return None


def _encode_image_b64(image: Image.Image) -> Optional[str]:
    try:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None


def _crop_geometry_image(frame_b64: str, geometry: Dict[str, Any]) -> Optional[str]:
    image = _decode_frame_image(frame_b64)
    if image is None:
        return None
    width, height = image.size
    if geometry.get("type") == "box":
        x = max(0.0, min(1.0, float(geometry.get("x", 0.0) or 0.0)))
        y = max(0.0, min(1.0, float(geometry.get("y", 0.0) or 0.0)))
        w = max(0.01, min(1.0, float(geometry.get("width", 0.2) or 0.2)))
        h = max(0.01, min(1.0, float(geometry.get("height", 0.2) or 0.2)))
        pad_x = min(0.12, w * 0.35)
        pad_y = min(0.12, h * 0.35)
        left = max(0, int((x - pad_x) * width))
        top = max(0, int((y - pad_y) * height))
        right = min(width, int((x + w + pad_x) * width))
        bottom = min(height, int((y + h + pad_y) * height))
    else:
        px = max(0.0, min(1.0, float(geometry.get("x", 0.5) or 0.5)))
        py = max(0.0, min(1.0, float(geometry.get("y", 0.5) or 0.5)))
        span = 0.18
        left = max(0, int((px - span) * width))
        top = max(0, int((py - span) * height))
        right = min(width, int((px + span) * width))
        bottom = min(height, int((py + span) * height))
    if right <= left or bottom <= top:
        return None
    return _encode_image_b64(image.crop((left, top, right, bottom)))


async def ground_feature(session_id: str, target: Dict[str, Any], action_type: str, allow_approximate: bool = True) -> Dict[str, Any]:
    runtime = _get_runtime(session_id)
    frame_b64 = runtime.get("latest_frame_b64")
    if not frame_b64:
        return {"status": "not_visible", "confidence": 0.0, "follow_up_prompt": "Turn the camera on and hold the target in frame."}

    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        logger.warning("ground_feature: no API key")
        return {"status": "not_visible", "confidence": 0.0, "follow_up_prompt": "I need the live camera frame to place a marker."}

    target_payload = {
        "target": target,
        "action_type": action_type,
        "recent_perception": runtime.get("last_perception") or {},
        "allow_approximate": allow_approximate,
    }
    prompt = (
        "You locate a requested target inside a live camera frame for a HUD overlay.\n"
        "Return STRICT JSON only with this schema:\n"
        "{\n"
        '  "status": "placed|approximate|ambiguous|not_visible|not_found",\n'
        '  "confidence": 0.0,\n'
        '  "label": "short label",\n'
        '  "notes": "brief observation",\n'
        '  "geometry": {\n'
        '    "type": "point|box|polygon|arrow_path",\n'
        '    "x": 0.0,\n'
        '    "y": 0.0,\n'
        '    "width": 0.0,\n'
        '    "height": 0.0,\n'
        '    "points": [{"x": 0.0, "y": 0.0}]\n'
        "  }\n"
        "}\n"
        "Rules:\n"
        "- Coordinates must be normalized from 0.0 to 1.0.\n"
        "- Use box for visible parts/regions, point for a single precise anchor, polygon for irregular hazard regions, arrow_path only when a direction path is necessary.\n"
        "- Use approximate when the general area is clear but exact boundaries are not.\n"
        "- Use ambiguous if the request could refer to multiple plausible targets.\n"
        "- Use not_visible if the relevant area is off-screen, too blurry, or blocked.\n"
        "- Never invent unseen screws, parts, or people.\n"
        "- If target.target_kind is person, mark the visible person/body part requested by the user, preferably the foreground speaker.\n"
        "- If target.target_kind is person and no person/body part is visible, return not_visible.\n"
        "- If target.target_kind is person, never substitute a background object, wall fixture, or room feature.\n"
        f"Target JSON:\n{json.dumps(target_payload, ensure_ascii=False)}"
    )
    try:
        parsed = await _generate_json_with_images(prompt, [frame_b64])
        geometry = parsed.get("geometry") or {}
        parsed["geometry"] = _normalize_geometry(geometry)
        return parsed
    except Exception as exc:
        logger.error("ground_feature failed: %s", exc)
        return {"status": "not_visible", "confidence": 0.0, "follow_up_prompt": "Hold the camera steadier and center the part so I can place the marker."}


async def verify_grounding_candidate(session_id: str, target: Dict[str, Any], candidate: Dict[str, Any], action_type: str) -> Dict[str, Any]:
    runtime = _get_runtime(session_id)
    frame_b64 = runtime.get("latest_frame_b64")
    if not frame_b64:
        return {
            "decision": "retry",
            "confidence": 0.0,
            "reason": "Live frame is unavailable for verification.",
            "retry_hint": target.get("original_hint") or target.get("label") or "the target",
        }

    if not _candidate_matches_target_heuristics(target, candidate):
        retry_hint = target.get("original_hint") or target.get("label") or "the target"
        if target.get("target_kind") == "person":
            retry_hint = "the foreground person, face, or upper body"
        elif target.get("target_kind") == "held_object":
            retry_hint = "the object in the user's hand near the fingers"
        return {
            "decision": "retry",
            "confidence": float(candidate.get("confidence", 0.0) or 0.0),
            "reason": "The candidate looks like the wrong type of target.",
            "retry_hint": retry_hint,
        }

    crop_b64 = _crop_geometry_image(frame_b64, candidate.get("geometry") or {})
    if not crop_b64:
        return {
            "decision": "retry",
            "confidence": 0.0,
            "reason": "Could not inspect the candidate crop.",
            "retry_hint": target.get("original_hint") or target.get("label") or "the target",
        }

    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return {
            "decision": "accept",
            "confidence": float(candidate.get("confidence", 0.0) or 0.0),
            "reason": "Verification fallback accepted due to missing API key.",
            "corrected_label": candidate.get("label") or target.get("label"),
        }

    verification_payload = {
        "target": target,
        "action_type": action_type,
        "candidate": {
            "label": candidate.get("label"),
            "status": candidate.get("status"),
            "confidence": candidate.get("confidence"),
            "geometry": candidate.get("geometry"),
            "notes": candidate.get("notes", ""),
        },
        "recent_perception": runtime.get("last_perception") or {},
    }
    prompt = (
        "You are verifying a candidate HUD marker before it is shown to the user.\n"
        "Image 1 is the full camera frame. Image 2 is the cropped candidate marker region.\n"
        "Return STRICT JSON only with this schema:\n"
        "{\n"
        '  "decision": "accept|retry|reject",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "brief reason",\n'
        '  "corrected_label": "short label or empty string",\n'
        '  "retry_hint": "better target hint or empty string"\n'
        "}\n"
        "Rules:\n"
        "- Accept only if the candidate crop actually matches the requested target.\n"
        "- Retry if the target is plausible but the wrong region or wrong object was selected.\n"
        "- Reject only if the target is definitely not visible in the frame.\n"
        "- For person targets, the crop should show the requested person/body part, not the background.\n"
        "- For held_object targets, the crop should show the item being held or touching the visible hand/fingers.\n"
        "- For screws or small parts, prefer the part itself over nearby background material.\n"
        f"Verification JSON:\n{json.dumps(verification_payload, ensure_ascii=False)}"
    )

    try:
        parsed = await _generate_json_with_images(prompt, [frame_b64, crop_b64])
        return {
            "decision": parsed.get("decision", "retry"),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "reason": parsed.get("reason", ""),
            "corrected_label": parsed.get("corrected_label") or candidate.get("label") or target.get("label"),
            "retry_hint": parsed.get("retry_hint", ""),
        }
    except Exception as exc:
        logger.error("verify_grounding_candidate failed: %s", exc)
        return {
            "decision": "accept",
            "confidence": float(candidate.get("confidence", 0.0) or 0.0),
            "reason": "Verification fallback accepted after verifier error.",
            "corrected_label": candidate.get("label") or target.get("label"),
        }


def _build_retry_target(target: Dict[str, Any], retry_hint: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    next_target = dict(target)
    hint = (retry_hint or "").strip()
    if hint:
        cues = list(next_target.get("visual_cues") or [])
        cues.insert(0, hint)
        next_target["visual_cues"] = list(dict.fromkeys(cues))[:6]
        next_target["description"] = " ".join(filter(None, [next_target.get("description", ""), f"Retry focus: {hint}."])).strip()
    if target.get("target_kind") == "held_object":
        next_target["description"] = "The object touching the visible hand or fingers in the foreground."
    elif target.get("target_kind") == "person":
        next_target["description"] = "The visible foreground person, face, or upper body rather than any background object."
    if candidate.get("label"):
        next_target["negative_hint"] = candidate.get("label")
    return next_target


async def ground_feature_with_verification(session_id: str, target: Dict[str, Any], action_type: str, allow_approximate: bool = True) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []
    candidate_target = dict(target)
    for attempt_index in range(1, GROUNDING_MAX_ATTEMPTS + 1):
        grounded = await ground_feature(session_id, candidate_target, action_type, allow_approximate=allow_approximate)
        attempts.append({
            "attempt": attempt_index,
            "target_label": candidate_target.get("label"),
            "target_kind": candidate_target.get("target_kind"),
            "grounded": {
                "status": grounded.get("status"),
                "confidence": grounded.get("confidence"),
                "label": grounded.get("label"),
                "notes": grounded.get("notes"),
                "geometry": grounded.get("geometry"),
            },
        })
        status = grounded.get("status", "not_found")
        if status in {"not_visible", "ambiguous", "not_found"}:
            grounded["attempts"] = attempts
            return grounded

        verification = await verify_grounding_candidate(session_id, candidate_target, grounded, action_type)
        attempts[-1]["verification"] = verification
        decision = verification.get("decision", "retry")
        if decision == "accept":
            grounded["attempts"] = attempts
            grounded["verification"] = verification
            if verification.get("corrected_label"):
                grounded["label"] = verification["corrected_label"]
            return grounded
        if decision == "reject":
            return {
                "status": "not_visible",
                "confidence": float(verification.get("confidence", 0.0) or 0.0),
                "follow_up_prompt": "Center the target and hold it steady so I can mark it accurately.",
                "attempts": attempts,
                "verification": verification,
            }
        if attempt_index < GROUNDING_MAX_ATTEMPTS:
            candidate_target = _build_retry_target(candidate_target, verification.get("retry_hint", ""), grounded)
            continue
        return {
            "status": "ambiguous",
            "confidence": float(verification.get("confidence", 0.0) or 0.0),
            "follow_up_prompt": "Center the target and hold it steady so I can mark the exact part, not the background.",
            "attempts": attempts,
            "verification": verification,
        }

    return {
        "status": "ambiguous",
        "confidence": 0.0,
        "follow_up_prompt": "Center the target and hold it steady so I can mark it.",
        "attempts": attempts,
    }


def _normalize_value(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _normalize_geometry(geometry: Dict[str, Any]) -> Dict[str, Any]:
    gtype = geometry.get("type") or "box"
    points = []
    for point in geometry.get("points", [])[:8]:
        x = _normalize_value(point.get("x"))
        y = _normalize_value(point.get("y"))
        if x is not None and y is not None:
            points.append({"x": x, "y": y})
    normalized = {"type": gtype}
    for field in ("x", "y", "width", "height"):
        value = _normalize_value(geometry.get(field))
        if value is not None:
            normalized[field] = value
    if points:
        normalized["points"] = points
    return normalized


async def run_highlight_tool(db: AsyncSession, session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    tool_run = SessionToolRun(
        id=str(uuid.uuid4()),
        session_id=session_id,
        tool_name="highlight",
        input_data=args,
        status="running",
    )
    db.add(tool_run)
    await db.commit()

    try:
        result = await _execute_highlight_tool(session_id, args)
        tool_run.output_data = result
        tool_run.status = "success"
        tool_run.completed_at = _now()
        await db.commit()
        return result
    except Exception as exc:
        tool_run.status = "error"
        tool_run.error = str(exc)
        tool_run.completed_at = _now()
        await db.commit()
        logger.error("[%s] highlight error: %s", session_id, exc)
        return {"status": "not_found", "error": str(exc)}


async def _execute_highlight_tool(session_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    runtime = _get_runtime(session_id)
    operation = (args.get("operation") or "create").lower()
    marker_id = args.get("marker_id")

    if operation == "clear":
        if marker_id and marker_id in runtime["markers"]:
            runtime["markers"].pop(marker_id, None)
        elif marker_id == "all":
            runtime["markers"].clear()
        return {
            "status": "cleared",
            "marker_id": marker_id,
            "markers": list(runtime["markers"].values()),
        }

    if operation == "update" and marker_id and marker_id not in runtime["markers"]:
        return {
            "status": "not_found",
            "marker_id": marker_id,
            "follow_up_prompt": "The previous marker is gone, so ask me to mark it again.",
            "markers": list(runtime["markers"].values()),
        }

    if operation in {"create", "update"}:
        feature_id = args.get("feature_id")
        target_hint = (args.get("target_hint") or "").strip()
        allow_approximate = bool(args.get("allow_approximate", True))
        if operation == "update" and marker_id:
            feature_id = runtime["markers"][marker_id].get("feature_id")

        if not feature_id:
            return {
                "status": "ambiguous",
                "follow_up_prompt": "Ask the user to describe or center the target before marking it.",
                "markers": list(runtime["markers"].values()),
            }

        if feature_id == "manual:current_step":
            current_step = int(args.get("current_step", 0))
            target = get_current_step_target(session_id, current_step)
        else:
            target = runtime["feature_catalog"].get(feature_id)

        if not target and feature_id.startswith("runtime:") and target_hint:
            target = _build_runtime_target(feature_id, target_hint)
        elif not target and feature_id == "runtime:auto" and target_hint:
            target = _build_runtime_target(feature_id, target_hint)

        if not target:
            return {
                "status": "not_found",
                "feature_id": feature_id,
                "follow_up_prompt": "Ask the user to name the part or load the manual first.",
                "markers": list(runtime["markers"].values()),
            }

        grounded = await ground_feature_with_verification(session_id, target, args.get("action_type", "inspect"), allow_approximate=allow_approximate)
        status = grounded.get("status", "not_found")
        if status in {"placed", "approximate"} and not _looks_like_person_grounding(target, grounded):
            return {
                "status": "ambiguous",
                "feature_id": feature_id,
                "confidence": float(grounded.get("confidence", 0.0) or 0.0),
                "follow_up_prompt": "Keep yourself centered in the frame so I can mark you instead of the background.",
                "attempts": grounded.get("attempts", []),
                "markers": list(runtime["markers"].values()),
            }
        if status in {"not_visible", "ambiguous", "not_found"}:
            if target.get("target_kind") == "person" and status == "not_visible" and not grounded.get("follow_up_prompt"):
                grounded["follow_up_prompt"] = "Keep yourself centered in the frame so I can mark you."
            grounded["feature_id"] = feature_id
            grounded["markers"] = list(runtime["markers"].values())
            return grounded

        if feature_id == "runtime:auto":
            feature_id = _register_runtime_feature(session_id, grounded.get("label") or target_hint or "target", target_hint)
            target = runtime["feature_catalog"][feature_id]

        marker_id = marker_id or f"marker:{uuid.uuid4().hex[:10]}"
        expires_ms = int(args.get("expires_ms", 5000))
        marker = {
            "marker_id": marker_id,
            "feature_id": feature_id,
            "label": args.get("label") or grounded.get("label") or target.get("label"),
            "priority": args.get("priority") or "primary",
            "action_type": args.get("action_type") or "inspect",
            "reason": args.get("reason") or "assistant_guidance",
            "confidence": float(grounded.get("confidence", 0.0) or 0.0),
            "status": status,
            "geometry": grounded.get("geometry") or {},
            "tracking": True,
            "approximate": status == "approximate",
            "updated_at": _now().isoformat(),
            "expires_at": (_now() + timedelta(milliseconds=expires_ms)).isoformat(),
        }
        runtime["markers"][marker_id] = marker
        _trim_marker_density(runtime)
        return {
            "status": status,
            "marker_id": marker_id,
            "feature_id": feature_id,
            "geometry": marker["geometry"],
            "confidence": marker["confidence"],
            "follow_up_prompt": grounded.get("follow_up_prompt"),
            "attempts": grounded.get("attempts", []),
            "verification": grounded.get("verification"),
            "markers": list(runtime["markers"].values()),
        }

    return {
        "status": "not_found",
        "follow_up_prompt": "Unsupported marker operation.",
        "markers": list(runtime["markers"].values()),
    }
