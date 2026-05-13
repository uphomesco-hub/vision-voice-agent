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
import re
import websockets
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from PIL import Image

from db import init_db, get_db, AsyncSessionLocal, SessionSnapshot
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id
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
_perception_cache: Dict[str, Dict[str, Any]] = {}

def _get_perception_state(sid: str) -> Dict[str, Any]:
    if sid not in _perception_cache:
        _perception_cache[sid] = {
            "last_perception": None,
            "last_perception_sig": "",
            "prev_frame_bytes": None,
            "latest_frame_b64": None,
        }
    return _perception_cache[sid]

def _clear_perception_state(sid: str):
    _perception_cache.pop(sid, None)


MANUAL_CLOSE_RE = re.compile(
    r"\b(close|hide|dismiss|remove|clear|stop|cancel)\s+(the\s+)?manual\b|"
    r"\bmanual\s+(close|hide|dismiss|remove|clear|stop|cancel)\b|"
    r"\b(no|dont|don't)\s+(use\s+)?(the\s+)?manual\b",
    re.IGNORECASE,
)

MANUAL_TERM_STOPWORDS = {
    "a", "an", "and", "for", "from", "guide", "help", "how", "manual",
    "model", "need", "of", "on", "repair", "the", "this", "to", "with",
}


def _manual_close_requested(text: str) -> bool:
    return bool(text and MANUAL_CLOSE_RE.search(text))


def _manual_lookup_terms(args: Dict[str, Any]) -> List[str]:
    terms: List[str] = []
    for key in ("brand", "model", "device_type", "query"):
        value = str(args.get(key, "") or "").strip().lower()
        if not value:
            continue
        terms.append(value)
        terms.extend(
            token
            for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", value)
            if token not in MANUAL_TERM_STOPWORDS
        )
    seen = set()
    return [term for term in terms if not (term in seen or seen.add(term))]


def _perception_confirms_terms(perception: Dict[str, Any], terms: List[str]) -> bool:
    confidence = float(perception.get("confidence", 0) or 0)
    if confidence < 0.55:
        return False

    visual_fields = [
        perception.get("device_state", ""),
        perception.get("focus_area", ""),
        " ".join(str(x) for x in perception.get("visible_features", []) or []),
        " ".join(str(x) for x in perception.get("changed_vs_prior", []) or []),
    ]
    visual_text = " ".join(visual_fields).lower()
    return any(term in visual_text for term in terms)


def _manual_lookup_grounded(
    args: Dict[str, Any],
    session_id: str,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
) -> Tuple[bool, str]:
    terms = _manual_lookup_terms(args)
    if not terms:
        return False, "manual lookup had no named device terms"

    if camera_status != "on" or frame_count <= 0:
        return False, "camera is unavailable"

    perception = (_get_perception_state(session_id).get("last_perception") or {})
    if _perception_confirms_terms(perception, terms):
        return True, "visual perception confirmed the device"

    return False, "requested manual terms were not visible"


async def _confirm_manual_lookup_grounding(
    args: Dict[str, Any],
    session_id: str,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
) -> Tuple[bool, str]:
    grounded, reason = _manual_lookup_grounded(
        args,
        session_id,
        camera_status=camera_status,
        frame_count=frame_count,
    )
    if grounded:
        return grounded, reason

    terms = _manual_lookup_terms(args)
    pstate = _get_perception_state(session_id)
    latest_frame_b64 = pstate.get("latest_frame_b64")
    if not terms:
        return False, "manual lookup had no named device terms"
    if camera_status != "on" or frame_count <= 0 or not latest_frame_b64:
        return False, "camera is unavailable"

    try:
        obs = await perceive_scene(latest_frame_b64, prior_obs=pstate.get("last_perception"))
    except Exception as e:
        logger.warning(f"[{session_id}] silent manual vision confirmation failed: {e}")
        return False, "silent vision confirmation failed"

    if not obs:
        return False, "silent vision confirmation returned no observation"

    confidence = float(obs.get("confidence", 0) or 0)
    if confidence >= 0.5:
        pstate["last_perception"] = obs
        pstate["last_perception_sig"] = observation_signature(obs)

    if _perception_confirms_terms(obs, terms):
        return True, "silent camera confirmation matched the requested device"

    return False, "silent camera confirmation did not see the requested device"


async def close_active_manual(session_id: str, client_ws: WebSocket, gemini_ws=None, reason: str = ""):
    async with AsyncSessionLocal() as db:
        await SessionStore.update_session(
            db,
            session_id,
            active_manual_id=None,
            active_device_type=None,
            active_device_model=None,
            current_step=0,
        )

    await client_ws.send_json({
        "type": "manual.closed",
        "message": "Manual closed.",
        "reason": reason,
    })

    if gemini_ws:
        await gemini_ws.send(json.dumps({
            "clientContent": {
                "turns": [{
                    "role": "user",
                    "parts": [{
                        "text": (
                            "[MANUAL_CLOSED] The active manual has been closed at the user's request. "
                            "Continue the conversation normally without using that manual. "
                            "Do not open another manual unless the user explicitly names a device or the camera visibly confirms one."
                        )
                    }],
                }],
                "turnComplete": True,
            }
        }))

