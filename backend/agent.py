"""
LiveKit Agent Worker for Realtime Repair Assistant
Step 1: Voice only with Gemini + Google Search grounding
"""

import logging
from dotenv import load_dotenv
from pathlib import Path
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, JobContext
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# Load environment
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import prompts
from prompts import build_agent_prompt

class RepairAssistant(Agent):
    """Voice-first repair troubleshooting assistant."""
    
    def __init__(self, persona_id: str = "calm-expert", voice_id: str = "Puck"):
        # Build system prompt
        system_prompt = build_agent_prompt(persona_id, voice_id)
        
        super().__init__(
            instructions=system_prompt
        )
        self.persona_id = persona_id
        self.voice_id = voice_id
        logger.info(f"RepairAssistant initialized with persona={persona_id}, voice={voice_id}")

# Create server
server = AgentServer()

@server.rtc_session(agent_name="repair-assistant")
async def entrypoint(ctx: JobContext):
    """
    Main entrypoint - called when participant joins room.
    """
    logger.info(f"Agent session starting for room: {ctx.room.name}")
    
    # Create session with Gemini
    # Note: Using OpenAI temporarily as Gemini plugin requires google.generativeai
    # Google Search grounding will be enabled through proper plugin setup
    session = AgentSession(
        # STT - Speech to Text
        stt="deepgram/nova-3:multi",
        
        # LLM - Using OpenAI as placeholder (replace with google plugin when available)
        llm="openai/gpt-4o-mini",
        
        # TTS - Text to Speech  
        tts="openai/tts-1:alloy",
        
        # VAD - Voice Activity Detection
        vad=silero.VAD.load(),
        
        # Turn detection
        turn_detection=MultilingualModel(),
    )
    
    # Start session
    await session.start(
        room=ctx.room,
        agent=RepairAssistant(persona_id="calm-expert", voice_id="Puck")
    )
    
    # Initial greeting
    logger.info("Generating initial greeting")
    await session.generate_reply(
        instructions=(
            "Greet the user warmly and introduce yourself as a repair assistant. "
            "Ask what device or problem they need help troubleshooting. "
            "Keep it under 2 sentences."
        )
    )
    
    logger.info("Agent session ready and greeting sent")

if __name__ == "__main__":
    logger.info("🚀 Starting Repair Assistant Agent Worker...")
    agents.cli.run_app(server)
