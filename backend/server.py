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

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load env
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-preview-native-audio-dialog"
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting...")
    yield
    logger.info("Server shutting down...")

app = FastAPI(title="Repair Assistant", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """
    Main voice session WebSocket.
    
    Client sends:
      { "type": "config", "persona_id": "...", "voice_id": "..." }  -- first message
      { "type": "audio", "data": "<base64 PCM16 16kHz mono>" }
      { "type": "video", "data": "<base64 JPEG>" }
      { "type": "text", "content": "..." }
    
    Server sends:
      { "type": "status", "message": "..." }
      { "type": "audio", "data": "<base64 PCM16 24kHz mono>" }
      { "type": "transcription", "role": "user"|"assistant", "text": "..." }
      { "type": "error", "message": "..." }
      { "type": "interrupted" }
      { "type": "turn_complete" }
    """
    await websocket.accept()
    logger.info("Client WebSocket connected")

    gemini_session = None
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
        enable_vision = config_msg.get("enable_vision", False)

        await websocket.send_json({"type": "status", "message": "Connecting to Gemini Live..."})

        # Build system instruction
        from prompts import build_agent_prompt
        system_instruction = build_agent_prompt(persona_id, voice_id)

        # Connect to Gemini Live API
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
            gemini_session = session
            logger.info(f"Connected to Gemini Live API with persona={persona_id}, voice={voice_id}")
            await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

            # Background task: receive responses from Gemini and forward to client
            async def receive_from_gemini():
                try:
                    async for response in session.receive():
                        try:
                            sc = response.server_content
                            if sc is None:
                                continue

                            # Audio response
                            if sc.model_turn and sc.model_turn.parts:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        audio_b64 = base64.b64encode(part.inline_data.data).decode()
                                        await websocket.send_json({
                                            "type": "audio",
                                            "data": audio_b64,
                                        })

                            # Input transcription (what user said)
                            if sc.input_transcription and sc.input_transcription.text:
                                await websocket.send_json({
                                    "type": "transcription",
                                    "role": "user",
                                    "text": sc.input_transcription.text,
                                })

                            # Output transcription (what assistant said)
                            if sc.output_transcription and sc.output_transcription.text:
                                await websocket.send_json({
                                    "type": "transcription",
                                    "role": "assistant",
                                    "text": sc.output_transcription.text,
                                })

                            # Interrupted
                            if sc.interrupted:
                                await websocket.send_json({"type": "interrupted"})

                            # Turn complete
                            if sc.turn_complete:
                                await websocket.send_json({"type": "turn_complete"})

                        except Exception as e:
                            logger.error(f"Error processing Gemini response: {e}")
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Gemini receive loop error: {e}")
                    try:
                        await websocket.send_json({"type": "error", "message": str(e)})
                    except:
                        pass

            receive_task = asyncio.create_task(receive_from_gemini())

            # Main loop: receive from client and forward to Gemini
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "message": "Session timeout (5 min idle)"})
                    break

                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "audio":
                        audio_bytes = base64.b64decode(msg["data"])
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=audio_bytes,
                                mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}"
                            )
                        )

                    elif msg_type == "video":
                        frame_bytes = base64.b64decode(msg["data"])
                        await session.send_realtime_input(
                            video=types.Blob(
                                data=frame_bytes,
                                mime_type="image/jpeg"
                            )
                        )

                    elif msg_type == "text":
                        text = msg.get("content", "")
                        if text:
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[types.Part(text=text)]
                                )
                            )

                    elif msg_type == "end":
                        logger.info("Client requested session end")
                        break

                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                except Exception as e:
                    logger.error(f"Error forwarding to Gemini: {e}")
                    await websocket.send_json({"type": "error", "message": str(e)})

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
