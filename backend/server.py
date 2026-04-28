from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
import asyncio
import json
import os
import base64
import io
import websockets
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from PIL import Image
import re

from db import init_db, get_db, AsyncSessionLocal, SessionSnapshot
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id
from hud_runtime import (
    build_hud_snapshot,
    clear_pending_confirmation,
    clear_runtime,
    evaluate_step_completion,
    get_pending_confirmation_prompt,
    get_runtime_state,
    maybe_track_pending_confirmation,
    run_highlight_tool,
    refresh_tracked_markers,
    set_last_perception,
    set_latest_frame,
    should_highlight_current_step,
    sync_manual_bundle,
    user_confirms_step,
)
from hud_planner import (
    clear_hud_planner_state,
    is_hud_worthy_user_request,
    run_hud_planner,
)
from nudge_engine import NudgeEngine
from vision_perception import perceive_scene, observation_signature

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-native-audio-latest"
INPUT_SAMPLE_RATE = 16000
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

# ─── Frame Diff ──────────────────────
DIFF_THUMB_SIZE = (32, 32)
DIFF_THRESHOLD = 12.0
DIFF_COOLDOWN_FRAMES = 1

def compute_frame_diff(prev_bytes, curr_bytes):
    """Compare two JPEG blobs as tiny grayscale thumbnails. Returns mean pixel diff 0-255."""
    try:
        prev = Image.open(io.BytesIO(prev_bytes)).convert("L").resize(DIFF_THUMB_SIZE)
        curr = Image.open(io.BytesIO(curr_bytes)).convert("L").resize(DIFF_THUMB_SIZE)
        pp, cp = list(prev.getdata()), list(curr.getdata())
        return sum(abs(a - b) for a, b in zip(pp, cp)) / len(pp)
    except Exception:
        return 0.0

# ─── Perception state cache (survives WS reconnects within same process) ───
_perception_cache: Dict[str, Dict[str, Any]] = {}  # session_id → {last_perception, last_perception_sig, prev_frame_bytes}

def _get_perception_state(sid: str) -> Dict[str, Any]:
    if sid not in _perception_cache:
        _perception_cache[sid] = {"last_perception": None, "last_perception_sig": "", "prev_frame_bytes": None}
    return _perception_cache[sid]

def _clear_perception_state(sid: str):
    _perception_cache.pop(sid, None)

LOOKUP_MANUAL_DECL = {
    "name": "lookup_manual",
    "description": "Search internal repair manuals database for a specific device. Returns structured repair guidance, troubleshooting steps, warnings, and tool requirements. Use when the user identifies a device or a label/model becomes visible.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "brand": {"type": "STRING", "description": "Device brand name (e.g., Stihl, Dyson)"},
            "model": {"type": "STRING", "description": "Device model (e.g., FS 56 RC, V15 Detect)"},
            "device_type": {"type": "STRING", "description": "Device category (e.g., string trimmer, vacuum)"},
            "issue": {"type": "STRING", "description": "The problem described by the user"},
            "query": {"type": "STRING", "description": "Free-text search query"}
        },
        "required": []
    }
}

HIGHLIGHT_DECL = {
    "name": "highlight",
    "description": (
        "Control the repair HUD. Use this when the user asks you to mark something or when a manual step, hidden fastener, "
        "or safety hazard would benefit from a visual overlay. For direct mark requests, call this before speaking. "
        "Use feature_id='manual:current_step' to mark the active manual target and feature_id='runtime:auto' with target_hint "
        "to mark an ad-hoc part from the current frame."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "enum": ["create", "update", "clear"]},
            "feature_id": {"type": "STRING", "description": "Stable HUD feature id such as manual:rear_visible_1, manual:current_step, or runtime:auto"},
            "marker_id": {"type": "STRING", "description": "Existing marker id for update or clear"},
            "action_type": {"type": "STRING", "enum": ["inspect", "unscrew_ccw", "pull", "pry", "lift", "slide", "avoid", "hold_here", "danger"]},
            "reason": {"type": "STRING", "enum": ["user_request", "manual_step", "safety", "assistant_guidance"]},
            "label": {"type": "STRING"},
            "priority": {"type": "STRING", "enum": ["primary", "secondary"]},
            "expires_ms": {"type": "NUMBER"},
            "target_hint": {"type": "STRING", "description": "Extra natural-language target description when feature_id is runtime:auto or the scene is ambiguous"},
            "allow_approximate": {"type": "BOOLEAN"},
        },
        "required": ["operation"],
    },
}


async def send_hud_state(client_ws: WebSocket, session_id: str, current_step: int = 0):
    await client_ws.send_json(build_hud_snapshot(session_id, current_step_index=current_step))


