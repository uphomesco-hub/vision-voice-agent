from fastapi import FastAPI, APIRouter, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
import logging
from pathlib import Path

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
        
        return TokenResponse(
            token=token,
            livekit_url=settings.livekit_url,
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

# Serve static frontend files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "public"

@app.get("/")
async def serve_index():
    """Serve the main HTML page"""
    return FileResponse(FRONTEND_DIR / "index.html")

# Mount static files
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
