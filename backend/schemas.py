from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

class PersonaResponse(BaseModel):
    id: str
    name: str
    description: str

class VoiceResponse(BaseModel):
    id: str
    name: str
    description: str

class SessionCreate(BaseModel):
    persona_id: str
    voice_id: str

class SessionResponse(BaseModel):
    id: str
    persona_id: str
    voice_id: str
    status: str
    room_name: str
    created_at: datetime
    updated_at: datetime

class TokenRequest(BaseModel):
    session_id: str

class TokenResponse(BaseModel):
    token: str
    livekit_url: str
    room_name: str

class SessionStateResponse(BaseModel):
    session_id: str
    status: str
    turns: List[dict]
    agent_state: Optional[str] = None
    user_state: Optional[str] = None

class TurnCreate(BaseModel):
    role: str
    content: str