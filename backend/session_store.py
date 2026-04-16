from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import Session, SessionTurn, SessionObservation, SessionToolRun
from typing import List, Optional, Dict
import uuid
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class SessionStore:

    @staticmethod
    async def create_session(db: AsyncSession, persona_id: str, voice_id: str) -> Session:
        session = Session(
            id=str(uuid.uuid4()),
            persona_id=persona_id,
            voice_id=voice_id,
            status="active",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        logger.info(f"Created session {session.id}")
        return session

    @staticmethod
    async def get_session(db: AsyncSession, session_id: str) -> Optional[Session]:
        result = await db.execute(select(Session).where(Session.id == session_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def update_session(db: AsyncSession, session_id: str, **kwargs) -> Optional[Session]:
        session = await SessionStore.get_session(db, session_id)
        if not session:
            return None
        for k, v in kwargs.items():
            if hasattr(session, k):
                setattr(session, k, v)
        session.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return session

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
    async def add_turn(db: AsyncSession, session_id: str, role: str, content: str, source_type: str = "voice") -> SessionTurn:
        turn = SessionTurn(id=str(uuid.uuid4()), session_id=session_id, role=role, content=content, source_type=source_type)
        db.add(turn)
        await db.commit()
        return turn

    @staticmethod
    async def get_turns(db: AsyncSession, session_id: str, limit: int = 50) -> List[SessionTurn]:
        result = await db.execute(
            select(SessionTurn).where(SessionTurn.session_id == session_id).order_by(SessionTurn.created_at.desc()).limit(limit)
        )
        return list(reversed(result.scalars().all()))

    @staticmethod
    async def add_observation(db: AsyncSession, session_id: str, obs_type: str, summary: str = None, data: dict = None, confidence: float = 0.5) -> SessionObservation:
        obs = SessionObservation(id=str(uuid.uuid4()), session_id=session_id, obs_type=obs_type, summary=summary, structured_data=data, confidence=confidence)
        db.add(obs)
        await db.commit()
        return obs

    @staticmethod
    async def get_observations(db: AsyncSession, session_id: str, limit: int = 20) -> List[SessionObservation]:
        result = await db.execute(
            select(SessionObservation).where(SessionObservation.session_id == session_id).order_by(SessionObservation.created_at.desc()).limit(limit)
        )
        return list(reversed(result.scalars().all()))

    @staticmethod
    async def get_tool_runs(db: AsyncSession, session_id: str, limit: int = 20) -> List[SessionToolRun]:
        result = await db.execute(
            select(SessionToolRun).where(SessionToolRun.session_id == session_id).order_by(SessionToolRun.created_at.desc()).limit(limit)
        )
        return list(reversed(result.scalars().all()))

    @staticmethod
    async def get_session_state(db: AsyncSession, session_id: str) -> Optional[Dict]:
        session = await SessionStore.get_session(db, session_id)
        if not session:
            return None
        turns = await SessionStore.get_turns(db, session_id, limit=30)
        observations = await SessionStore.get_observations(db, session_id, limit=10)
        tool_runs = await SessionStore.get_tool_runs(db, session_id, limit=10)
        return {
            "session_id": session.id,
            "status": session.status,
            "persona_id": session.persona_id,
            "voice_id": session.voice_id,
            "active_device_type": session.active_device_type,
            "active_device_model": session.active_device_model,
            "active_manual_id": session.active_manual_id,
            "current_step": session.current_step,
            "pending_goal": session.pending_goal,
            "warnings_given": session.warnings_given,
            "turns": [{"role": t.role, "content": t.content, "source_type": t.source_type, "created_at": t.created_at.isoformat()} for t in turns],
            "observations": [{"type": o.obs_type, "summary": o.summary, "confidence": o.confidence, "created_at": o.created_at.isoformat()} for o in observations],
            "tool_runs": [{"tool_name": tr.tool_name, "status": tr.status, "input": tr.input_data, "output": tr.output_data, "created_at": tr.created_at.isoformat()} for tr in tool_runs],
        }
