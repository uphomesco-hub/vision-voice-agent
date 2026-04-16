from pydantic_settings import BaseSettings
from pydantic import Field
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

class Settings(BaseSettings):
    # MongoDB
    mongo_url: str = Field(default="mongodb://localhost:27017")
    db_name: str = Field(default="test_database")
    cors_origins: str = Field(default="*")
    
    # LiveKit
    livekit_url: str = Field(default="ws://localhost:7880")
    livekit_api_key: str = Field(default="devkey")
    livekit_api_secret: str = Field(default="secret")
    
    # Google/Gemini
    google_api_key: str = Field(default="")
    
    # OpenAI (for plugins)
    openai_api_key: str = Field(default="")
    
    # Deepgram
    deepgram_api_key: str = Field(default="")
    
    # Application
    agent_name: str = Field(default="repair-assistant")
    log_level: str = Field(default="INFO")
    max_session_duration: int = Field(default=3600)
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Ignore extra fields

settings = Settings(
    mongo_url=os.getenv('MONGO_URL', 'mongodb://localhost:27017'),
    db_name=os.getenv('DB_NAME', 'test_database'),
    cors_origins=os.getenv('CORS_ORIGINS', '*'),
    livekit_url=os.getenv('LIVEKIT_URL', 'ws://localhost:7880'),
    livekit_api_key=os.getenv('LIVEKIT_API_KEY', 'devkey'),
    livekit_api_secret=os.getenv('LIVEKIT_API_SECRET', 'secret'),
    google_api_key=os.getenv('GOOGLE_API_KEY', ''),
    openai_api_key=os.getenv('OPENAI_API_KEY', ''),
    deepgram_api_key=os.getenv('DEEPGRAM_API_KEY', ''),
    agent_name=os.getenv('AGENT_NAME', 'repair-assistant'),
    log_level=os.getenv('LOG_LEVEL', 'INFO'),
    max_session_duration=int(os.getenv('MAX_SESSION_DURATION', '3600'))
)