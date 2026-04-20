from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer, Float, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone
from pathlib import Path
import os
import uuid

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sessions.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}")

Base = declarative_base()

class Session(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    persona_id = Column(String, nullable=False)
    voice_id = Column(String, nullable=False)
    status = Column(String, default="active")
    should_greet = Column(Boolean, default=True)
    active_device_type = Column(String, nullable=True)
    active_device_model = Column(String, nullable=True)
    active_manual_id = Column(String, nullable=True)
    current_step = Column(Integer, default=0)
    pending_goal = Column(String, nullable=True)
    warnings_given = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class SessionTurn(Base):
    __tablename__ = "session_turns"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    source_type = Column(String, default="voice")  # voice, text, tool, vision
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class SessionObservation(Base):
    __tablename__ = "session_observations"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, nullable=False)
    obs_type = Column(String, nullable=False)  # device_visible, label_visible, housing_opened, etc.
    summary = Column(Text, nullable=True)
    structured_data = Column(JSON, nullable=True)
    confidence = Column(Float, default=0.5)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class SessionToolRun(Base):
    __tablename__ = "session_tool_runs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, nullable=False)
    tool_name = Column(String, nullable=False)
    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    status = Column(String, default="running")  # running, success, error
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

class Manual(Base):
    __tablename__ = "manuals"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    brand = Column(String, nullable=False)
    model = Column(String, nullable=False)
    device_type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    json_data = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class SessionSnapshot(Base):
    __tablename__ = "session_snapshots"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, nullable=False)
    state_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
