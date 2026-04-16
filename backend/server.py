from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio
import base64
import json
import os
import websockets

from personas import get_personas, get_voices

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

GEMINI_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
MODEL_ID = "gemini-2.5-flash-native-audio-latest"
INPUT_SAMPLE_RATE = 16000
GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

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

    gemini_ws = None
    receive_task = None
    session_alive = True

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

        # Connect to Gemini via raw WebSocket — full control over keepalive
        logger.info(f"Connecting to Gemini raw WS: model={MODEL_ID}, voice={voice_id}")
        gemini_ws = await websockets.connect(
            GEMINI_WS_URL,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=5,
            max_size=16 * 1024 * 1024,  # 16MB max message
        )

        # Send setup message with context compression for long sessions
        setup_msg = {
            "setup": {
                "model": f"models/{MODEL_ID}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {
                            "prebuilt_voice_config": {
                                "voice_name": voice_id
                            }
                        }
                    }
                },
                "system_instruction": {
                    "parts": [{"text": system_instruction}]
                },
                "session_resumption": {},
                "input_audio_transcription": {},
                "output_audio_transcription": {}
            }
        }
        await gemini_ws.send(json.dumps(setup_msg))
        
        # Wait for setup response
        setup_resp = await asyncio.wait_for(gemini_ws.recv(), timeout=10)
        setup_data = json.loads(setup_resp)
        logger.info(f"Gemini setup response: {json.dumps(setup_data)[:200]}")

        logger.info(f"Gemini Live connected: persona={persona_id}, voice={voice_id}")
        await websocket.send_json({"type": "status", "message": "Connected! Start speaking..."})

        # Background: receive from Gemini, forward to client
        async def receive_from_gemini():
            nonlocal session_alive
            try:
                async for raw_msg in gemini_ws:
                    if not session_alive:
                        break
                    try:
                        data = json.loads(raw_msg)
                        sc = data.get("serverContent")
                        if sc is None:
                            # Could be a toolCall or other message type
                            if "toolCall" in data:
                                logger.info(f"Tool call received: {json.dumps(data)[:200]}")
                            continue

                        # Audio response
                        model_turn = sc.get("modelTurn")
                        if model_turn and model_turn.get("parts"):
                            for part in model_turn["parts"]:
                                inline_data = part.get("inlineData")
                                if inline_data and inline_data.get("data"):
                                    await websocket.send_json({
                                        "type": "audio",
                                        "data": inline_data["data"],  # Already base64 from Gemini
                                    })

                        # Input transcription
                        input_tx = sc.get("inputTranscription")
                        if input_tx and input_tx.get("text"):
                            await websocket.send_json({
                                "type": "transcription",
                                "role": "user",
                                "text": input_tx["text"],
                            })

                        # Output transcription
                        output_tx = sc.get("outputTranscription")
                        if output_tx and output_tx.get("text"):
                            await websocket.send_json({
                                "type": "transcription",
                                "role": "assistant",
                                "text": output_tx["text"],
                            })

                        # Interrupted
                        if sc.get("interrupted"):
                            await websocket.send_json({"type": "interrupted"})

                        # Turn complete — session stays alive for next turn
                        if sc.get("turnComplete"):
                            logger.info("Turn complete — ready for next input")
                            await websocket.send_json({"type": "turn_complete"})

                    except Exception as e:
                        logger.error(f"Error processing Gemini msg: {e}")

            except websockets.exceptions.ConnectionClosedOK:
                logger.info("Gemini connection closed normally")
            except websockets.exceptions.ConnectionClosedError as e:
                logger.error(f"Gemini connection closed with error: {e}")
                session_alive = False
                try:
                    await websocket.send_json({"type": "error", "message": "Voice session disconnected. Please start a new session."})
                except:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Gemini receive error: {e}")
                session_alive = False

        receive_task = asyncio.create_task(receive_from_gemini())

        # Main loop: receive from client, forward to Gemini
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
                msg_type = msg.get("type")

                if msg_type == "audio":
                    if not session_alive:
                        break
                    # Forward audio as realtime input
                    audio_msg = {
                        "realtimeInput": {
                            "mediaChunks": [{
                                "mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                "data": msg["data"]  # Already base64 from client
                            }]
                        }
                    }
                    await gemini_ws.send(json.dumps(audio_msg))

                elif msg_type == "video":
                    if not session_alive:
                        break
                    video_msg = {
                        "realtimeInput": {
                            "mediaChunks": [{
                                "mimeType": "image/jpeg",
                                "data": msg["data"]
                            }]
                        }
                    }
                    await gemini_ws.send(json.dumps(video_msg))

                elif msg_type == "text":
                    text = msg.get("content", "")
                    if text and session_alive:
                        text_msg = {
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": text}]}],
                                "turnComplete": True
                            }
                        }
                        await gemini_ws.send(json.dumps(text_msg))

                elif msg_type == "end":
                    logger.info("Client requested session end")
                    break

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
            except websockets.exceptions.ConnectionClosed:
                logger.error("Gemini WS closed while sending")
                session_alive = False
                await websocket.send_json({"type": "error", "message": "Voice session disconnected. Please start a new session."})
                break
            except Exception as e:
                logger.error(f"Error forwarding to Gemini: {e}")
                if "closed" in str(e).lower():
                    session_alive = False
                    await websocket.send_json({"type": "error", "message": "Voice session disconnected. Please start a new session."})
                    break

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
        if gemini_ws:
            try:
                await gemini_ws.close()
            except:
                pass
        try:
            await websocket.close()
        except:
            pass
        logger.info("Session cleaned up")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
