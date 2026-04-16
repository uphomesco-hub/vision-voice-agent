from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
import asyncio
import base64
import json
import os
import websockets

from db import init_db, get_db, AsyncSessionLocal
from personas import get_personas, get_voices
from session_store import SessionStore
from manual_repo import seed_manuals, lookup_manual_tool, get_manual_by_id

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
    logger.info("Server starting — initializing DB...")
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_manuals(db)
    logger.info("DB initialized, manuals seeded")
    yield
    logger.info("Server shutting down...")

app = FastAPI(title="Repair Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ─── REST API ───────────────────────────────────────────────
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
    session = await SessionStore.create_session(db, payload.get("persona_id", "calm-expert"), payload.get("voice_id", "Puck"))
    return {"id": session.id, "status": session.status}

@api.get("/sessions/{session_id}/state")
async def get_session_state(session_id: str, db: AsyncSession = Depends(get_db)):
    state = await SessionStore.get_session_state(db, session_id)
    if not state:
        return {"error": "Session not found"}
    return state

@api.get("/sessions/{session_id}/logs")
async def get_session_logs(session_id: str, db: AsyncSession = Depends(get_db)):
    turns = await SessionStore.get_turns(db, session_id, limit=100)
    tool_runs = await SessionStore.get_tool_runs(db, session_id, limit=50)
    return {
        "turns": [{"role": t.role, "content": t.content, "source_type": t.source_type, "created_at": t.created_at.isoformat()} for t in turns],
        "tool_runs": [{"tool": tr.tool_name, "status": tr.status, "input": tr.input_data, "output": tr.output_data, "created_at": tr.created_at.isoformat()} for tr in tool_runs],
    }

@api.post("/sessions/{session_id}/end")
async def end_session_api(session_id: str, db: AsyncSession = Depends(get_db)):
    ok = await SessionStore.end_session(db, session_id)
    return {"status": "ended" if ok else "not_found"}

app.include_router(api)

# ─── WEBSOCKET REALTIME SESSION ─────────────────────────────
@app.websocket("/api/ws/session")
async def voice_session(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client WS connected")

    gemini_ws = None
    receive_task = None
    session_alive = True
    session_id = None

    try:
        # 1. Wait for config
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        config_msg = json.loads(raw)
        if config_msg.get("type") != "config":
            await websocket.send_json({"type": "error", "message": "First message must be config"})
            return

        persona_id = config_msg.get("persona_id", "calm-expert")
        voice_id = config_msg.get("voice_id", "Puck")

        # 2. Create session in DB
        async with AsyncSessionLocal() as db:
            sess = await SessionStore.create_session(db, persona_id, voice_id)
            session_id = sess.id

        await websocket.send_json({"type": "session.ready", "session_id": session_id})
        await websocket.send_json({"type": "assistant.state", "state": "connecting"})

        # 3. Build prompt
        from prompts import build_agent_prompt
        system_prompt = build_agent_prompt(persona_id, voice_id)

        # 4. Connect to Gemini raw WS
        logger.info(f"[{session_id}] Connecting Gemini: {MODEL_ID}, voice={voice_id}")
        gemini_ws = await websockets.connect(
            GEMINI_WS_URL,
            ping_interval=30, ping_timeout=60, close_timeout=5,
            max_size=16 * 1024 * 1024,
        )

        setup_msg = {
            "setup": {
                "model": f"models/{MODEL_ID}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {"prebuilt_voice_config": {"voice_name": voice_id}}
                    }
                },
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "tools": [{"function_declarations": [LOOKUP_MANUAL_DECL]}],
                "input_audio_transcription": {},
                "output_audio_transcription": {}
            }
        }
        await gemini_ws.send(json.dumps(setup_msg))
        setup_resp = json.loads(await asyncio.wait_for(gemini_ws.recv(), timeout=10))
        logger.info(f"[{session_id}] Gemini setup OK")

        await websocket.send_json({"type": "assistant.state", "state": "listening"})
        await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

        # 5. Receive from Gemini → forward to client
        async def receive_from_gemini():
            nonlocal session_alive
            user_transcript_buf = ""
            assistant_transcript_buf = ""
            try:
                async for raw_msg in gemini_ws:
                    if not session_alive:
                        break
                    try:
                        data = json.loads(raw_msg)

                        # ── Tool call from Gemini ──
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
                            for part in mt["parts"]:
                                idata = part.get("inlineData")
                                if idata and idata.get("data"):
                                    await websocket.send_json({"type": "audio", "data": idata["data"]})

                        # Input transcription
                        itx = sc.get("inputTranscription")
                        if itx and itx.get("text"):
                            user_transcript_buf += itx["text"]
                            await websocket.send_json({"type": "transcription", "role": "user", "text": itx["text"]})

                        # Output transcription
                        otx = sc.get("outputTranscription")
                        if otx and otx.get("text"):
                            assistant_transcript_buf += otx["text"]
                            await websocket.send_json({"type": "transcription", "role": "assistant", "text": otx["text"]})

                        # Interrupted
                        if sc.get("interrupted"):
                            await websocket.send_json({"type": "interrupted"})

                        # Turn complete
                        if sc.get("turnComplete"):
                            logger.info(f"[{session_id}] Turn complete")
                            await websocket.send_json({"type": "turn_complete"})
                            await websocket.send_json({"type": "assistant.state", "state": "listening"})
                            # Persist transcripts
                            if user_transcript_buf.strip():
                                async with AsyncSessionLocal() as db:
                                    await SessionStore.add_turn(db, session_id, "user", user_transcript_buf.strip())
                            if assistant_transcript_buf.strip():
                                async with AsyncSessionLocal() as db:
                                    await SessionStore.add_turn(db, session_id, "assistant", assistant_transcript_buf.strip())
                            user_transcript_buf = ""
                            assistant_transcript_buf = ""

                    except Exception as e:
                        logger.error(f"[{session_id}] Gemini msg error: {e}")

            except websockets.exceptions.ConnectionClosedOK:
                logger.info(f"[{session_id}] Gemini closed normally")
            except websockets.exceptions.ConnectionClosedError as e:
                logger.error(f"[{session_id}] Gemini closed: {e}")
                session_alive = False
                try:
                    await websocket.send_json({"type": "error", "message": "Voice session disconnected. Please start a new session."})
                except:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[{session_id}] Gemini receive error: {e}")
                session_alive = False

        receive_task = asyncio.create_task(receive_from_gemini())

        # 6. Main loop: client → Gemini
        while session_alive:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=900)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "message": "Session timeout (15 min idle)"})
                break

            if not session_alive:
                break

            try:
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "audio":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}", "data": msg["data"]}]}
                    }))

                elif t == "video":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"mediaChunks": [{"mimeType": "image/jpeg", "data": msg["data"]}]}
                    }))
                    logger.debug(f"[{session_id}] Video frame forwarded")

                elif t == "text":
                    text = msg.get("content", "")
                    if text:
                        await gemini_ws.send(json.dumps({
                            "clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}
                        }))

                elif t == "heartbeat":
                    await websocket.send_json({"type": "heartbeat"})

                elif t == "end":
                    logger.info(f"[{session_id}] Client ended session")
                    break

            except websockets.exceptions.ConnectionClosed:
                logger.error(f"[{session_id}] Gemini closed while sending")
                session_alive = False
                await websocket.send_json({"type": "error", "message": "Voice session disconnected."})
                break
            except Exception as e:
                logger.error(f"[{session_id}] Send error: {e}")
                if "closed" in str(e).lower():
                    session_alive = False
                    break

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] Client disconnected")
    except asyncio.TimeoutError:
        logger.info("Config timeout")
    except Exception as e:
        logger.error(f"[{session_id}] Session error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        session_alive = False
        if receive_task:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
        if gemini_ws:
            try:
                await gemini_ws.close()
            except:
                pass
        if session_id:
            async with AsyncSessionLocal() as db:
                await SessionStore.end_session(db, session_id)
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"[{session_id}] Session cleaned up")


