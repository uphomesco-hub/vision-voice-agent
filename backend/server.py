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
import time
import websockets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from PIL import Image
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from db import init_db, get_db, AsyncSessionLocal, SessionSnapshot
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id
from nudge_engine import NudgeEngine
from vision_perception import perceive_scene, observation_signature
from precision_hud import (
    HIGHLIGHT_DECL,
    HUD_ADJUST_DECL,
    HUD_VERIFY_DECL,
    INSPECT_FRAME_DECL,
    apply_local_tracks,
    build_hud_snapshot,
    clear_hud_runtime,
    run_highlight_tool,
    send_hud_snapshot,
    set_latest_frame,
    refresh_stale_masks,
    correct_hud_marker,
    update_hud_marker,
    adjust_hud_marker,
    verify_hud_marker,
    propagate_stream_masks,
    inspect_current_frame,
)
from hud_review import create_manual_review, get_review_status, queue_gemini_review, run_gemini_review

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-native-audio-latest"
INPUT_SAMPLE_RATE = 16000
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

# ─── Frame Diff ──────────────────────
DIFF_THUMB_SIZE = (32, 32)
DIFF_THRESHOLD = 12.0
DIFF_COOLDOWN_FRAMES = 1
AUDIO_QUEUE_MAX = 24

_realtime_sessions: Dict[str, Dict[str, Any]] = {}
_visual_inspection_cache: Dict[str, Dict[str, Any]] = {}
VISUAL_INSPECTION_CACHE_TTL_SEC = 12.0

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

def _put_drop_oldest(queue: asyncio.Queue, item: Any):
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(item)


def _looks_like_visual_question(text: str) -> bool:
    t = (text or "").lower()
    visual_terms = (
        "what do you see", "can you see", "do you see", "in my hand", "my hand",
        "camera", "frame", "visible", "look at", "what device", "what is this",
        "what's this", "mark this", "highlight this", "point to", "show me",
        "how many fingers", "white case", "case",
    )
    return any(term in t for term in visual_terms)


def _compact_inspection_for_prompt(inspection: Dict[str, Any]) -> Dict[str, Any]:
    """Keep injected visual context small enough for realtime turns."""
    if not isinstance(inspection, dict):
        return {"ok": False, "error": "inspection unavailable"}
    best_target = inspection.get("best_target") if isinstance(inspection.get("best_target"), dict) else {}
    return {
        "ok": bool(inspection.get("ok", False)),
        "direct_answer": inspection.get("direct_answer", ""),
        "hand_visible": bool(inspection.get("hand_visible", False)),
        "objects_in_hand": inspection.get("objects_in_hand", []) or [],
        "visible_devices": inspection.get("visible_devices", []) or [],
        "visible_parts": inspection.get("visible_parts", []) or [],
        "best_target": {
            "label": best_target.get("label", ""),
            "description": best_target.get("description", ""),
            "geometry": best_target.get("geometry", {}),
            "confidence": best_target.get("confidence", 0),
        },
        "scene_summary": inspection.get("scene_summary", ""),
        "confidence": inspection.get("confidence", 0),
        "needs_user_adjustment": bool(inspection.get("needs_user_adjustment", False)),
    }


def _normalize_question_for_cache(text: str) -> str:
    return " ".join((text or "").lower().split())


def _cache_visual_inspection(session_id: str, question: str, focus: str, inspection: Dict[str, Any]) -> None:
    _visual_inspection_cache[session_id] = {
        "at": time.monotonic(),
        "question": _normalize_question_for_cache(question),
        "focus": (focus or "").strip().lower(),
        "inspection": inspection,
    }


def _get_cached_visual_inspection(session_id: str, question: str = "") -> Optional[Dict[str, Any]]:
    cached = _visual_inspection_cache.get(session_id)
    if not cached:
        return None
    if time.monotonic() - float(cached.get("at", 0) or 0) > VISUAL_INSPECTION_CACHE_TTL_SEC:
        _visual_inspection_cache.pop(session_id, None)
        return None
    cached_q = cached.get("question", "")
    incoming_q = _normalize_question_for_cache(question)
    if incoming_q and cached_q and incoming_q != cached_q:
        # Reuse the inspection for equivalent short visual followups, but not unrelated visual questions.
        overlap = set(incoming_q.split()) & set(cached_q.split())
        if len(overlap) < 3 and not ("hand" in incoming_q and "hand" in cached_q):
            return None
    return cached.get("inspection")

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

