from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import Manual, SessionToolRun
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import uuid
import json
import logging
import os

from hud_runtime import build_manual_hud_bundle

logger = logging.getLogger(__name__)

MANUALS_DIR = os.path.join(os.path.dirname(__file__), "manuals")


async def seed_manuals(db: AsyncSession):
    """Load bundled JSON manuals from manuals/ directory into DB."""
    if not os.path.isdir(MANUALS_DIR):
        os.makedirs(MANUALS_DIR, exist_ok=True)
        return
    for fname in os.listdir(MANUALS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(MANUALS_DIR, fname)
        with open(path, "r") as f:
            data = json.load(f)
        manual_id = data.get("id", fname.replace(".json", ""))
        existing = await db.execute(select(Manual).where(Manual.id == manual_id))
        if existing.scalar_one_or_none():
            continue
        manual = Manual(
            id=manual_id,
            brand=data.get("brand", ""),
            model=data.get("model", ""),
            device_type=data.get("device_type", ""),
            title=data.get("title", ""),
            json_data=data,
        )
        db.add(manual)
        logger.info(f"Seeded manual: {manual.title}")
    await db.commit()


async def search_manuals(
    db: AsyncSession,
    brand: str = "",
    model: str = "",
    device_type: str = "",
    issue: str = "",
    query: str = "",
) -> List[Dict[str, Any]]:
    """Search manuals with fuzzy matching. Returns ranked list."""
    result = await db.execute(select(Manual))
    all_manuals = result.scalars().all()

    scored = []
    search_terms = (brand + " " + model + " " + device_type + " " + issue + " " + query).lower().split()

    for m in all_manuals:
        score = 0
        jd = m.json_data or {}
        # Support both manual formats
        aliases = jd.get("aliases", [])
        issue_kw = jd.get("issue_keywords", [])
        search_text = jd.get("search_text", "")
        # Build searchable text from all available fields
        searchable = " ".join([
            m.brand, m.model, m.device_type, m.title,
            " ".join(aliases),
            " ".join(issue_kw),
            search_text,
            jd.get("summary", ""),
        ]).lower()

        if brand and brand.lower() in searchable:
            score += 30
        if model and model.lower() in searchable:
            score += 40
        if device_type and device_type.lower() in searchable:
            score += 20
        for term in search_terms:
            if len(term) > 2 and term in searchable:
                score += 5

        if score > 0:
            scored.append((score, m))

    scored.sort(key=lambda x: -x[0])

    # Minimum score threshold — don't return garbage matches
    MIN_SCORE = 20
    scored = [(s, m) for s, m in scored if s >= MIN_SCORE]

    results = []
    for score, m in scored[:5]:
        jd = m.json_data or {}
        # Extract troubleshooting steps from either format
        ts_steps = jd.get("troubleshooting_steps", [])
        if not ts_steps and jd.get("troubleshooting"):
            ts_steps = [{"step": i+1, "title": t.get("issue", ""), "action": "; ".join(t.get("recommended_actions", []))} for i, t in enumerate(jd["troubleshooting"])]
        # Extract teardown steps if available
        teardown = jd.get("teardown", {})
        teardown_steps = teardown.get("steps", []) if isinstance(teardown, dict) else []
        # Extract warnings from either format
        warnings = jd.get("warnings", [])
        if not warnings and jd.get("safety", {}).get("warnings"):
            warnings = jd["safety"]["warnings"]
        # Extract tools from either format
        tools = jd.get("tools_required", [])
        if isinstance(tools, dict):
            tools = [t.get("name", "") + " — " + t.get("purpose", "") for t in tools.get("required", [])]
        # Extract repair playbooks
        playbooks = jd.get("repair_playbooks", [])

        results.append({
            "manual_id": m.id,
            "brand": m.brand,
            "model": m.model,
            "device_type": m.device_type,
            "title": m.title,
            "score": score,
            "summary": jd.get("summary", ""),
            "warnings": warnings,
            "troubleshooting_steps": ts_steps,
            "teardown_steps": teardown_steps,
            "tools_required": tools,
            "common_failures": jd.get("common_failures", []),
            "success_signs": jd.get("success_signs", []),
            "stop_conditions": jd.get("stop_conditions", []),
            "inspection_prompts": jd.get("inspection_prompts_for_agent", []),
            "follow_up_questions": jd.get("follow_up_questions", []),
            "repair_playbooks": [{"issue": p.get("issue", ""), "goal": p.get("goal", ""), "steps": p.get("steps", [])} for p in playbooks[:3]],
            "hidden_tips": [t.get("tip", "") for t in jd.get("hidden_tips", [])],
            "screws": jd.get("screws", {}),
            "hidden_clips": jd.get("hidden_clips", {}),
        })

    return results


async def get_manual_by_id(db: AsyncSession, manual_id: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(select(Manual).where(Manual.id == manual_id))
    m = result.scalar_one_or_none()
    if not m:
        return None
    manual = {"manual_id": m.id, "brand": m.brand, "model": m.model, "device_type": m.device_type, "title": m.title, **m.json_data}
    manual.update(build_manual_hud_bundle(manual))
    return manual


async def lookup_manual_tool(
    db: AsyncSession,
    session_id: str,
    brand: str = "",
    model: str = "",
    device_type: str = "",
    issue: str = "",
    query: str = "",
) -> Dict[str, Any]:
    """Execute the lookup_manual tool — search DB, log the run, return structured result."""
    tool_run = SessionToolRun(
        id=str(uuid.uuid4()),
        session_id=session_id,
        tool_name="lookup_manual",
        input_data={"brand": brand, "model": model, "device_type": device_type, "issue": issue, "query": query},
        status="running",
    )
    db.add(tool_run)
    await db.commit()

    try:
        results = await search_manuals(db, brand, model, device_type, issue, query)
        selected = results[0] if results else None
        output = {
            "found_count": len(results),
            "selected_manual_id": selected["manual_id"] if selected else None,
            "manual_summary": f"{selected['brand']} {selected['model']} — {selected['title']}" if selected else "No manual found",
            "summary": selected.get("summary", "") if selected else "",
            "warnings": selected["warnings"] if selected else [],
            "troubleshooting_steps": selected["troubleshooting_steps"][:6] if selected else [],
            "teardown_steps": selected.get("teardown_steps", [])[:8] if selected else [],
            "tools_required": selected["tools_required"] if selected else [],
            "inspection_prompts": selected["inspection_prompts"][:5] if selected else [],
            "success_signs": selected.get("success_signs", []) if selected else [],
            "stop_conditions": selected.get("stop_conditions", []) if selected else [],
            "common_failures": selected.get("common_failures", [])[:3] if selected else [],
            "follow_up_questions": selected.get("follow_up_questions", [])[:3] if selected else [],
            "repair_playbooks": selected.get("repair_playbooks", []) if selected else [],
            "hidden_tips": selected.get("hidden_tips", []) if selected else [],
            "screws": selected.get("screws", {}) if selected else {},
            "hidden_clips": selected.get("hidden_clips", {}) if selected else {},
            "alternatives": [{"manual_id": r["manual_id"], "title": r["title"], "score": r["score"]} for r in results[1:3]],
        }
        if selected:
            manual = await get_manual_by_id(db, selected["manual_id"])
            output["hud_features"] = manual.get("hud_features", [])
            output["step_targets"] = manual.get("step_targets", [])
            output["completion_checks"] = manual.get("completion_checks", {})
        else:
            output["hud_features"] = []
            output["step_targets"] = []
            output["completion_checks"] = {}
        tool_run.output_data = output
        tool_run.status = "success"
        tool_run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info(f"[{session_id}] lookup_manual found {len(results)} result(s)")
        return output

    except Exception as e:
        tool_run.status = "error"
        tool_run.error = str(e)
        tool_run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.error(f"[{session_id}] lookup_manual error: {e}")
        return {"found_count": 0, "error": str(e)}
