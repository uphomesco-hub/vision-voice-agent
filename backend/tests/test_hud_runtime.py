from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from db import AsyncSessionLocal, init_db
from hud_runtime import (
    build_hud_snapshot,
    clear_runtime,
    evaluate_step_completion,
    get_runtime_state,
    refresh_tracked_markers,
    run_highlight_tool,
    set_hud_prompt,
    set_last_perception,
    set_latest_frame,
    sync_manual_bundle,
)
from manual_repo import get_manual_by_id, seed_manuals


@pytest.mark.asyncio
async def test_manual_bundle_exposes_stable_feature_ids():
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_manuals(db)
        manual = await get_manual_by_id(db, "morphy-richards-trimmer-generic-v2")

    feature_ids = {item["feature_id"] for item in manual["hud_features"]}
    assert "manual:rear_visible_1" in feature_ids
    assert "manual:front_hidden_1_2" in feature_ids
    assert "manual:left_side_seam" in feature_ids

    step_targets = {item["step"]: item for item in manual["step_targets"]}
    assert step_targets[3]["primary_feature_id"] == "manual:rear_visible_1"
    assert step_targets[7]["action_type"] == "unscrew_ccw"
    assert "manual:right_side_seam" in step_targets[8]["support_feature_ids"]


@pytest.mark.asyncio
async def test_highlight_tool_create_update_clear(monkeypatch):
    session_id = "hud-test-session"
    clear_runtime(session_id)
    set_latest_frame(session_id, "fake-frame")

    async def fake_ground_feature(_session_id, target, action_type, allow_approximate=True):
        return {
            "status": "placed",
            "confidence": 0.91,
            "label": target.get("label", "Rear screw"),
            "geometry": {"type": "box", "x": 0.1, "y": 0.2, "width": 0.2, "height": 0.16},
        }

    async def fake_verify(_session_id, _target, _candidate, _action_type):
        return {
            "decision": "accept",
            "confidence": 0.91,
            "reason": "Candidate matches target.",
            "corrected_label": "Rear screw",
            "retry_hint": "",
        }

    monkeypatch.setattr("hud_runtime.ground_feature", fake_ground_feature)
    monkeypatch.setattr("hud_runtime.verify_grounding_candidate", fake_verify)

    async with AsyncSessionLocal() as db:
        create_result = await run_highlight_tool(db, session_id, {
            "operation": "create",
            "feature_id": "runtime:auto",
            "target_hint": "rear screw",
            "action_type": "inspect",
            "reason": "user_request",
        })
        assert create_result["status"] == "placed"
        marker_id = create_result["marker_id"]
        snapshot = build_hud_snapshot(session_id, current_step_index=0)
        assert len(snapshot["markers"]) == 1

        update_result = await run_highlight_tool(db, session_id, {
            "operation": "update",
            "marker_id": marker_id,
            "action_type": "unscrew_ccw",
            "reason": "assistant_guidance",
        })
        assert update_result["status"] == "placed"

        clear_result = await run_highlight_tool(db, session_id, {
            "operation": "clear",
            "marker_id": marker_id,
        })
        assert clear_result["status"] == "cleared"
        assert build_hud_snapshot(session_id, current_step_index=0)["markers"] == []

    clear_runtime(session_id)


@pytest.mark.asyncio
async def test_highlight_tool_rejects_background_object_for_mark_me(monkeypatch):
    session_id = "hud-person-session"
    clear_runtime(session_id)
    set_latest_frame(session_id, "fake-frame")

    async def fake_ground_feature(_session_id, _target, _action_type, allow_approximate=True):
        return {
            "status": "placed",
            "confidence": 0.92,
            "label": "Wall fixture",
            "notes": "Small wall hook on the back wall",
            "geometry": {"type": "box", "x": 0.28, "y": 0.25, "width": 0.05, "height": 0.06},
        }

    monkeypatch.setattr("hud_runtime.ground_feature", fake_ground_feature)

    async with AsyncSessionLocal() as db:
        result = await run_highlight_tool(db, session_id, {
            "operation": "create",
            "feature_id": "runtime:auto",
            "target_hint": "user",
            "action_type": "inspect",
            "reason": "user_request",
        })
        assert result["status"] == "ambiguous"
        assert "center" in result["follow_up_prompt"].lower()
        assert build_hud_snapshot(session_id, current_step_index=0)["markers"] == []

    clear_runtime(session_id)