@api.get("/hud/dataset/{sid}")
async def get_hud_dataset(sid: str):
    safe_id = "".join(c if c.isalnum() or c in "_.-" else "_" for c in sid)
    root = Path(__file__).resolve().parent / "hud_dataset" / safe_id

    def read_jsonl(name: str, limit: int = 120):
        path = root / name
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows

    return {
        "session_id": sid,
        "exists": root.exists(),
        "root": str(root),
        "frames": read_jsonl("frames.jsonl"),
        "events": read_jsonl("events.jsonl"),
    }

@api.post("/hud/review/{sid}")
async def start_hud_review(sid: str, payload: dict):
    mode = str(payload.get("mode") or "manual").lower().strip()
    if mode == "manual":
        return create_manual_review(sid)
    if mode == "gemini":
        current = get_review_status(sid)
        if current.get("status") == "running":
            return current
        job = queue_gemini_review(sid)
        asyncio.create_task(run_gemini_review(sid))
        return job
    return {"session_id": sid, "status": "invalid_mode", "allowed_modes": ["manual", "gemini"]}

@api.get("/hud/review/{sid}")
async def hud_review_status(sid: str):
    return get_review_status(sid)

app.include_router(api)

# ─── WEBSOCKET ───────────────────────────
@app.websocket("/api/ws/audio/{session_id}")
async def ws_audio(websocket: WebSocket, session_id: str):
    await websocket.accept()
    state = _realtime_sessions.get(session_id)
    if not state:
        logger.warning(f"[{session_id}] Audio WS rejected: session not ready")
        await websocket.close(code=4404)
        return

    logger.info(f"[{session_id}] Audio WS connected")
    frame_count = 0
    try:
        while not state.get("closed", False):
            packet = await websocket.receive()
            if packet.get("bytes") is not None:
                frame = packet["bytes"]
                if frame:
                    _put_drop_oldest(state["audio_queue"], (frame, time.perf_counter()))
                    frame_count += 1
                    if frame_count % 250 == 0:
                        logger.info(f"[{session_id}] Audio frames queued: {frame_count}")
            elif packet.get("text"):
                try:
                    msg = json.loads(packet["text"])
                    if msg.get("type") == "end":
                        break
                except json.JSONDecodeError:
                    pass
            elif packet.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        logger.info(f"[{session_id}] Audio WS disconnected")
    except Exception as exc:
        logger.error(f"[{session_id}] Audio WS error: {exc}")
    finally:
        logger.info(f"[{session_id}] Audio WS closed (frames={frame_count})")