async def emit_step_highlight_if_needed(gemini_ws, client_ws: WebSocket, session_id: str, current_step: int):
    step_target = should_highlight_current_step(session_id, current_step)
    if not step_target:
        return
    prompt = (
        f"[STEP_TARGET] Manual step {step_target.get('step')}: {step_target.get('title', '')}.\n"
        f"Instruction: {step_target.get('instruction', '')}\n"
        f"Primary feature id: {step_target.get('primary_feature_id')}\n"
        f"Suggested action: {step_target.get('action_type', 'inspect')}\n"
        "If a visual marker would help right now, call highlight before you speak."
    )
    await gemini_ws.send(json.dumps({
        "clientContent": {
            "turns": [{"role": "user", "parts": [{"text": prompt}]}],
            "turnComplete": True,
        }
    }))
    await send_hud_state(client_ws, session_id, current_step=current_step)


def _hud_planner_summary(result: Dict[str, Any]) -> str:
    highlight = result.get("highlight_result") or {}
    if highlight.get("status"):
        attempts = highlight.get("attempts") or []
        suffix = f" ({len(attempts)} attempt{'s' if len(attempts) != 1 else ''})" if attempts else ""
        return f"{highlight.get('status')}{suffix}"
    if result.get("decision") == "ask_user":
        return result.get("assistant_hint") or result.get("reason") or "Needs a clearer target"
    return result.get("reason") or result.get("decision") or "No HUD marker needed"