@pytest.mark.asyncio
async def test_highlight_tool_retries_wrong_candidate_then_commits(monkeypatch):
    session_id = "hud-retry-session"
    clear_runtime(session_id)
    set_latest_frame(session_id, "fake-frame")
    calls = {"count": 0}

    async def fake_ground_feature(_session_id, _target, _action_type, allow_approximate=True):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "placed",
                "confidence": 0.6,
                "label": "Wall hook",
                "notes": "Small wall fixture",
                "geometry": {"type": "box", "x": 0.2, "y": 0.1, "width": 0.04, "height": 0.04},
            }
        return {
            "status": "placed",
            "confidence": 0.87,
            "label": "AirPods case",
            "notes": "Object held in hand",
            "geometry": {"type": "box", "x": 0.45, "y": 0.35, "width": 0.18, "height": 0.16},
        }

    async def fake_verify(_session_id, _target, candidate, _action_type):
        if candidate.get("label") == "Wall hook":
            return {
                "decision": "retry",
                "confidence": 0.6,
                "reason": "Wrong object.",
                "corrected_label": "",
                "retry_hint": "the object in the user's hand near the fingers",
            }
        return {
            "decision": "accept",
            "confidence": 0.87,
            "reason": "Correct held object.",
            "corrected_label": "AirPods case",
            "retry_hint": "",
        }

    monkeypatch.setattr("hud_runtime.ground_feature", fake_ground_feature)
    monkeypatch.setattr("hud_runtime.verify_grounding_candidate", fake_verify)

    async with AsyncSessionLocal() as db:
        result = await run_highlight_tool(db, session_id, {
            "operation": "create",
            "feature_id": "runtime:auto",
            "target_hint": "what I am holding",
            "action_type": "inspect",
            "reason": "user_request",
        })
        assert result["status"] == "placed"
        assert result["feature_id"].startswith("runtime:")
        assert result["verification"]["decision"] == "accept"
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["verification"]["decision"] == "retry"
        assert build_hud_snapshot(session_id, current_step_index=0)["markers"][0]["label"] == "AirPods case"

    clear_runtime(session_id)


@pytest.mark.asyncio
async def test_tracked_marker_updates_geometry_and_history(monkeypatch):
    session_id = "hud-tracking-session"
    clear_runtime(session_id)
    set_latest_frame(session_id, "fake-frame")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    calls = {"count": 0}

    async def fake_ground_feature(_session_id, target, _action_type, allow_approximate=True):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "placed",
                "confidence": 0.9,
                "label": target.get("label", "Rear screw"),
                "geometry": {"type": "box", "x": 0.1, "y": 0.2, "width": 0.2, "height": 0.16},
            }
        return {
            "status": "placed",
            "confidence": 0.86,
            "label": "Rear screw",
            "geometry": {"type": "box", "x": 0.5, "y": 0.4, "width": 0.2, "height": 0.16},
        }

    async def fake_verify(_session_id, _target, candidate, _action_type):
        return {
            "decision": "accept",
            "confidence": candidate.get("confidence", 0.8),
            "reason": "Candidate matches target.",
            "corrected_label": candidate.get("label"),
            "retry_hint": "",
        }

    monkeypatch.setattr("hud_runtime.ground_feature", fake_ground_feature)
    monkeypatch.setattr("hud_runtime.verify_grounding_candidate", fake_verify)

    async with AsyncSessionLocal() as db:
        result = await run_highlight_tool(db, session_id, {
            "operation": "create",
            "feature_id": "runtime:auto",
            "target_hint": "rear screw",
            "action_type": "inspect",
            "reason": "user_request",
        })
        marker_id = result["marker_id"]
        runtime = get_runtime_state(session_id)
        runtime["markers"][marker_id]["last_tracked_at"] = "2020-01-01T00:00:00+00:00"
        tracking = await refresh_tracked_markers(session_id)

    marker = build_hud_snapshot(session_id, current_step_index=0)["markers"][0]
    assert tracking["updated"] == 1
    assert 0.1 < marker["geometry"]["x"] < 0.5
    assert marker["track_count"] == 1
    assert marker["confidence_level"] == "high"
    assert any(item["event"] == "tracked" for item in runtime["marker_history"])
    clear_runtime(session_id)


def test_hud_snapshot_includes_prompt_and_history():
    session_id = "hud-prompt-session"
    clear_runtime(session_id)
    set_hud_prompt(session_id, "Center the screw area", "warning")
    snapshot = build_hud_snapshot(session_id, current_step_index=0)
    assert snapshot["hud_prompt"]["message"] == "Center the screw area"
    assert snapshot["marker_history"] == []
    clear_runtime(session_id)


@pytest.mark.asyncio
async def test_visual_completion_check_advances_on_verified_observation():
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_manuals(db)
        manual = await get_manual_by_id(db, "morphy-richards-trimmer-generic-v2")

    session_id = "step-check-session"
    clear_runtime(session_id)
    sync_manual_bundle(session_id, manual)
    observation = {
        "device_state": "front opening visible with two screw holes empty",
        "visible_features": ["two screw holes empty", "front cavity exposed"],
        "changed_vs_prior": ["two screw holes empty"],
        "objects": ["trimmer housing"],
    }
    set_last_perception(session_id, observation)
    status, checks = evaluate_step_completion(session_id, 6, observation)
    assert status == "verified"
    assert "hidden screws" in checks["fallback_prompt"].lower()
    clear_runtime(session_id)