@app.websocket("/api/ws/session")
async def ws_session(websocket: WebSocket):
    await websocket.accept()
    logger.info("WS connected")

    gemini_ws = None
    recv_task = None
    audio_task = None
    video_task = None
    alive = True
    session_id = None
    frame_count = 0
    nudge = NudgeEngine()
    ai_speaking = False
    intentional_end = False
    realtime_state = None
    greeting_sent = False
    hud_refresh_task = None
    hud_stream_task = None

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
                "tools": [{"function_declarations": [LOOKUP_MANUAL_DECL, HIGHLIGHT_DECL, HUD_ADJUST_DECL, HUD_VERIFY_DECL, INSPECT_FRAME_DECL]}, {"google_search": {}}],
                "input_audio_transcription": {},
                "output_audio_transcription": {}
            }
        }
        await gemini_ws.send(json.dumps(setup))
        json.loads(await asyncio.wait_for(gemini_ws.recv(), timeout=10))
        logger.info(f"[{session_id}] Gemini ready")

        realtime_state = {
            "audio_queue": asyncio.Queue(maxsize=AUDIO_QUEUE_MAX),
            "latest_video": None,
            "video_event": asyncio.Event(),
            "closed": False,
        }
        _realtime_sessions[session_id] = realtime_state

        async def forward_audio():
            sent = 0
            first_frame_at = None
            while alive and not realtime_state.get("closed", False):
                try:
                    frame, queued_at = await realtime_state["audio_queue"].get()
                    if first_frame_at is None:
                        first_frame_at = time.perf_counter()
                        logger.info(f"[{session_id}] First mic frame reached Gemini lane")
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {
                            "mediaChunks": [{
                                "mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                "data": base64.b64encode(frame).decode("ascii"),
                            }]
                        }
                    }))
                    sent += 1
                    if sent % 250 == 0:
                        lag_ms = (time.perf_counter() - queued_at) * 1000
                        logger.info(f"[{session_id}] Audio frames sent: {sent} queue_lag_ms={lag_ms:.1f}")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error(f"[{session_id}] Audio forward error: {exc}")
                    break

        async def run_perception_for_frame(raw_b64: str, diff: float):
            ps = _get_perception_state(session_id)

            async def _ws_send(msg):
                try:
                    await websocket.send_json(msg)
                except Exception:
                    pass

            try:
                obs = await perceive_scene(raw_b64, prior_obs=ps["last_perception"])
                if obs is None:
                    logger.info(f"[{session_id}] perception failed, skipping nudge")
                    await _ws_send({"type": "vision.perception", "status": "failed", "diff": round(diff, 1)})
                    return
                sig = observation_signature(obs)
                confidence = float(obs.get("confidence", 0) or 0)
                changes = obs.get("changed_vs_prior", []) or []
                safety = (obs.get("safety_concern") or "").strip()
                device_state = str(obs.get("device_state", "")).lower()
                no_meaningful_change = not changes or device_state.startswith("no primary device")

                if not safety and (confidence < 0.5 or no_meaningful_change) and sig == ps["last_perception_sig"]:
                    logger.info(f"[{session_id}] perception stable (conf={confidence:.2f}), no narration")
                    await _ws_send({
                        "type": "vision.perception", "status": "stable",
                        "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(diff, 1),
                    })
                    return
                if not safety and (confidence < 0.5 or no_meaningful_change):
                    if confidence >= 0.5:
                        ps["last_perception"] = obs
                        ps["last_perception_sig"] = sig
                    logger.info(f"[{session_id}] perception: no new changes (conf={confidence:.2f})")
                    await _ws_send({
                        "type": "vision.perception", "status": "no_change",
                        "confidence": confidence, "device_state": obs.get("device_state", ""), "diff": round(diff, 1),
                    })
                    return

                if confidence >= 0.5:
                    ps["last_perception"] = obs
                    ps["last_perception_sig"] = sig

                step_context = ""
                async with AsyncSessionLocal() as db:
                    sess = await SessionStore.get_session(db, session_id)
                    if sess and sess.active_manual_id:
                        manual = await get_manual_by_id(db, sess.active_manual_id)
                        if manual:
                            step_num = sess.current_step or 0
                            teardown = manual.get("teardown", {})
                            steps_list = teardown.get("steps", []) if isinstance(teardown, dict) else []
                            if steps_list and step_num < len(steps_list):
                                current = steps_list[step_num]
                                step_context = f" Current manual step ({step_num+1}/{len(steps_list)}): {current.get('title','')} - {current.get('instruction','')}"

                obs_summary = json.dumps({
                    "device_state": obs.get("device_state", ""),
                    "visible_features": obs.get("visible_features", []),
                    "changed_vs_prior": changes,
                    "focus_area": obs.get("focus_area", ""),
                    "safety_concern": safety,
                }, ensure_ascii=False)

                if safety:
                    nudge_text = f"[SAFETY_ALERT: {safety}] Look at the current frame and warn the user about this specific concern right now, in one sentence."
                else:
                    if not step_context:
                        logger.info(f"[{session_id}] perception context updated, no voice nudge outside repair step")
                        await _ws_send({
                            "type": "vision.perception", "status": "context_only",
                            "confidence": confidence, "device_state": obs.get("device_state", ""),
                            "changes": changes, "diff": round(diff, 1),
                        })
                        return
                    nudge_text = (
                        f"[VISION_UPDATE] Verified visual context for the current repair step:\n{obs_summary}\n\n"
                        f"Current task context:{step_context}\n"
                        "If this changes the repair guidance, respond with one short useful sentence. Otherwise no spoken response is needed."
                    )

                await gemini_ws.send(json.dumps({
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": nudge_text}]}],
                        "turnComplete": True,
                    }
                }))
                logger.info(f"[{session_id}] VISION_UPDATE sent (conf={confidence:.2f}, changes={len(changes)})")
                await _ws_send({
                    "type": "vision.perception", "status": "update_sent",
                    "confidence": confidence, "device_state": obs.get("device_state", ""),
                    "changes": changes, "safety_concern": safety, "diff": round(diff, 1),
                })
            except Exception as exc:
                logger.error(f"[{session_id}] perception task error: {exc}")

        async def forward_latest_video():
            nonlocal frame_count
            while alive and not realtime_state.get("closed", False):
                try:
                    await realtime_state["video_event"].wait()
                    realtime_state["video_event"].clear()
                    item = realtime_state.get("latest_video")
                    realtime_state["latest_video"] = None
                    if not item:
                        continue
                    raw_b64 = item["data"]
                    frame_count += 1

                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": raw_b64}]}
                    }))

                    curr_bytes = base64.b64decode(raw_b64)
                    pstate = _get_perception_state(session_id)
                    if pstate["prev_frame_bytes"] and not ai_speaking:
                        diff = await asyncio.to_thread(compute_frame_diff, pstate["prev_frame_bytes"], curr_bytes)
                        if diff >= DIFF_THRESHOLD and nudge.should_nudge("vision_check", ""):
                            asyncio.create_task(run_perception_for_frame(raw_b64, diff))
                    pstate["prev_frame_bytes"] = curr_bytes

                    if frame_count % 10 == 0:
                        logger.info(f"[{session_id}] Frames sent: {frame_count}")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error(f"[{session_id}] Video forward error: {exc}")

        audio_task = asyncio.create_task(forward_audio())
        video_task = asyncio.create_task(forward_latest_video())

        await websocket.send_json({"type": "session.ready", "session_id": session_id})
        await send_hud_snapshot(websocket, session_id, "session_ready")
        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

        # Send session state to client if resuming
        if resume_id:
            async with AsyncSessionLocal() as db:
                state = await SessionStore.get_session_state(db, session_id)
                if state:
                    await websocket.send_json({"type": "session.state", "data": state})

        visual_injection_task = None

        async def inject_visual_context_for_turn(question: str, reason: str = "voice_transcript"):
            """Force fresh visual context into Gemini as soon as a visual question is detected."""
            q = (question or "").strip()
            if not q:
                return
            try:
                await websocket.send_json({
                    "type": "tool.status",
                    "tool": "inspect_current_frame",
                    "status": "running",
                    "args": {"question": q, "focus": "current user visual question", "reason": reason},
                })
            except Exception:
                pass

            inspection = await inspect_current_frame(session_id, q, "current user visual question")
            _cache_visual_inspection(session_id, q, "current user visual question", inspection)
            compact = _compact_inspection_for_prompt(inspection)
            if compact.get("ok"):
                prompt = (
                    "[CURRENT_FRAME_INSPECTION_FOR_ACTIVE_USER_QUESTION]\n"
                    f"User's active visual question/transcript: {q}\n"
                    f"Inspection JSON from the latest camera frame: {json.dumps(compact, ensure_ascii=False)}\n\n"
                    "Use this inspection as the source of truth for the active user question. "
                    "Do not call inspect_current_frame again for this same question; this is the current result. "
                    "If your answer has already started and conflicts with this, correct yourself immediately in one short sentence. "
                    "Do not deny seeing a held object when objects_in_hand or best_target describe one."
                )
            else:
                prompt = (
                    "[CURRENT_FRAME_INSPECTION_FAILED]\n"
                    f"User's active visual question/transcript: {q}\n"
                    f"Inspection failure: {json.dumps(compact, ensure_ascii=False)}\n\n"
                    "Do not claim that no device/object is visible based on this failure. "
                    "Say briefly that the current-frame visual check failed and ask the user to try again or hold the object steady."
                )
            await gemini_ws.send(json.dumps({
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": prompt}]}],
                    "turnComplete": True,
                }
            }))
            try:
                await websocket.send_json({
                    "type": "tool.status",
                    "tool": "inspect_current_frame",
                    "status": "done",
                    "summary": compact.get("direct_answer", ""),
                    "output": compact,
                })
            except Exception:
                pass
            logger.info(f"[{session_id}] Forced visual context injected ({reason}): {compact.get('direct_answer', '')}")

        # ─── Gemini → Client ───
        async def recv_gemini():
            nonlocal alive, ai_speaking, visual_injection_task
            user_buf = ""
            asst_buf = ""
            visual_injected_this_turn = False
            try:
                async for raw_msg in gemini_ws:
                    if not alive:
                        break
                    try:
                        data = json.loads(raw_msg)

                        # Tool call
                        tc = data.get("toolCall")
                        if tc:
                            await handle_tool_call(tc, session_id, gemini_ws, websocket)
                            continue

                        sc = data.get("serverContent")
                        if not sc:
                            continue

                        # Audio
                        mt = sc.get("modelTurn")
                        if mt and mt.get("parts"):
                            if not ai_speaking:
                                ai_speaking = True
                                await websocket.send_json({"type": "assistant.state", "state": "speaking"})
                            for p in mt["parts"]:
                                idata = p.get("inlineData")
                                if idata and idata.get("data"):
                                    await websocket.send_bytes(base64.b64decode(idata["data"]))

                        # Input transcription
                        itx = sc.get("inputTranscription")
                        if itx and itx.get("text"):
                            user_buf += itx["text"]
                            nudge.user_spoke()
                            await websocket.send_json({"type": "transcription", "role": "user", "text": itx["text"]})
                            if (
                                _looks_like_visual_question(user_buf)
                                and not visual_injected_this_turn
                                and (visual_injection_task is None or visual_injection_task.done())
                            ):
                                visual_injected_this_turn = True
                                visual_injection_task = asyncio.create_task(
                                    inject_visual_context_for_turn(user_buf, reason="voice_transcript")
                                )

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
                                    if _looks_like_visual_question(user_buf):
                                        inspection = _get_cached_visual_inspection(session_id, user_buf.strip())
                                        if inspection is None:
                                            inspection = await inspect_current_frame(session_id, user_buf.strip(), "current user visual question")
                                            _cache_visual_inspection(session_id, user_buf.strip(), "current user visual question", inspection)
                                        await SessionStore.add_turn(
                                            db,
                                            session_id,
                                            "user",
                                            "[CURRENT_FRAME_INSPECTION] " + json.dumps(inspection, ensure_ascii=False),
                                            source_type="vision",
                                        )
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
                            visual_injected_this_turn = False

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
                    raw_audio = msg.get("data")
                    if raw_audio and realtime_state:
                        _put_drop_oldest(realtime_state["audio_queue"], (base64.b64decode(raw_audio), time.perf_counter()))

                elif t == "video":
                    if realtime_state:
                        realtime_state["latest_video"] = {"data": msg["data"], "received_at": time.perf_counter()}
                        set_latest_frame(session_id, msg["data"])
                        realtime_state["video_event"].set()
                        if hud_stream_task is None or hud_stream_task.done():
                            async def _stream_hud_masks():
                                snapshot = await propagate_stream_masks(session_id)
                                if snapshot:
                                    await websocket.send_json({"type": "hud.state", "reason": "sam2_stream", "data": snapshot})
                            hud_stream_task = asyncio.create_task(_stream_hud_masks())

                elif t == "text":
                    txt = msg.get("content", "")
                    if txt:
                        if _looks_like_visual_question(txt):
                            await websocket.send_json({
                                "type": "tool.status",
                                "tool": "inspect_current_frame",
                                "status": "running",
                                "args": {"question": txt, "focus": "current typed visual question", "reason": "typed_text"},
                            })
                            inspection = await inspect_current_frame(session_id, txt, "current typed visual question")
                            _cache_visual_inspection(session_id, txt, "current typed visual question", inspection)
                            compact = _compact_inspection_for_prompt(inspection)
                            await websocket.send_json({
                                "type": "tool.status",
                                "tool": "inspect_current_frame",
                                "status": "done",
                                "summary": compact.get("direct_answer", ""),
                                "output": compact,
                            })
                            txt = (
                                f"{txt}\n\n"
                                "[CURRENT_FRAME_INSPECTION]\n"
                                f"{json.dumps(compact, ensure_ascii=False)}\n\n"
                                "Answer from CURRENT_FRAME_INSPECTION as the source of truth. Do not call inspect_current_frame again for this same question. "
                                "If it shows an object in the user's hand, describe that object even if the exact category is uncertain."
                            )
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": txt}]}], "turnComplete": True}
                        }))

                elif t == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif t == "hud.local_track":
                    apply_local_tracks(session_id, msg.get("tracks", []))
                    if hud_refresh_task is None or hud_refresh_task.done():
                        async def _refresh_hud_masks():
                            snapshot = await refresh_stale_masks(session_id)
                            if snapshot:
                                await websocket.send_json({"type": "hud.state", "reason": "mask_refresh", "data": snapshot})
                        hud_refresh_task = asyncio.create_task(_refresh_hud_masks())

                elif t == "hud.correct":
                    snapshot = await correct_hud_marker(
                        session_id,
                        msg.get("marker_id"),
                        msg.get("point") or {},
                        int(msg.get("label", 1) or 1),
                    )
                    await websocket.send_json({"type": "hud.state", "reason": "user_correction", "data": snapshot})

                elif t == "hud.update":
                    snapshot = await update_hud_marker(
                        session_id,
                        msg.get("marker_id"),
                        msg.get("geometry") or {},
                        bool(msg.get("locked", True)),
                        bool(msg.get("refine", False)),
                    )
                    await websocket.send_json({"type": "hud.state", "reason": "manual_update", "data": snapshot})

                elif t == "client_ready":
                    if not resume_id and not greeting_sent:
                        greeting_sent = True
                        greet_msg = {
                            "clientContent": {
                                "turns": [{
                                    "role": "user",
                                    "parts": [{"text": "Session just started. Greet the user briefly and ask what device they need help with. Keep it to one natural sentence."}],
                                }],
                                "turnComplete": True,
                            }
                        }
                        await gemini_ws.send(json.dumps(greet_msg))
                        logger.info(f"[{session_id}] Greeting trigger sent after client_ready")

                elif t == "barge_in":
                    logger.info(f"[{session_id}] Client barge-in detected")

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
        if realtime_state is not None:
            realtime_state["closed"] = True
            try:
                realtime_state["video_event"].set()
            except Exception:
                pass
        for task in (audio_task, video_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for task in (hud_refresh_task, hud_stream_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
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
        if session_id:
            _realtime_sessions.pop(session_id, None)
            _visual_inspection_cache.pop(session_id, None)
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
            clear_hud_runtime(session_id)


async def handle_tool_call(tc, session_id, gemini_ws, client_ws):
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
            async with AsyncSessionLocal() as db:
                result = await lookup_manual_tool(db, session_id, brand=args.get("brand", ""), model=args.get("model", ""), device_type=args.get("device_type", ""), issue=args.get("issue", ""), query=args.get("query", ""))
                if result.get("selected_manual_id"):
                    await SessionStore.update_session(db, session_id, active_manual_id=result["selected_manual_id"], active_device_type=args.get("device_type", ""), active_device_model=args.get("model", ""))
                else:
                    # No manual found — short instruction for Gemini
                    result["no_manual_instruction"] = "No manual found. Tell the user briefly: 'No manual for this one — I'll use general knowledge and web search.' Then help using your training and Google Search."
        elif fn == "highlight":
            hud_result = await run_highlight_tool(session_id, args)
            marker = hud_result.get("marker", {})
            result = {
                "ok": True,
                "marker_id": marker.get("id"),
                "label": marker.get("label"),
                "tracking_status": marker.get("tracking_status"),
                "confidence": marker.get("confidence"),
                "instruction": "The HUD marker is now visible and will keep tracking locally. Speak naturally and refer to the marker briefly.",
            }
            await client_ws.send_json({"type": "hud.state", "reason": "tool_highlight", "data": hud_result.get("snapshot", build_hud_snapshot(session_id))})
        elif fn == "inspect_current_frame":
            cached = _get_cached_visual_inspection(session_id, args.get("question", ""))
            if cached is not None:
                inspection = cached
                logger.info(f"[{session_id}] Tool inspect_current_frame reused cached result")
            else:
                inspection = await inspect_current_frame(session_id, args.get("question", ""), args.get("focus", ""))
                _cache_visual_inspection(session_id, args.get("question", ""), args.get("focus", ""), inspection)
            result = {
                "ok": inspection.get("ok", True) if isinstance(inspection, dict) else False,
                "direct_answer": inspection.get("direct_answer"),
                "hand_visible": inspection.get("hand_visible"),
                "objects_in_hand": inspection.get("objects_in_hand", []),
                "visible_devices": inspection.get("visible_devices", []),
                "visible_parts": inspection.get("visible_parts", []),
                "best_target": inspection.get("best_target"),
                "scene_summary": inspection.get("scene_summary"),
                "confidence": inspection.get("confidence"),
                "needs_user_adjustment": inspection.get("needs_user_adjustment"),
                "instruction": (
                    "Answer the user using this current-frame inspection as ground truth."
                    if inspection.get("ok")
                    else "The current-frame inspection failed. Do not claim no object is visible; say the visual check failed and ask the user to try again."
                ),
            }
        elif fn == "adjust_marker":
            hud_result = await adjust_hud_marker(session_id, args)
            snapshot = hud_result.get("snapshot", build_hud_snapshot(session_id))
            result = {
                "ok": hud_result.get("ok", False),
                "operation": hud_result.get("operation"),
                "marker_id": hud_result.get("marker_id") or (hud_result.get("marker") or {}).get("id"),
                "error": hud_result.get("error"),
                "instruction": "HUD marker adjustment has been applied. Speak briefly about what changed.",
            }
            await client_ws.send_json({"type": "hud.state", "reason": f"ai_{args.get('operation', 'adjust')}", "data": snapshot})
        elif fn == "verify_marker":
            hud_result = await verify_hud_marker(session_id, args)
            snapshot = hud_result.get("snapshot", build_hud_snapshot(session_id))
            result = {
                "ok": hud_result.get("ok", False),
                "marker_id": hud_result.get("marker_id"),
                "verification": hud_result.get("verification"),
                "applied": hud_result.get("applied", False),
                "error": hud_result.get("error"),
                "instruction": "Use the verification result to say whether the marker is right or that you adjusted it.",
            }
            await client_ws.send_json({"type": "hud.state", "reason": "ai_verify", "data": snapshot})
        else:
            result = {"error": f"Unknown tool: {fn}"}

        responses.append({"id": fid, "name": fn, "response": result})

        # Send different UI status based on whether manual was found
        if fn in ("highlight", "adjust_marker", "verify_marker", "inspect_current_frame"):
            if fn == "highlight":
                summary = f"Marked: {result.get('label') or args.get('target_hint', 'target')}"
            elif fn == "adjust_marker":
                summary = f"HUD {result.get('operation') or 'adjusted'}"
            elif fn == "inspect_current_frame":
                summary = result.get("direct_answer") or "Current frame inspected"
            else:
                verification = result.get("verification") or {}
                summary = "Marker verified" if verification.get("ok") else f"Marker needs adjustment: {verification.get('issue', '')}".strip()
            await client_ws.send_json({
                "type": "tool.status", "tool": fn, "status": "done",
                "result_summary": summary,
                "marker_id": result.get("marker_id"),
                "confidence": result.get("confidence"),
            })
        elif result.get("selected_manual_id"):
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
                "result_summary": "No manual found — using general knowledge",
                "manual_id": None,
                "warnings": [],
                "steps": [],
            })

    await gemini_ws.send(json.dumps({
        "toolResponse": {"functionResponses": [{"id": r["id"], "name": r["name"], "response": r["response"]} for r in responses]}
    }))
    logger.info(f"[{session_id}] Tool response → Gemini")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