async def run_hud_planner_and_emit(
    client_ws: WebSocket,
    session_id: str,
    event: str,
    user_text: str = "",
    current_step: Optional[int] = None,
    observation: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    if current_step is None:
        async with AsyncSessionLocal() as db:
            sess = await SessionStore.get_session(db, session_id)
            current_step = sess.current_step or 0 if sess else 0
    if force:
        await client_ws.send_json({
            "type": "tool.status",
            "tool": "hud_planner",
            "status": "running",
            "args": {"event": event, "user_text": user_text},
        })
    async with AsyncSessionLocal() as db:
        result = await run_hud_planner(
            db,
            session_id=session_id,
            event=event,
            user_text=user_text,
            current_step=current_step or 0,
            observation=observation,
            force=force,
        )
    highlight = result.get("highlight_result") or {}
    should_report = force or result.get("decision") in {"mark", "clear", "ask_user"} or highlight.get("status")
    if should_report:
        await client_ws.send_json({
            "type": "tool.status",
            "tool": "hud_planner",
            "status": "done",
            "result_summary": _hud_planner_summary(result),
            "manual_id": None,
            "warnings": [],
            "steps": [],
            "follow_up_prompt": highlight.get("follow_up_prompt") or result.get("assistant_hint"),
        })
    if highlight.get("status"):
        await send_hud_state(client_ws, session_id, current_step=current_step or 0)
    return result


def is_explicit_mark_request(text: str) -> bool:
    normalized = (text or "").lower().strip()
    if not normalized:
        return False
    patterns = [
        r"\bmark (this|that|it|this one|that one|me)\b",
        r"\bhighlight (this|that|it|this one|that one)\b",
        r"\bpoint (to|at) (this|that|it|this one|that one)\b",
        r"\bcircle (this|that|it|this one|that one)\b",
        r"\boutline (this|that|it|this one|that one)\b",
        r"\bshow me which\b",
        r"\bcan you mark\b",
        r"\bcan you highlight\b",
        r"\bwhich screw\b",
        r"\bwhich one\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting — init DB...")
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_manuals(db)
    logger.info("DB ready, manuals seeded")
    yield
    logger.info("Server shutting down")

app = FastAPI(title="Repair Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ─── REST API ───────────────────────────
api = APIRouter(prefix="/api")

@api.get("/health")
async def health():
    return {"status": "healthy", "model": MODEL_ID, "version": "2.0-step2"}

@api.get("/personas")
async def list_personas():
    return get_personas()

@api.get("/voices")
async def list_voices():
    return get_voices()

@api.post("/sessions")
async def create_session(payload: dict, db: AsyncSession = Depends(get_db)):
    s = await SessionStore.create_session(db, payload.get("persona_id", "calm-expert"), payload.get("voice_id", "Puck"))
    return {"id": s.id, "status": s.status}

@api.get("/sessions/{sid}/state")
async def get_state(sid: str, db: AsyncSession = Depends(get_db)):
    st = await SessionStore.get_session_state(db, sid)
    if not st:
        return {"error": "Session not found"}
    st.update({
        "hud_markers": build_hud_snapshot(sid, current_step_index=st.get("current_step", 0)).get("markers", []),
        "hud_feature_catalog": build_hud_snapshot(sid, current_step_index=st.get("current_step", 0)).get("feature_catalog", []),
        "current_step_target": build_hud_snapshot(sid, current_step_index=st.get("current_step", 0)).get("current_step_target"),
    })
    return st

@api.get("/sessions/{sid}/logs")
async def get_logs(sid: str, db: AsyncSession = Depends(get_db)):
    turns = await SessionStore.get_turns(db, sid, limit=100)
    tool_runs = await SessionStore.get_tool_runs(db, sid, limit=50)
    return {
        "turns": [{"role": t.role, "content": t.content, "source_type": t.source_type, "created_at": t.created_at.isoformat()} for t in turns],
        "tool_runs": [{"tool": tr.tool_name, "status": tr.status, "input": tr.input_data, "output": tr.output_data, "created_at": tr.created_at.isoformat()} for tr in tool_runs],
    }

@api.post("/sessions/{sid}/end")
async def end_session_api(sid: str, db: AsyncSession = Depends(get_db)):
    return {"status": "ended" if await SessionStore.end_session(db, sid) else "not_found"}

@api.get("/sessions/{sid}/full-transcript")
async def get_full_transcript(sid: str, db: AsyncSession = Depends(get_db)):
    """Get the complete session transcript + tool runs + observations for debugging."""
    state = await SessionStore.get_session_state(db, sid)
    if not state:
        return {"error": "Session not found"}
    return {
        "session_id": state["session_id"],
        "status": state["status"],
        "persona_id": state["persona_id"],
        "voice_id": state["voice_id"],
        "active_manual_id": state["active_manual_id"],
        "current_step": state["current_step"],
        "transcript": [{"role": t["role"], "text": t["content"], "source": t["source_type"], "at": t["created_at"]} for t in state["turns"]],
        "tool_runs": [{"tool": tr["tool_name"], "status": tr["status"], "input": tr["input"], "output": tr["output"], "at": tr["created_at"]} for tr in state["tool_runs"]],
        "observations": state["observations"],
        "hud_markers": build_hud_snapshot(sid, current_step_index=state.get("current_step", 0)).get("markers", []),
    }

app.include_router(api)

# ─── WEBSOCKET ───────────────────────────
@app.websocket("/api/ws/session")
async def ws_session(websocket: WebSocket):
    await websocket.accept()
    logger.info("WS connected")

    gemini_ws = None
    recv_task = None
    alive = True
    session_id = None
    frame_count = 0
    nudge = NudgeEngine()
    ai_speaking = False
    intentional_end = False

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        cfg = json.loads(raw)
        if cfg.get("type") != "config":
            await websocket.send_json({"type": "error", "message": "First message must be config"})
            return

        persona_id = cfg.get("persona_id", "calm-expert")
        voice_id = cfg.get("voice_id", "Puck")
        resume_id = cfg.get("resume_session_id")  # For reconnect-with-history

        # Create or resume session
        async with AsyncSessionLocal() as db:
            if resume_id:
                existing = await SessionStore.get_session(db, resume_id)
                if existing and existing.status == "active":
                    session_id = resume_id
                    logger.info(f"[{session_id}] Resuming session")
                else:
                    s = await SessionStore.create_session(db, persona_id, voice_id)
                    session_id = s.id
            else:
                s = await SessionStore.create_session(db, persona_id, voice_id)
                session_id = s.id

        await websocket.send_json({"type": "session.ready", "session_id": session_id})
        await websocket.send_json({"type": "assistant.state", "state": "connecting"})

        # Build prompt + conversation history for context
        from prompts import build_agent_prompt
        system_prompt = build_agent_prompt(persona_id, voice_id)

        # Load previous turns for reconnect context
        history_context = ""
        active_manual_context = ""
        if resume_id:
            async with AsyncSessionLocal() as db:
                turns = await SessionStore.get_turns(db, session_id, limit=20)
                if turns:
                    history_context = "\n\nPREVIOUS CONVERSATION (for context — session was briefly interrupted):\n"
                    for t in turns:
                        role_label = "User" if t.role == "user" else "Assistant"
                        history_context += f"{role_label}: {t.content}\n"
                    history_context += "\nContinue the conversation naturally from where you left off. Do NOT repeat your last response."

                # Load active manual context
                sess = await SessionStore.get_session(db, session_id)
                if sess and sess.active_manual_id:
                    manual = await get_manual_by_id(db, sess.active_manual_id)
                    if manual:
                        sync_manual_bundle(session_id, manual)
                        active_manual_context = f"\n\nACTIVE MANUAL (already loaded — do NOT call lookup_manual again): {manual.get('brand','')} {manual.get('model','')} — {manual.get('title','')}\nCurrent step: {sess.current_step}\n"

                # Load previous tool runs so Gemini doesn't repeat them
                tool_runs = await SessionStore.get_tool_runs(db, session_id, limit=10)
                if tool_runs:
                    history_context += "\n\nPREVIOUS TOOL CALLS (already executed — do NOT repeat):\n"
                    for tr in tool_runs:
                        if tr.tool_name == "lookup_manual":
                            out = tr.output_data or {}
                            if out.get("selected_manual_id"):
                                history_context += f"- lookup_manual: Found {out.get('manual_summary','')}\n"
                            else:
                                history_context += f"- lookup_manual: No manual found for {tr.input_data}. Used general knowledge instead.\n"
                    history_context += "Do NOT call these tools again unless the user mentions a DIFFERENT device.\n"

        full_prompt = system_prompt + history_context + active_manual_context

        # Connect to Gemini
        logger.info(f"[{session_id}] Connecting Gemini (resume={bool(resume_id)})")
        gemini_ws = await websockets.connect(
            GEMINI_WS_URL,
            ping_interval=30, ping_timeout=60, close_timeout=5,
            max_size=16 * 1024 * 1024,
        )

        setup = {
            "setup": {
                "model": f"models/{MODEL_ID}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice_id}}}
                },
                "system_instruction": {"parts": [{"text": full_prompt}]},
                "tools": [{"function_declarations": [LOOKUP_MANUAL_DECL, HIGHLIGHT_DECL]}],
                "input_audio_transcription": {},
                "output_audio_transcription": {}
            }
        }
        await gemini_ws.send(json.dumps(setup))
        json.loads(await asyncio.wait_for(gemini_ws.recv(), timeout=10))
        logger.info(f"[{session_id}] Gemini ready")

        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})
        await send_hud_state(websocket, session_id, current_step=0)

        # Send session state to client if resuming
        if resume_id:
            async with AsyncSessionLocal() as db:
                state = await SessionStore.get_session_state(db, session_id)
                if state:
                    state.update({
                        "hud_markers": build_hud_snapshot(session_id, current_step_index=state.get("current_step", 0)).get("markers", []),
                        "hud_feature_catalog": build_hud_snapshot(session_id, current_step_index=state.get("current_step", 0)).get("feature_catalog", []),
                        "current_step_target": build_hud_snapshot(session_id, current_step_index=state.get("current_step", 0)).get("current_step_target"),
                    })
                    await websocket.send_json({"type": "session.state", "data": state})
                    await emit_step_highlight_if_needed(gemini_ws, websocket, session_id, state.get("current_step", 0))

        # Trigger greeting — tell Gemini to introduce itself
        if not resume_id:
            greet_msg = {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": "Session just started. Greet the user briefly and ask what device they need help with. Keep it to one natural sentence."}]}],
                    "turnComplete": True
                }
            }
            await gemini_ws.send(json.dumps(greet_msg))
            logger.info(f"[{session_id}] Greeting trigger sent")

        # ─── Gemini → Client ───
        async def recv_gemini():
            nonlocal alive, ai_speaking
            user_buf = ""
            asst_buf = ""
            turn_flags = {
                "user_requested_highlight": False,
                "tool_guidance_sent": False,
                "highlight_called": False,
                "retry_sent": False,
                "hud_planner_called": False,
            }
            try:
                async for raw_msg in gemini_ws:
                    if not alive:
                        break
                    try:
                        data = json.loads(raw_msg)

                        # Tool call
                        tc = data.get("toolCall")
                        if tc:
                            tool_info = await handle_tool_call(tc, session_id, gemini_ws, websocket)
                            if tool_info.get("highlight_called"):
                                turn_flags["highlight_called"] = True
                            continue

                        sc = data.get("serverContent")
                        if not sc:
                            continue

                        # Audio
                        mt = sc.get("modelTurn")
                        if mt and mt.get("parts"):
                            ai_speaking = True
                            await websocket.send_json({"type": "assistant.state", "state": "speaking"})
                            for p in mt["parts"]:
                                idata = p.get("inlineData")
                                if idata and idata.get("data"):
                                    await websocket.send_json({"type": "audio", "data": idata["data"]})

                        # Input transcription
                        itx = sc.get("inputTranscription")
                        if itx and itx.get("text"):
                            user_buf += itx["text"]
                            nudge.user_spoke()
                            await websocket.send_json({"type": "transcription", "role": "user", "text": itx["text"]})
                            if is_explicit_mark_request(user_buf) and not turn_flags["tool_guidance_sent"] and not turn_flags["highlight_called"]:
                                turn_flags["user_requested_highlight"] = True
                                turn_flags["tool_guidance_sent"] = True
                                await gemini_ws.send(json.dumps({
                                    "clientContent": {
                                        "turns": [{
                                            "role": "user",
                                            "parts": [{
                                                "text": (
                                                    "[TOOL_REQUIREMENT] The user's current request is an explicit request to mark or highlight something in the camera view. "
                                                    "Before you answer, you MUST call highlight. If the target is unclear, still call highlight with "
                                                    'feature_id="runtime:auto" and a target_hint from the user request, then speak based on the tool result.'
                                                )
                                            }]
                                        }],
                                        "turnComplete": True,
                                    }
                                }))

                        # Output transcription
                        otx = sc.get("outputTranscription")
                        if otx and otx.get("text"):
                            asst_buf += otx["text"]
                            await websocket.send_json({"type": "transcription", "role": "assistant", "text": otx["text"]})

                        # Grounding (Google Search)
                        gm = sc.get("groundingMetadata")
                        if gm:
                            queries = gm.get("webSearchQueries", [])
                            if queries:
                                await websocket.send_json({"type": "tool.status", "tool": "google_search", "status": "done", "queries": queries})
                                logger.info(f"[{session_id}] Google Search: {queries}")

                        if sc.get("interrupted"):
                            ai_speaking = False
                            await websocket.send_json({"type": "interrupted"})
                            await websocket.send_json({"type": "assistant.state", "state": "listening"})

                        if sc.get("turnComplete"):
                            ai_speaking = False
                            logger.info(f"[{session_id}] Turn complete")
                            await websocket.send_json({"type": "turn_complete"})
                            await websocket.send_json({"type": "assistant.state", "state": "listening"})
                            planner_marked = False
                            # Persist turns
                            async with AsyncSessionLocal() as db:
                                if user_buf.strip():
                                    await SessionStore.add_turn(db, session_id, "user", user_buf.strip())
                                    sess = await SessionStore.get_session(db, session_id)
                                    if sess and user_confirms_step(session_id, user_buf):
                                        new_step = (sess.current_step or 0) + 1
                                        await SessionStore.update_session(db, session_id, current_step=new_step)
                                        clear_pending_confirmation(session_id)
                                        await websocket.send_json({"type": "step.update", "step": new_step})
                                        await send_hud_state(websocket, session_id, current_step=new_step)
                                        await emit_step_highlight_if_needed(gemini_ws, websocket, session_id, new_step)
                                if asst_buf.strip():
                                    await SessionStore.add_turn(db, session_id, "assistant", asst_buf.strip())
                            if user_buf.strip():
                                force_plan = is_hud_worthy_user_request(user_buf) or turn_flags["user_requested_highlight"]
                                planner_result = await run_hud_planner_and_emit(
                                    websocket,
                                    session_id,
                                    event="user_turn",
                                    user_text=user_buf.strip(),
                                    force=force_plan,
                                )
                                turn_flags["hud_planner_called"] = True
                                planner_highlight = planner_result.get("highlight_result") or {}
                                planner_marked = planner_highlight.get("status") in {"placed", "approximate", "cleared"}
                            if user_buf.strip() and is_explicit_mark_request(user_buf) and not turn_flags["highlight_called"] and not turn_flags["retry_sent"]:
                                if planner_marked:
                                    await gemini_ws.send(json.dumps({
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [{
                                                    "text": (
                                                        f'[HUD_PLANNER_RESULT] The independent HUD planner handled the user request: "{user_buf.strip()}". '
                                                        "A verified marker has been placed or updated. Acknowledge it in one short sentence and continue helping."
                                                    )
                                                }]
                                            }],
                                            "turnComplete": True,
                                        }
                                    }))
                                else:
                                    turn_flags["retry_sent"] = True
                                    logger.info(f"[{session_id}] Retrying explicit mark request without tool call")
                                    await gemini_ws.send(json.dumps({
                                        "clientContent": {
                                            "turns": [{
                                                "role": "user",
                                                "parts": [{
                                                    "text": (
                                                        f'[TOOL_RETRY] The user explicitly asked you to mark something: "{user_buf.strip()}". '
                                                        "You answered without calling highlight. Retry now. Call highlight first. "
                                                        'Use feature_id="runtime:auto" with a target_hint from the user request if needed. '
                                                        "After the tool returns, answer in one short sentence."
                                                    )
                                                }]
                                            }],
                                            "turnComplete": True,
                                        }
                                    }))
                            user_buf = ""
                            asst_buf = ""
                            turn_flags = {
                                "user_requested_highlight": False,
                                "tool_guidance_sent": False,
                                "highlight_called": False,
                                "retry_sent": False,
                                "hud_planner_called": False,
                            }

                    except Exception as e:
                        logger.error(f"[{session_id}] Gemini msg err: {e}")

            except websockets.exceptions.ConnectionClosedOK:
                logger.info(f"[{session_id}] Gemini closed OK")
            except websockets.exceptions.ConnectionClosedError as e:
                logger.error(f"[{session_id}] Gemini closed: {e}")
                alive = False
                try:
                    await websocket.send_json({"type": "error", "message": "Voice session disconnected."})
                except:
                    pass
            except asyncio.CancelledError:
                pass

        recv_task = asyncio.create_task(recv_gemini())

        # ─── Client → Gemini ───
        while alive:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=900)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "message": "Session timeout (15 min idle)"})
                break

            if not alive:
                break

            try:
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "audio":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}", "data": msg["data"]}]}
                    }))

                elif t == "video":
                    frame_count += 1
                    raw_b64 = msg["data"]
                    set_latest_frame(session_id, raw_b64)
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": raw_b64}]}
                    }))
                    if frame_count % 2 == 0:
                        _track_sid = session_id
                        async def _run_hud_tracking():
                            try:
                                result = await refresh_tracked_markers(_track_sid)
                                if result.get("updated", 0) > 0:
                                    async with AsyncSessionLocal() as db:
                                        sess = await SessionStore.get_session(db, _track_sid)
                                        step = sess.current_step or 0 if sess else 0
                                    await websocket.send_json(build_hud_snapshot(_track_sid, current_step_index=step))
                            except Exception as exc:
                                logger.error(f"[{_track_sid}] HUD tracking error: {exc}")
                        asyncio.create_task(_run_hud_tracking())

                    # Frame-diff nudge: only trigger when the scene actually changes
                    curr_bytes = base64.b64decode(raw_b64)
                    pstate = _get_perception_state(session_id)

                    if pstate["prev_frame_bytes"] and not ai_speaking:
                        diff = compute_frame_diff(pstate["prev_frame_bytes"], curr_bytes)
                        if diff >= DIFF_THRESHOLD and nudge.should_nudge("vision_check", ""):
                            # Fire perception off the main loop to avoid blocking frame intake
                            _snap_b64 = raw_b64
                            _snap_sid = session_id
                            _snap_diff = diff
                            async def _run_perception():
                                ps = _get_perception_state(_snap_sid)
                                async def _ws_send(msg):
                                    try:
                                        await websocket.send_json(msg)
                                    except Exception:
                                        pass
                                try:
                                    obs = await perceive_scene(_snap_b64, prior_obs=ps["last_perception"])
                                    if obs is None:
                                        logger.info(f"[{_snap_sid}] perception failed, skipping nudge")
                                        await _ws_send({"type": "vision.perception", "status": "failed", "diff": round(_snap_diff, 1)})
                                        return
                                    set_last_perception(_snap_sid, obs)
                                    sig = observation_signature(obs)
                                    confidence = float(obs.get("confidence", 0) or 0)
                                    changes = obs.get("changed_vs_prior", []) or []
                                    safety = (obs.get("safety_concern") or "").strip()
                                    is_first_obs = ps["last_perception"] is None

                                    # Skip if low confidence or nothing new (safety always passes)
                                    # BUT: if this is the first observation, treat it as a change so AI reacts to device appearing
                                    if not safety and not is_first_obs and (confidence < 0.5 or not changes) and sig == ps["last_perception_sig"]:
                                        logger.info(f"[{_snap_sid}] perception stable (conf={confidence:.2f}), no narration")
                                        await _ws_send({
                                            "type": "vision.perception", "status": "stable",
                                            "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(_snap_diff, 1),
                                        })
                                        return
                                    if not safety and not is_first_obs and (confidence < 0.5 or not changes):
                                        if confidence >= 0.5:
                                            ps["last_perception"] = obs
                                            ps["last_perception_sig"] = sig
                                        logger.info(f"[{_snap_sid}] perception: no new changes (conf={confidence:.2f})")
                                        await _ws_send({
                                            "type": "vision.perception", "status": "no_change",
                                            "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(_snap_diff, 1),
                                        })
                                        return

                                    # First observation or real changes — update prior and send to Live
                                    if confidence >= 0.5:
                                        ps["last_perception"] = obs
                                        ps["last_perception_sig"] = sig

                                    # Build step context if a manual is active
                                    step_context = ""
                                    current_step = 0
                                    async with AsyncSessionLocal() as db:
                                        sess = await SessionStore.get_session(db, _snap_sid)
                                        if sess and sess.active_manual_id:
                                            manual = await get_manual_by_id(db, sess.active_manual_id)
                                            if manual:
                                                sync_manual_bundle(_snap_sid, manual)
                                                step_num = sess.current_step or 0
                                                current_step = step_num
                                                teardown = manual.get("teardown", {})
                                                steps_list = teardown.get("steps", []) if isinstance(teardown, dict) else []
                                                if steps_list and step_num < len(steps_list):
                                                    current = steps_list[step_num]
                                                    step_context = f" Current manual step ({step_num+1}/{len(steps_list)}): {current.get('title','')} — {current.get('instruction','')}"
                                                completion_status, checks = evaluate_step_completion(_snap_sid, step_num, obs)
                                                if completion_status == "verified":
                                                    new_step = step_num + 1
                                                    await SessionStore.update_session(db, _snap_sid, current_step=new_step)
                                                    clear_pending_confirmation(_snap_sid)
                                                    current_step = new_step
                                                    await _ws_send({"type": "step.update", "step": new_step})
                                                    await _ws_send(build_hud_snapshot(_snap_sid, current_step_index=new_step))
                                                    await gemini_ws.send(json.dumps({
                                                        "clientContent": {
                                                            "turns": [{
                                                                "role": "user",
                                                                "parts": [{
                                                                    "text": (
                                                                        f"[STEP_VERIFIED] Manual step {step_num + 1} is visually verified complete. "
                                                                        "Briefly guide the user to the next step. If a precise visual marker would help, call highlight before speaking."
                                                                    )
                                                                }]
                                                            }],
                                                            "turnComplete": True,
                                                        }
                                                    }))
                                                elif completion_status == "ambiguous":
                                                    if maybe_track_pending_confirmation(_snap_sid, step_num, checks):
                                                        follow_up = get_pending_confirmation_prompt(_snap_sid) or "Show me that area clearly so I can confirm the step."
                                                        await _ws_send(build_hud_snapshot(_snap_sid, current_step_index=step_num))
                                                        await gemini_ws.send(json.dumps({
                                                            "clientContent": {
                                                                "turns": [{
                                                                    "role": "user",
                                                                    "parts": [{
                                                                        "text": (
                                                                            f"[STEP_CHECK_AMBIGUOUS] The current manual step may be complete, but visual verification is inconclusive. "
                                                                            f"Ask the user one short confirmation question. Preferred prompt: {follow_up}"
                                                                        )
                                                                    }]
                                                                }],
                                                                "turnComplete": True,
                                                            }
                                                        }))

                                    obs_summary = json.dumps({
                                        "device_state": obs.get("device_state", ""),
                                        "visible_features": obs.get("visible_features", []),
                                        "changed_vs_prior": changes,
                                        "focus_area": obs.get("focus_area", ""),
                                        "safety_concern": safety,
                                    }, ensure_ascii=False)

                                    planner_result = await run_hud_planner_and_emit(
                                        websocket,
                                        _snap_sid,
                                        event="vision_update",
                                        current_step=current_step,
                                        observation=obs,
                                        force=bool(safety),
                                    )
                                    planner_highlight = planner_result.get("highlight_result") or {}

                                    if safety:
                                        nudge_text = (
                                            f"[SAFETY_ALERT: {safety}] Look at the current frame and warn the user about this specific concern right now, in one sentence."
                                        )
                                    else:
                                        nudge_text = (
                                            f"[VISION_UPDATE] Verified facts from a grounded visual observer:\n{obs_summary}\n\n"
                                            f"Now LOOK at the current camera frame yourself.{step_context}\n"
                                            "Describe what you see in one short natural sentence — speak from your own eyes, "
                                            "but use the verified facts above as guardrails. "
                                            "If focus_area is given, direct your attention there first. "
                                            "Never contradict the verified facts. Never claim user actions (no 'removed', 'installed'); "
                                            "describe current state only. If safety_concern is non-empty, interrupt immediately about that. "
                                            "If the HUD planner placed a marker, refer to it briefly as the marked target. "
                                            "Do not read the JSON aloud — use it as reference, narrate naturally."
                                        )

                                    await gemini_ws.send(json.dumps({
                                        "clientContent": {
                                            "turns": [{"role": "user", "parts": [{"text": nudge_text}]}],
                                            "turnComplete": True
                                        }
                                    }))
                                    logger.info(f"[{_snap_sid}] VISION_UPDATE sent (conf={confidence:.2f}, changes={len(changes)})")
                                    await _ws_send({
                                            "type": "vision.perception", "status": "update_sent",
                                            "confidence": confidence, "device_state": obs.get("device_state", ""),
                                            "changes": changes, "safety_concern": safety, "diff": round(_snap_diff, 1),
                                            "hud_planner": {
                                                "decision": planner_result.get("decision"),
                                                "highlight_status": planner_highlight.get("status"),
                                            },
                                        })
                                    await _ws_send(build_hud_snapshot(_snap_sid, current_step_index=current_step))
                                except Exception as e:
                                    logger.error(f"[{_snap_sid}] perception task error: {e}")

                            asyncio.create_task(_run_perception())

                    pstate["prev_frame_bytes"] = curr_bytes
                    if frame_count % 10 == 0:
                        logger.info(f"[{session_id}] Frames sent: {frame_count}")

                elif t == "text":
                    txt = msg.get("content", "")
                    if txt:
                        if is_hud_worthy_user_request(txt):
                            asyncio.create_task(run_hud_planner_and_emit(
                                websocket,
                                session_id,
                                event="user_text",
                                user_text=txt,
                                force=True,
                            ))
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": txt}]}], "turnComplete": True}
                        }))

                elif t == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif t == "end":
                    intentional_end = True
                    logger.info(f"[{session_id}] Client ended session intentionally")
                    break

            except websockets.exceptions.ConnectionClosed:
                alive = False
                await websocket.send_json({"type": "error", "message": "Voice session disconnected."})
                break
            except Exception as e:
                logger.error(f"[{session_id}] Fwd err: {e}")
                if "closed" in str(e).lower():
                    alive = False
                    break

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] Client disconnected")
    except asyncio.TimeoutError:
        logger.info("Config timeout")
    except Exception as e:
        logger.error(f"[{session_id}] Session err: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        alive = False
        if recv_task:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
        if gemini_ws:
            try:
                await gemini_ws.close()
            except:
                pass
        # Save snapshot on disconnect
        if session_id:
            try:
                async with AsyncSessionLocal() as db:
                    state = await SessionStore.get_session_state(db, session_id)
                    if state:
                        import uuid
                        snap = SessionSnapshot(id=str(uuid.uuid4()), session_id=session_id, state_data=state)
                        db.add(snap)
                        await db.commit()
                        logger.info(f"[{session_id}] Snapshot saved")
                    # Only mark session as ended if user intentionally clicked End
                    if intentional_end:
                        await SessionStore.end_session(db, session_id)
                        logger.info(f"[{session_id}] Session ended (intentional)")
                    else:
                        logger.info(f"[{session_id}] Session kept active for reconnect")
            except:
                pass
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"[{session_id}] Cleaned up (frames={frame_count})")
        if intentional_end and session_id:
            _clear_perception_state(session_id)
            clear_runtime(session_id)
            clear_hud_planner_state(session_id)