LOOKUP_MANUAL_DECL = {
    "name": "lookup_manual",
    "description": "Search internal repair manuals database for a specific device. Returns structured repair guidance, troubleshooting steps, warnings, and tool requirements. Use only after the device is visible on the current camera feed; the backend will silently verify the latest frame before opening a manual.",
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
    return st or {"error": "Session not found"}

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
    camera_status = "unavailable"
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
        language = cfg.get("language", "en-US")
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
        full_prompt += (
            "\n\nCAMERA AVAILABILITY: At session start no camera frame has been received yet. "
            "Until you receive image frames, do not claim you can see the scene. "
            "If the user asks what you see before frames arrive, say: "
            "\"I can't see anything right now — please turn on the camera or check camera access in settings.\""
        )
        if language.lower().startswith("en"):
            full_prompt += (
                "\n\nLANGUAGE: The client requested English. Speak only in English and keep all output transcription in English. "
                "If the input audio transcription is in another language, answer in English anyway."
            )

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
                "tools": [{"function_declarations": [LOOKUP_MANUAL_DECL]}],
                "input_audio_transcription": {},
                "output_audio_transcription": {}
            }
        }
        await gemini_ws.send(json.dumps(setup))
        json.loads(await asyncio.wait_for(gemini_ws.recv(), timeout=10))
        logger.info(f"[{session_id}] Gemini ready")

        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

        # Send session state to client if resuming
        if resume_id:
            async with AsyncSessionLocal() as db:
                state = await SessionStore.get_session_state(db, session_id)
                if state:
                    await websocket.send_json({"type": "session.state", "data": state})

        # Trigger greeting — tell Gemini to introduce itself
        if not resume_id:
            greet_msg = {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": "Session just started. Greet the user briefly in English and ask what device they need help with. Keep it to one natural sentence."}]}],
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
            manual_close_seen = False
            try:
                async for raw_msg in gemini_ws:
                    if not alive:
                        break
                    try:
                        data = json.loads(raw_msg)

                        # Tool call
                        tc = data.get("toolCall")
                        if tc:
                            await handle_tool_call(
                                tc,
                                session_id,
                                gemini_ws,
                                websocket,
                                camera_status=camera_status,
                                frame_count=frame_count,
                            )
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
                            if not manual_close_seen and _manual_close_requested(user_buf):
                                manual_close_seen = True
                                await close_active_manual(session_id, websocket, gemini_ws, reason="voice request")

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
                            # Persist turns
                            async with AsyncSessionLocal() as db:
                                if user_buf.strip():
                                    await SessionStore.add_turn(db, session_id, "user", user_buf.strip())
                                if asst_buf.strip():
                                    await SessionStore.add_turn(db, session_id, "assistant", asst_buf.strip())
                                    # Check if assistant mentioned completing a step — advance step counter
                                    step_keywords = ["next step", "step done", "move on to", "that's done", "let's proceed", "now we need to", "good, now"]
                                    lower_asst = asst_buf.lower()
                                    if any(kw in lower_asst for kw in step_keywords):
                                        sess = await SessionStore.get_session(db, session_id)
                                        if sess:
                                            new_step = (sess.current_step or 0) + 1
                                            await SessionStore.update_session(db, session_id, current_step=new_step)
                                            await websocket.send_json({"type": "step.update", "step": new_step})
                                            logger.info(f"[{session_id}] Step advanced to {new_step}")
                            user_buf = ""
                            asst_buf = ""
                            manual_close_seen = False

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
                    if camera_status != "on":
                        camera_status = "on"
                        await gemini_ws.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": "[CAMERA_STATUS: on] Camera frames are now available. Use only current visible frames for visual claims."}]}],
                                "turnComplete": True,
                            }
                        }))
                    raw_b64 = msg["data"]
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": raw_b64}]}
                    }))

                    # Frame-diff nudge: only trigger when the scene actually changes
                    curr_bytes = base64.b64decode(raw_b64)
                    pstate = _get_perception_state(session_id)
                    pstate["latest_frame_b64"] = raw_b64

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
                                    async with AsyncSessionLocal() as db:
                                        sess = await SessionStore.get_session(db, _snap_sid)
                                        if sess and sess.active_manual_id:
                                            manual = await get_manual_by_id(db, sess.active_manual_id)
                                            if manual:
                                                step_num = sess.current_step or 0
                                                teardown = manual.get("teardown", {})
                                                steps_list = teardown.get("steps", []) if isinstance(teardown, dict) else []
                                                if steps_list and step_num < len(steps_list):
                                                    current = steps_list[step_num]
                                                    step_context = f" Current manual step ({step_num+1}/{len(steps_list)}): {current.get('title','')} — {current.get('instruction','')}"

                                    obs_summary = json.dumps({
                                        "device_state": obs.get("device_state", ""),
                                        "visible_features": obs.get("visible_features", []),
                                        "changed_vs_prior": changes,
                                        "focus_area": obs.get("focus_area", ""),
                                        "safety_concern": safety,
                                    }, ensure_ascii=False)

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
                                    })
                                except Exception as e:
                                    logger.error(f"[{_snap_sid}] perception task error: {e}")

                            asyncio.create_task(_run_perception())

                    pstate["prev_frame_bytes"] = curr_bytes
                    if frame_count % 10 == 0:
                        logger.info(f"[{session_id}] Frames sent: {frame_count}")

                elif t == "text":
                    txt = msg.get("content", "")
                    if txt:
                        if _manual_close_requested(txt):
                            await close_active_manual(session_id, websocket, gemini_ws, reason="text request")
                        if camera_status != "on" or frame_count == 0:
                            txt = (
                                "[CAMERA_STATUS: unavailable] No camera frame is currently available. "
                                "If this message asks what you see, say you can't see anything right now and ask the user to turn on the camera or check camera access in settings. "
                                "Do not guess.\n\n"
                                f"USER MESSAGE: {txt}"
                            )
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": txt}]}], "turnComplete": True}
                        }))

                elif t == "manual.close":
                    await close_active_manual(session_id, websocket, gemini_ws, reason="client request")

                elif t == "camera_status":
                    state = msg.get("state", "unavailable")
                    reason = msg.get("reason", "")
                    if state not in {"on", "off", "unavailable"}:
                        state = "unavailable"
                    camera_status = state
                    status_text = (
                        f"[CAMERA_STATUS: {state}] "
                        "The camera feed is not visible to you. "
                        "If the user asks what you see, say you can't see anything right now and ask them to turn on the camera or check camera access in settings. "
                        "Do not guess."
                    )
                    if state == "on":
                        status_text = "[CAMERA_STATUS: on] Camera frames are available. Only describe what is visible in the current frame."
                    if reason:
                        status_text += f" Reason: {reason}"
                    await gemini_ws.send(json.dumps({
                        "clientContent": {"turns": [{"role": "user", "parts": [{"text": status_text}]}], "turnComplete": True}
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


async def handle_tool_call(
    tc,
    session_id,
    gemini_ws,
    client_ws,
    *,
    camera_status: str = "unavailable",
    frame_count: int = 0,
):
    calls = tc.get("functionCalls", [])
    responses = []
    for call in calls:
        fn = call.get("name")
        fid = call.get("id")
        args = call.get("args", {})
        logger.info(f"[{session_id}] Tool: {fn}({json.dumps(args)[:100]})")
        await client_ws.send_json({"type": "tool.status", "tool": fn, "status": "running", "args": args})

        result = {}
        if fn == "lookup_manual":
            grounded, ground_reason = await _confirm_manual_lookup_grounding(
                args,
                session_id,
                camera_status=camera_status,
                frame_count=frame_count,
            )
            if not grounded:
                logger.warning(f"[{session_id}] lookup_manual blocked: {ground_reason}; args={args}")
                result = {
                    "found_count": 0,
                    "selected_manual_id": None,
                    "manual_summary": "Manual not opened — device was not confirmed.",
                    "blocked": True,
                    "block_reason": ground_reason,
                    "no_manual_instruction": (
                        "Do not open a manual yet. Tell the user you need to see the device clearly "
                        "on camera first. Do not guess the device."
                    ),
                }
            else:
                async with AsyncSessionLocal() as db:
                    result = await lookup_manual_tool(db, session_id, brand=args.get("brand", ""), model=args.get("model", ""), device_type=args.get("device_type", ""), issue=args.get("issue", ""), query=args.get("query", ""))
                    if result.get("selected_manual_id"):
                        await SessionStore.update_session(db, session_id, active_manual_id=result["selected_manual_id"], active_device_type=args.get("device_type", ""), active_device_model=args.get("model", ""))
                    else:
                        # No manual found — short instruction for Gemini
                        result["no_manual_instruction"] = "No manual found. Tell the user briefly: 'No manual for this one — I'll use general knowledge and web search.' Then help using your training and Google Search."
        else:
            result = {"error": f"Unknown tool: {fn}"}

        responses.append({"id": fid, "name": fn, "response": result})

        # Send different UI status based on whether manual was found
        if result.get("selected_manual_id"):
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": result.get("manual_summary", ""),
                "manual_id": result.get("selected_manual_id"),
                "warnings": result.get("warnings", []),
                "steps": result.get("troubleshooting_steps", [])[:5],
            })
        else:
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": result.get("manual_summary") or "No manual found — using general knowledge",
                "manual_id": None,
                "warnings": [],
                "steps": [],
                "blocked": result.get("blocked", False),
            })

    await gemini_ws.send(json.dumps({
        "toolResponse": {"functionResponses": [{"id": r["id"], "name": r["name"], "response": r["response"]} for r in responses]}
    }))
    logger.info(f"[{session_id}] Tool response → Gemini")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
