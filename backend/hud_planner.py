import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from db import SessionToolRun
from hud_runtime import build_hud_snapshot, get_runtime_state, run_highlight_tool

logger = logging.getLogger(__name__)

HUD_PLANNER_MODEL = os.environ.get("HUD_PLANNER_MODEL", "gemini-2.5-flash")
AUTO_PLAN_COOLDOWN_SECONDS = 8
AUTO_MARK_CONFIDENCE = 0.72
FORCED_MARK_CONFIDENCE = 0.45

_planner_state: Dict[str, Dict[str, Any]] = {}

HUD_WORTHY_PATTERNS = [
    r"\bmark\b",
    r"\bhighlight\b",
    r"\bpoint (to|at|out)\b",
    r"\bcircle\b",
    r"\boutline\b",
    r"\bshow me which\b",
    r"\bwhich (one|screw|part|side|button|clip)\b",
    r"\bwhere (is|are|do|should)\b",
    r"\bwhat (am i holding|i am holding|i'm holding|is in my hand)\b",
    r"\bwhat should i do now\b",
    r"\bnext step\b",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clear_hud_planner_state(session_id: str):
    _planner_state.pop(session_id, None)


def is_hud_worthy_user_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in HUD_WORTHY_PATTERNS)


def _get_state(session_id: str) -> Dict[str, Any]:
    if session_id not in _planner_state:
        _planner_state[session_id] = {
            "last_planned_at": None,
            "last_signature": "",
            "last_intent_key": "",
        }
    return _planner_state[session_id]


def _planner_url() -> str:
    key = os.environ.get("GOOGLE_API_KEY", "")
    return f"https://generativelanguage.googleapis.com/v1beta/models/{HUD_PLANNER_MODEL}:generateContent?key={key}"


async def _generate_plan_json(prompt: str, frame_b64: str) -> Dict[str, Any]:
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": frame_b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(_planner_url(), json=payload)
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)


def _compact_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "markers": [
            {
                "marker_id": item.get("marker_id"),
                "feature_id": item.get("feature_id"),
                "label": item.get("label"),
                "action_type": item.get("action_type"),
                "status": item.get("status"),
            }
            for item in snapshot.get("markers", [])[:4]
        ],
        "current_step_target": snapshot.get("current_step_target"),
        "feature_catalog": [
            {
                "feature_id": item.get("feature_id"),
                "label": item.get("label"),
                "category": item.get("category"),
            }
            for item in snapshot.get("feature_catalog", [])[:20]
        ],
    }