async def handle_tool_call(tc: dict, session_id: str, gemini_ws, client_ws: WebSocket):
    """Handle Gemini tool calls (function calling)."""
    calls = tc.get("functionCalls", [])
    responses = []

    for call in calls:
        fn_name = call.get("name")
        fn_id = call.get("id")
        fn_args = call.get("args", {})
        logger.info(f"[{session_id}] Tool call: {fn_name}({json.dumps(fn_args)[:100]})")

        # Notify client
        await client_ws.send_json({"type": "tool.status", "tool": fn_name, "status": "running", "args": fn_args})

        result = {}
        if fn_name == "lookup_manual":
            async with AsyncSessionLocal() as db:
                result = await lookup_manual_tool(
                    db, session_id,
                    brand=fn_args.get("brand", ""),
                    model=fn_args.get("model", ""),
                    device_type=fn_args.get("device_type", ""),
                    issue=fn_args.get("issue", ""),
                    query=fn_args.get("query", ""),
                )
                # Update session with active manual
                if result.get("selected_manual_id"):
                    await SessionStore.update_session(db, session_id,
                        active_manual_id=result["selected_manual_id"],
                        active_device_type=fn_args.get("device_type", ""),
                        active_device_model=fn_args.get("model", ""),
                    )
        else:
            result = {"error": f"Unknown tool: {fn_name}"}

        responses.append({"id": fn_id, "name": fn_name, "response": result})

        # Notify client of result
        await client_ws.send_json({
            "type": "tool.status", "tool": fn_name, "status": "done",
            "result_summary": result.get("manual_summary", ""),
            "manual_id": result.get("selected_manual_id"),
            "warnings": result.get("warnings", []),
            "steps": result.get("troubleshooting_steps", [])[:3],
        })

    # Send tool response back to Gemini
    tool_resp = {
        "toolResponse": {
            "functionResponses": [
                {"id": r["id"], "name": r["name"], "response": r["response"]}
                for r in responses
            ]
        }
    }
    await gemini_ws.send(json.dumps(tool_resp))
    logger.info(f"[{session_id}] Tool response sent to Gemini")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
