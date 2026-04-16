from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import Session, SessionTurn
from typing import List, Optional
import uuid
import logging

logger = logging.getLogger(__name__)

class SessionStore:
    
    @staticmethod
    async def create_session(db: AsyncSession, persona_id: str, voice_id: str) -> Session:
        room_name = f"room-{uuid.uuid4().hex[:12]}"
        session = Session(
            id=str(uuid.uuid4()),
            persona_id=persona_id,
            voice_id=voice_id,
            room_name=room_name,
            status="active"
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        logger.info(f"Created session {session.id} with room {room_name}")
        return session
    
    @staticmethod
    async def get_session(db: AsyncSession, session_id: str) -> Optional[Session]:
        result = await db.execute(select(Session).where(Session.id == session_id))
        return result.scalar_one_or_none()
    
    @staticmethod
    async def end_session(db: AsyncSession, session_id: str) -> bool:
        session = await SessionStore.get_session(db, session_id)
        if session:
            session.status = "ended"
            await db.commit()
            logger.info(f"Ended session {session_id}")
            return True
        return False
    
    @staticmethod
    async def add_turn(db: AsyncSession, session_id: str, role: str, content: str) -> SessionTurn:
        turn = SessionTurn(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn
    
    @staticmethod
    async def get_turns(db: AsyncSession, session_id: str, limit: int = 50) -> List[SessionTurn]:
        result = await db.execute(
            select(SessionTurn)
            .where(SessionTurn.session_id == session_id)
            .order_by(SessionTurn.timestamp.desc())
            .limit(limit)
        )
        turns = result.scalars().all()
        return list(reversed(turns))  # Return in chronological order