async def handle_tool_call(tc, session_id, gemini_ws, client_ws):
    calls = tc.get("functionCalls", [])
    responses = []
    highlight_called = False
    for call in calls:
        fn = call.get("name")
        fid = call.get("id")
        args = call.get("args", {})
        logger.info(f"[{session_id}] Tool: {fn}({json.dumps(args)[:100]})")
        await client_ws.send_json({"type": "tool.status", "tool": fn, "status": "running", "args": args})

        result = {}
        if fn == "lookup_manual":
            async with AsyncSessionLocal() as db:
                result = await lookup_manual_tool(db, session_id, brand=args.get("brand", ""), model=args.get("model", ""), device_type=args.get("device_type", ""), issue=args.get("issue", ""), query=args.get("query", ""))
                if result.get("selected_manual_id"):
                    await SessionStore.update_session(db, session_id, active_manual_id=result["selected_manual_id"], active_device_type=args.get("device_type", ""), active_device_model=args.get("model", ""), current_step=0)
                    manual = await get_manual_by_id(db, result["selected_manual_id"])
                    sync_manual_bundle(session_id, manual)
                else:
                    # No manual found — short instruction for Gemini
                    result["no_manual_instruction"] = "No manual found. Tell the user briefly: 'No manual for this one — I'll use general knowledge and web search.' Then help using your training and Google Search."
                    sync_manual_bundle(session_id, None)
        elif fn == "highlight":
            highlight_called = True
            async with AsyncSessionLocal() as db:
                sess = await SessionStore.get_session(db, session_id)
                if sess:
                    args["current_step"] = sess.current_step or 0
                result = await run_highlight_tool(db, session_id, args)
        else:
            result = {"error": f"Unknown tool: {fn}"}

        responses.append({"id": fid, "name": fn, "response": result})

        # Send different UI status based on whether manual was found
        if fn == "highlight":
            async with AsyncSessionLocal() as db:
                sess = await SessionStore.get_session(db, session_id)
                current_step = sess.current_step or 0 if sess else 0
            attempts = result.get("attempts") or []
            summary = result.get("status", "")
            if attempts and result.get("status") in {"placed", "approximate"}:
                summary = f"{result.get('status')} ({len(attempts)} attempt{'s' if len(attempts) != 1 else ''})"
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": summary,
                "manual_id": None,
                "warnings": [],
                "steps": [],
                "follow_up_prompt": result.get("follow_up_prompt"),
            })
            await send_hud_state(client_ws, session_id, current_step=current_step)
        elif result.get("selected_manual_id"):
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": result.get("manual_summary", ""),
                "manual_id": result.get("selected_manual_id"),
                "warnings": result.get("warnings", []),
                "steps": result.get("troubleshooting_steps", [])[:5],
            })
            await send_hud_state(client_ws, session_id, current_step=0)
            await emit_step_highlight_if_needed(gemini_ws, client_ws, session_id, 0)
        else:
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": "No manual found — using general knowledge",
                "manual_id": None,
                "warnings": [],
                "steps": [],
            })
            await send_hud_state(client_ws, session_id, current_step=0)

    await gemini_ws.send(json.dumps({
        "toolResponse": {"functionResponses": [{"id": r["id"], "name": r["name"], "response": r["response"]} for r in responses]}
    }))
    logger.info(f"[{session_id}] Tool response → Gemini")
    return {"highlight_called": highlight_called}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
