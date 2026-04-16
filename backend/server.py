from fastapi import FastAPI, APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
import asyncio
import websockets

from config import settings
from db import get_db, init_db
from schemas import *
from session_store import SessionStore
from livekit_token import generate_token
from personas import get_personas, get_voices, get_persona, get_voice

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized")
    yield
    # Shutdown
    logger.info("Shutting down...")

# Create FastAPI app
app = FastAPI(
    title="Realtime Repair Assistant",
    description="Voice-first repair assistant with Gemini Live and LiveKit",
    version="1.0.0",
    lifespan=lifespan
)

# CORS - Allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Router
api_router = APIRouter(prefix="/api")

@api_router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "agent_name": settings.agent_name,
        "livekit_url": settings.livekit_url
    }

@api_router.get("/personas", response_model=List[PersonaResponse])
async def list_personas():
    """Get available personas"""
    return get_personas()

@api_router.get("/voices", response_model=List[VoiceResponse])
async def list_voices():
    """Get available voices"""
    return get_voices()

@api_router.post("/sessions", response_model=SessionResponse)
async def create_session(
    payload: SessionCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new session"""
    try:
        session = await SessionStore.create_session(
            db,
            persona_id=payload.persona_id,
            voice_id=payload.voice_id
        )
        
        return SessionResponse(
            id=session.id,
            persona_id=session.persona_id,
            voice_id=session.voice_id,
            status=session.status,
            room_name=session.room_name,
            created_at=session.created_at,
            updated_at=session.updated_at
        )
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/sessions/{session_id}/token", response_model=TokenResponse)
async def get_session_token(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Generate LiveKit token for a session"""
    try:
        session = await SessionStore.get_session(db, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Generate token
        token = generate_token(
            room_name=session.room_name,
            participant_identity=f"user-{session_id[:8]}",
            participant_name="User"
        )
        
        # Use external WebSocket proxy URL for browser connectivity
        import os
        backend_url = os.environ.get('REACT_APP_BACKEND_URL', '')
        if backend_url:
            # Convert https:// to wss:// for WebSocket
            ws_url = backend_url.replace('https://', 'wss://').replace('http://', 'ws://')
            external_livekit_url = f"{ws_url}/api/livekit-ws"
        else:
            external_livekit_url = settings.livekit_url
        
        return TokenResponse(
            token=token,
            livekit_url=external_livekit_url,
            room_name=session.room_name
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating token: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/sessions/{session_id}/state", response_model=SessionStateResponse)
async def get_session_state(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get session state and turns"""
    try:
        session = await SessionStore.get_session(db, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        turns = await SessionStore.get_turns(db, session_id)
        
        return SessionStateResponse(
            session_id=session.id,
            status=session.status,
            turns=[
                {
                    "role": turn.role,
                    "content": turn.content,
                    "timestamp": turn.timestamp.isoformat()
                }
                for turn in turns
            ],
            agent_state="connected" if session.status == "active" else "disconnected"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session state: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """End a session"""
    try:
        success = await SessionStore.end_session(db, session_id)
        if not success:
            raise HTTPException(status_code=404, detail="Session not found")
        
        return {"status": "ended", "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ending session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Include API router
app.include_router(api_router)

# WebSocket proxy for LiveKit signaling
@app.websocket("/api/livekit-ws/{path:path}")
async def livekit_ws_proxy(websocket: WebSocket, path: str = ""):
    """Proxy WebSocket connections to local LiveKit server"""
    await websocket.accept()
    
    # Forward query params and path to local LiveKit
    query_string = websocket.scope.get("query_string", b"").decode()
    livekit_ws_url = f"ws://localhost:7880/{path}"
    if query_string:
        livekit_ws_url += f"?{query_string}"
    
    logger.info(f"LiveKit WS proxy connecting to: {livekit_ws_url}")
    
    try:
        async with websockets.connect(
            livekit_ws_url,
            additional_headers={},
            max_size=None,
            ping_interval=None
        ) as lk_ws:
            async def forward_to_livekit():
                try:
                    while True:
                        data = await websocket.receive()
                        if "text" in data:
                            await lk_ws.send(data["text"])
                        elif "bytes" in data:
                            await lk_ws.send(data["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass
            
            async def forward_to_client():
                try:
                    async for message in lk_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception:
                    pass
            
            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_to_livekit()), asyncio.create_task(forward_to_client())],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                
    except Exception as e:
        logger.error(f"LiveKit WS proxy error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