def _make_signature(event: str, user_text: str, current_step: int, observation: Optional[Dict[str, Any]], snapshot: Dict[str, Any]) -> str:
    current = snapshot.get("current_step_target") or {}
    payload = {
        "event": event,
        "user_text": (user_text or "").strip().lower()[:180],
        "current_step": current_step,
        "current_feature": current.get("primary_feature_id"),
        "observation": {
            "device_state": (observation or {}).get("device_state", ""),
            "focus_area": (observation or {}).get("focus_area", ""),
            "safety_concern": (observation or {}).get("safety_concern", ""),
            "changes": (observation or {}).get("changed_vs_prior", [])[:4],
        },
        "markers": [
            {
                "feature_id": marker.get("feature_id"),
                "label": marker.get("label"),
            }
            for marker in snapshot.get("markers", [])[:4]
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _within_cooldown(state: Dict[str, Any], signature: str) -> bool:
    if state.get("last_signature") != signature:
        return False
    last = state.get("last_planned_at")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return False
    return _now() - last_dt < timedelta(seconds=AUTO_PLAN_COOLDOWN_SECONDS)


def _sanitize_operation(value: str) -> str:
    value = (value or "create").lower()
    return value if value in {"create", "update", "clear"} else "create"


def _sanitize_action_type(value: str) -> str:
    value = (value or "inspect").lower()
    allowed = {"inspect", "unscrew_ccw", "pull", "pry", "lift", "slide", "avoid", "hold_here", "danger"}
    return value if value in allowed else "inspect"


def _sanitize_reason(value: str, event: str) -> str:
    value = (value or "").lower()
    allowed = {"user_request", "manual_step", "safety", "assistant_guidance"}
    if value in allowed:
        return value
    if event == "vision_update":
        return "assistant_guidance"
    return "user_request"


def _intent_key(intent: Dict[str, Any]) -> str:
    return json.dumps({
        "operation": intent.get("operation"),
        "feature_id": intent.get("feature_id"),
        "target_hint": (intent.get("target_hint") or "").strip().lower(),
        "action_type": intent.get("action_type"),
    }, sort_keys=True)


def _marker_already_covers_intent(snapshot: Dict[str, Any], intent: Dict[str, Any]) -> bool:
    if intent.get("operation") != "create":
        return False
    feature_id = intent.get("feature_id")
    target_hint = (intent.get("target_hint") or "").strip().lower()
    for marker in snapshot.get("markers", []):
        marker_feature = marker.get("feature_id")
        marker_label = (marker.get("label") or "").strip().lower()
        if feature_id and feature_id != "runtime:auto" and marker_feature == feature_id:
            return True
        if target_hint and marker_label and (target_hint in marker_label or marker_label in target_hint):
            return True
    return False


def _fallback_plan(event: str, user_text: str, current_step: int, snapshot: Dict[str, Any], force: bool) -> Dict[str, Any]:
    if force and is_hud_worthy_user_request(user_text):
        return {
            "decision": "mark",
            "confidence": 0.68,
            "reason": "Explicit user request can be routed through verified HUD placement without a planner model.",
            "intent": {
                "operation": "create",
                "feature_id": "runtime:auto",
                "target_hint": user_text.strip(),
                "action_type": "inspect",
                "reason": "user_request",
                "priority": "primary",
                "expires_ms": 6500,
                "allow_approximate": True,
            },
            "assistant_hint": "",
        }
    current_target = snapshot.get("current_step_target")
    if event in {"manual_step", "vision_update"} and current_target and force:
        return {
            "decision": "mark",
            "confidence": 0.74,
            "reason": "Current manual step has a stable HUD target.",
            "intent": {
                "operation": "create",
                "feature_id": "manual:current_step",
                "target_hint": current_target.get("title", ""),
                "action_type": current_target.get("action_type", "inspect"),
                "reason": "manual_step",
                "priority": "primary",
                "expires_ms": 6500,
                "allow_approximate": True,
                "current_step": current_step,
            },
            "assistant_hint": "",
        }
    return {
        "decision": "none",
        "confidence": 0.0,
        "reason": "No planner model is configured and no forced HUD target was obvious.",
        "intent": {},
        "assistant_hint": "",
    }


def _normalize_plan(plan: Dict[str, Any], event: str, user_text: str, current_step: int, snapshot: Dict[str, Any], force: bool) -> Dict[str, Any]:
    decision = (plan.get("decision") or "none").lower()
    if decision not in {"mark", "clear", "none", "ask_user"}:
        decision = "none"
    try:
        confidence = float(plan.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    intent = dict(plan.get("intent") or {})
    threshold = FORCED_MARK_CONFIDENCE if force else AUTO_MARK_CONFIDENCE
    if decision == "mark" and confidence < threshold:
        decision = "ask_user" if force else "none"

    if decision == "mark":
        intent["operation"] = _sanitize_operation(intent.get("operation", "create"))
        intent["feature_id"] = intent.get("feature_id") or "runtime:auto"
        intent["target_hint"] = (intent.get("target_hint") or user_text or "").strip()
        intent["action_type"] = _sanitize_action_type(intent.get("action_type", "inspect"))
        intent["reason"] = _sanitize_reason(intent.get("reason", ""), event)
        intent["priority"] = intent.get("priority") if intent.get("priority") in {"primary", "secondary"} else "primary"
        intent["expires_ms"] = int(intent.get("expires_ms") or 6500)
        intent["allow_approximate"] = bool(intent.get("allow_approximate", True))
        intent["current_step"] = current_step
        if intent["feature_id"] == "manual:current_step" and not intent["target_hint"]:
            current_target = snapshot.get("current_step_target") or {}
            intent["target_hint"] = current_target.get("title", "")
    elif decision == "clear":
        intent = {
            "operation": "clear",
            "marker_id": intent.get("marker_id") or "all",
        }
    else:
        intent = {}

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": plan.get("reason", ""),
        "intent": intent,
        "assistant_hint": plan.get("assistant_hint", ""),
    }


async def plan_hud_action(
    session_id: str,
    event: str,
    user_text: str = "",
    current_step: int = 0,
    observation: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    runtime = get_runtime_state(session_id)
    frame_b64 = runtime.get("latest_frame_b64")
    snapshot = build_hud_snapshot(session_id, current_step_index=current_step)
    state = _get_state(session_id)
    signature = _make_signature(event, user_text, current_step, observation, snapshot)

    if not frame_b64:
        return {
            "status": "skipped",
            "decision": "none",
            "confidence": 0.0,
            "reason": "No live camera frame is available for HUD planning.",
            "intent": {},
        }
    if not force and _within_cooldown(state, signature):
        return {
            "status": "skipped",
            "decision": "none",
            "confidence": 0.0,
            "reason": "HUD planner cooldown suppressed repeated work.",
            "intent": {},
        }

    if not os.environ.get("GOOGLE_API_KEY", ""):
        raw_plan = _fallback_plan(event, user_text, current_step, snapshot, force)
    else:
        prompt = (
            "You are the repair HUD planner. You do not speak to the user. You inspect the latest camera frame and decide "
            "whether a visual marker would make the assistant more useful.\n\n"
            "Return STRICT JSON only with this schema:\n"
            "{\n"
            '  "decision": "mark|clear|none|ask_user",\n'
            '  "confidence": 0.0,\n'
            '  "reason": "brief reason",\n'
            '  "intent": {\n'
            '    "operation": "create|update|clear",\n'
            '    "feature_id": "manual:current_step|runtime:auto|manual:...",\n'
            '    "marker_id": "existing marker id or all",\n'
            '    "target_hint": "specific visual target to mark",\n'
            '    "action_type": "inspect|unscrew_ccw|pull|pry|lift|slide|avoid|hold_here|danger",\n'
            '    "reason": "user_request|manual_step|safety|assistant_guidance",\n'
            '    "priority": "primary|secondary",\n'
            '    "expires_ms": 6500,\n'
            '    "allow_approximate": true\n'
            "  },\n"
            '  "assistant_hint": "short internal hint or empty string"\n'
            "}\n\n"
            "Rules:\n"
            "- If the user explicitly asks which thing, where something is, or asks to mark/highlight/point/circle, prefer decision=mark.\n"
            "- If the user asks what to do next and a manual current_step_target exists, mark that target.\n"
            "- For general scene changes, mark only high-value repair targets, hazards, or the next manual target.\n"
            "- Do not mark decorative background objects, random furniture, or uncertain guesses.\n"
            "- If the target is unclear but the user explicitly asked, use decision=ask_user unless a reasonable target_hint can be verified by the executor.\n"
            "- Prefer updating or clearing existing markers over clutter.\n"
            "- The executor will verify and retry marker placement, so your job is to choose the target intent, not exact coordinates.\n\n"
            f"Planner event: {event}\n"
            f"Force planning: {force}\n"
            f"User text: {user_text or ''}\n"
            f"Current step index: {current_step}\n"
            f"Observation JSON: {json.dumps(observation or {}, ensure_ascii=False)}\n"
            f"HUD context JSON: {json.dumps(_compact_snapshot(snapshot), ensure_ascii=False)}"
        )
        try:
            raw_plan = await _generate_plan_json(prompt, frame_b64)
        except Exception as exc:
            logger.error("[%s] HUD planner failed: %s", session_id, exc)
            raw_plan = _fallback_plan(event, user_text, current_step, snapshot, force)

    plan = _normalize_plan(raw_plan, event, user_text, current_step, snapshot, force)
    intent_key = _intent_key(plan.get("intent", {}))
    if plan["decision"] == "mark" and not force:
        if _marker_already_covers_intent(snapshot, plan["intent"]) or state.get("last_intent_key") == intent_key:
            plan.update({
                "status": "skipped",
                "decision": "none",
                "reason": "Existing HUD marker already covers this target.",
                "intent": {},
            })
            return plan

    state["last_planned_at"] = _now().isoformat()
    state["last_signature"] = signature
    if intent_key:
        state["last_intent_key"] = intent_key
    plan["status"] = "planned"
    return plan


async def run_hud_planner(
    db: AsyncSession,
    session_id: str,
    event: str,
    user_text: str = "",
    current_step: int = 0,
    observation: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    input_data = {
        "event": event,
        "user_text": user_text,
        "current_step": current_step,
        "observation": observation or {},
        "force": force,
    }
    tool_run = SessionToolRun(
        id=str(uuid.uuid4()),
        session_id=session_id,
        tool_name="hud_planner",
        input_data=input_data,
        status="running",
    )
    db.add(tool_run)
    await db.commit()

    try:
        plan = await plan_hud_action(
            session_id=session_id,
            event=event,
            user_text=user_text,
            current_step=current_step,
            observation=observation,
            force=force,
        )
        if plan.get("decision") in {"mark", "clear"} and plan.get("intent"):
            highlight_result = await run_highlight_tool(db, session_id, plan["intent"])
            plan["highlight_result"] = highlight_result
        tool_run.output_data = plan
        tool_run.status = "success"
        tool_run.completed_at = _now()
        await db.commit()
        return plan
    except Exception as exc:
        tool_run.status = "error"
        tool_run.error = str(exc)
        tool_run.completed_at = _now()
        await db.commit()
        logger.error("[%s] hud_planner error: %s", session_id, exc)
        return {
            "status": "error",
            "decision": "none",
            "confidence": 0.0,
            "reason": str(exc),
            "intent": {},
        }
