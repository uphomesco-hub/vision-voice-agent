from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import Manual, SessionToolRun
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import uuid
import json
import logging
import os

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
        searchable = " ".join([
            m.brand, m.model, m.device_type, m.title,
            " ".join(jd.get("aliases", [])),
            " ".join(jd.get("issue_keywords", [])),
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

    results = []
    for score, m in scored[:5]:
        jd = m.json_data or {}
        results.append({
            "manual_id": m.id,
            "brand": m.brand,
            "model": m.model,
            "device_type": m.device_type,
            "title": m.title,
            "score": score,
            "warnings": jd.get("warnings", []),
            "troubleshooting_steps": jd.get("troubleshooting_steps", []),
            "tools_required": jd.get("tools_required", []),
            "common_failures": jd.get("common_failures", []),
            "success_signs": jd.get("success_signs", []),
            "stop_conditions": jd.get("stop_conditions", []),
            "inspection_prompts": jd.get("inspection_prompts_for_agent", []),
            "follow_up_questions": jd.get("follow_up_questions", []),
        })

    return results


async def get_manual_by_id(db: AsyncSession, manual_id: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(select(Manual).where(Manual.id == manual_id))
    m = result.scalar_one_or_none()
    if not m:
        return None
    return {"manual_id": m.id, "brand": m.brand, "model": m.model, "device_type": m.device_type, "title": m.title, **m.json_data}


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
            "warnings": selected["warnings"] if selected else [],
            "troubleshooting_steps": selected["troubleshooting_steps"][:5] if selected else [],
            "tools_required": selected["tools_required"] if selected else [],
            "inspection_prompts": selected["inspection_prompts"][:3] if selected else [],
            "success_signs": selected["success_signs"] if selected else [],
            "stop_conditions": selected["stop_conditions"] if selected else [],
            "common_failures": selected["common_failures"][:3] if selected else [],
            "follow_up_questions": selected["follow_up_questions"][:3] if selected else [],
            "alternatives": [{"manual_id": r["manual_id"], "title": r["title"], "score": r["score"]} for r in results[1:3]],
        }
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
