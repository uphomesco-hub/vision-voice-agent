from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from db import AsyncSessionLocal, init_db
from hud_planner import (
    clear_hud_planner_state,
    is_hud_worthy_user_request,
    plan_hud_action,
    run_hud_planner,
)
from hud_runtime import clear_runtime, set_latest_frame


def test_hud_worthy_user_request_detection():
    assert is_hud_worthy_user_request("which screw should I remove?")
    assert is_hud_worthy_user_request("mark what I am holding")
    assert is_hud_worthy_user_request("what should I do now?")
    assert not is_hud_worthy_user_request("thanks")


@pytest.mark.asyncio
async def test_planner_skips_without_live_frame():
    session_id = "hud-planner-no-frame"
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)

    result = await plan_hud_action(
        session_id=session_id,
        event="user_turn",
        user_text="mark this screw",
        force=True,
    )

    assert result["status"] == "skipped"
    assert result["decision"] == "none"
    assert "frame" in result["reason"].lower()
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)


@pytest.mark.asyncio
async def test_planner_fallback_routes_explicit_request_without_api_key(monkeypatch):
    session_id = "hud-planner-explicit"
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)
    set_latest_frame(session_id, "fake-frame")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = await plan_hud_action(
        session_id=session_id,
        event="user_turn",
        user_text="mark what I am holding",
        force=True,
    )

    assert result["status"] == "planned"
    assert result["decision"] == "mark"
    assert result["intent"]["feature_id"] == "runtime:auto"
    assert result["intent"]["target_hint"] == "mark what I am holding"
    assert result["intent"]["reason"] == "user_request"
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)


@pytest.mark.asyncio
async def test_model_plan_below_threshold_does_not_auto_mark(monkeypatch):
    session_id = "hud-planner-low-confidence"
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)
    set_latest_frame(session_id, "fake-frame")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_generate(_prompt, _frame_b64):
        return {
            "decision": "mark",
            "confidence": 0.51,
            "reason": "Maybe useful but uncertain.",
            "intent": {
                "operation": "create",
                "feature_id": "runtime:auto",
                "target_hint": "small dark area",
            },
        }

    monkeypatch.setattr("hud_planner._generate_plan_json", fake_generate)

    result = await plan_hud_action(
        session_id=session_id,
        event="vision_update",
        observation={"device_state": "device in frame"},
        force=False,
    )

    assert result["status"] == "planned"
    assert result["decision"] == "none"
    assert result["intent"] == {}
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)


@pytest.mark.asyncio
async def test_run_hud_planner_executes_verified_highlight(monkeypatch):
    await init_db()
    session_id = "hud-planner-executes"
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)
    set_latest_frame(session_id, "fake-frame")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_generate(_prompt, _frame_b64):
        return {
            "decision": "mark",
            "confidence": 0.91,
            "reason": "The user asked which screw.",
            "intent": {
                "operation": "create",
                "feature_id": "runtime:auto",
                "target_hint": "rear screw",
                "action_type": "unscrew_ccw",
                "reason": "user_request",
            },
        }

    async def fake_highlight(_db, _session_id, args):
        return {
            "status": "placed",
            "marker_id": "marker:test",
            "feature_id": "runtime:rear_screw",
            "confidence": 0.9,
            "attempts": [{"attempt": 1}],
            "args": args,
        }

    monkeypatch.setattr("hud_planner._generate_plan_json", fake_generate)
    monkeypatch.setattr("hud_planner.run_highlight_tool", fake_highlight)

    async with AsyncSessionLocal() as db:
        result = await run_hud_planner(
            db,
            session_id=session_id,
            event="user_turn",
            user_text="which screw?",
            force=True,
        )

    assert result["decision"] == "mark"
    assert result["highlight_result"]["status"] == "placed"
    assert result["highlight_result"]["args"]["target_hint"] == "rear screw"
    assert result["highlight_result"]["args"]["current_step"] == 0
    clear_runtime(session_id)
    clear_hud_planner_state(session_id)
