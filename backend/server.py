from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio
import base64
import json
import os
from google import genai
from google.genai import types

from personas import get_personas, get_voices

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-native-audio-latest"
INPUT_SAMPLE_RATE = 16000

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting...")
    yield
    logger.info("Server shutting down...")

app = FastAPI(title="Repair Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

api_router = APIRouter(prefix="/api")

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "model": MODEL_ID, "mode": "direct-gemini-live"}

@api_router.get("/personas")
async def list_personas():
    return get_personas()

@api_router.get("/voices")
async def list_voices():
    return get_voices()

app.include_router(api_router)


@app.websocket("/api/ws/session")
async def voice_session(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client WebSocket connected")

    session_alive = True  # Flag to stop sending when Gemini disconnects
    receive_task = None

    try:
        # Wait for config message
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        config_msg = json.loads(raw)
        
        if config_msg.get("type") != "config":
            await websocket.send_json({"type": "error", "message": "First message must be type 'config'"})
            return

        persona_id = config_msg.get("persona_id", "calm-expert")
        voice_id = config_msg.get("voice_id", "Puck")

        await websocket.send_json({"type": "status", "message": "Connecting to Gemini Live..."})

        from prompts import build_agent_prompt
        system_instruction = build_agent_prompt(persona_id, voice_id)

        logger.info(f"Connecting to Gemini: model={MODEL_ID}, voice={voice_id}")
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_id)
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=system_instruction)]
            ),
        )

        async with client.aio.live.connect(model=MODEL_ID, config=live_config) as session:
            logger.info(f"Gemini Live connected: persona={persona_id}, voice={voice_id}")
            await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

            # Background: receive from Gemini, forward to client
            async def receive_from_gemini():
                nonlocal session_alive
                try:
                    async for response in session.receive():
                        try:
                            sc = response.server_content
                            if sc is None:
                                continue

                            if sc.model_turn and sc.model_turn.parts:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        audio_b64 = base64.b64encode(part.inline_data.data).decode()
                                        await websocket.send_json({"type": "audio", "data": audio_b64})

                            if sc.input_transcription and sc.input_transcription.text:
                                await websocket.send_json({"type": "transcription", "role": "user", "text": sc.input_transcription.text})

                            if sc.output_transcription and sc.output_transcription.text:
                                await websocket.send_json({"type": "transcription", "role": "assistant", "text": sc.output_transcription.text})

                            if sc.interrupted:
                                await websocket.send_json({"type": "interrupted"})

                            if sc.turn_complete:
                                await websocket.send_json({"type": "turn_complete"})

                        except Exception as e:
                            logger.error(f"Error processing Gemini response: {e}")
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    if "1000" in str(e):
                        logger.info("Gemini session closed normally")
                    else:
                        logger.error(f"Gemini receive loop ended: {e}")
                    session_alive = False
                    try:
                        await websocket.send_json({"type": "error", "message": f"Gemini session ended: {e}"})
                    except:
                        pass

            receive_task = asyncio.create_task(receive_from_gemini())

            # Main loop: receive from client, forward to Gemini
            while session_alive:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "message": "Session timeout (5 min idle)"})
                    break

                if not session_alive:
                    break

                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "audio":
                        if not session_alive:
                            break
                        audio_bytes = base64.b64decode(msg["data"])
                        await session.send_realtime_input(
                            audio=types.Blob(data=audio_bytes, mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}")
                        )
                        # Yield to event loop so pings can be processed
                        await asyncio.sleep(0)

                    elif msg_type == "video":
                        if not session_alive:
                            break
                        frame_bytes = base64.b64decode(msg["data"])
                        await session.send_realtime_input(
                            video=types.Blob(data=frame_bytes, mime_type="image/jpeg")
                        )

                    elif msg_type == "text":
                        text = msg.get("content", "")
                        if text and session_alive:
                            await session.send_client_content(
                                turns=types.Content(role="user", parts=[types.Part(text=text)])
                            )

                    elif msg_type == "end":
                        logger.info("Client requested session end")
                        break

                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"Error forwarding to Gemini: {err_msg}")
                    if "keepalive" in err_msg or "1011" in err_msg or "closed" in err_msg.lower():
                        session_alive = False
                        await websocket.send_json({"type": "error", "message": "Voice session disconnected. Please start a new session."})
                        break
                    else:
                        await websocket.send_json({"type": "error", "message": err_msg})

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except asyncio.TimeoutError:
        logger.info("Config message timeout")
    except Exception as e:
        logger.error(f"Session error: {e}")
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
        try:
            await websocket.close()
        except:
            pass
        logger.info("Session cleaned up")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
