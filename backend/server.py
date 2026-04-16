from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
import asyncio
import json
import os
import websockets
from datetime import datetime, timezone

from db import init_db, get_db, AsyncSessionLocal, SessionSnapshot
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id
from nudge_engine import NudgeEngine

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-native-audio-latest"
INPUT_SAMPLE_RATE = 16000
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

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
    nudge = NudgeEngine()
    frame_count = 0
    last_nudge_frame = 0
    is_speaking = False  # Track if Gemini is currently speaking

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
                        active_manual_context = f"\n\nACTIVE MANUAL: {manual.get('brand','')} {manual.get('model','')} — {manual.get('title','')}\nCurrent step: {sess.current_step}\n"

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
                    "turns": [{"role": "user", "parts": [{"text": "Session just started. Greet the user briefly and ask what device they need help with. Keep it to one natural sentence."}]}],
                    "turnComplete": True
                }
            }
            await gemini_ws.send(json.dumps(greet_msg))
            logger.info(f"[{session_id}] Greeting trigger sent")

        # ─── Gemini → Client ───
        async def recv_gemini():
            nonlocal alive
            user_buf = ""
            asst_buf = ""
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
                            is_speaking = True
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
                            await websocket.send_json({"type": "interrupted"})
                            await websocket.send_json({"type": "assistant.state", "state": "listening"})

                        if sc.get("turnComplete"):
                            is_speaking = False
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
                    # Always send frame to Gemini for context
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": msg["data"]}]}
                    }))

                    # ─── NUDGE SYSTEM: Periodically prompt Gemini to analyze what it sees ───
                    # Trigger every 10 frames (~20s), but NOT while Gemini is speaking or user just spoke
                    frames_since_nudge = frame_count - last_nudge_frame
                    should_nudge = (
                        frames_since_nudge >= 10
                        and not is_speaking
                        and nudge.should_nudge("vision_check", "")
                    )

                    if should_nudge:
                        last_nudge_frame = frame_count
                        # Load current session state for context
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
                                        step_context = f" Current manual step ({step_num+1}/{len(steps_list)}): {current.get('title','')} — {current.get('instruction','')}"

                        nudge_prompt = f"[VISION CHECK] Look at the current camera frame carefully. What do you see RIGHT NOW? Has anything changed since last time?{step_context} If the current step looks completed based on what you see, confirm it and tell the user the next step. If nothing meaningful changed, stay silent — do NOT repeat old observations. Only speak if you see something new or important."

                        await gemini_ws.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": nudge_prompt}]}],
                                "turnComplete": True
                            }
                        }))
                        logger.info(f"[{session_id}] Vision nudge sent (frame #{frame_count})")
                        await websocket.send_json({"type": "vision.status", "status": "analyzing"})

                    if frame_count % 10 == 0:
                        logger.info(f"[{session_id}] Frames sent: {frame_count}")

                elif t == "text":
                    txt = msg.get("content", "")
                    if txt:
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": txt}]}], "turnComplete": True}
                        }))

                elif t == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif t == "end":
                    logger.info(f"[{session_id}] Client ended")
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
        # Save snapshot on disconnect (not on intentional end)
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
            except:
                pass
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"[{session_id}] Cleaned up (frames={frame_count})")


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
            result = {"error": f"Unknown tool: {fn}"}

        responses.append({"id": fid, "name": fn, "response": result})
        await client_ws.send_json({
            "type": "tool.status", "tool": fn, "status": "done",
            "result_summary": result.get("manual_summary", ""),
            "manual_id": result.get("selected_manual_id"),
            "warnings": result.get("warnings", []),
            "steps": result.get("troubleshooting_steps", [])[:5],
        })

    await gemini_ws.send(json.dumps({
        "toolResponse": {"functionResponses": [{"id": r["id"], "name": r["name"], "response": r["response"]} for r in responses]}
    }))
    logger.info(f"[{session_id}] Tool response → Gemini")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
