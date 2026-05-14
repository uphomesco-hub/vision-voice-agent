"""
LiveKit Agent Worker for Zeno AI
Step 1: Voice only with Gemini Live API + Google Search grounding
"""

import logging
from dotenv import load_dotenv
from pathlib import Path
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, JobContext
from livekit.plugins import google

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
    """Voice-first repair troubleshooting assistant using Gemini Live API."""
    
    def __init__(self, persona_id: str = "calm-expert", voice_id: str = "Puck"):
        # Build system prompt with Google Search grounding instructions
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
    Uses Gemini Live API with native audio for realtime voice interaction.
    """
    logger.info(f"Agent session starting for room: {ctx.room.name}")
    
    # Create session with Gemini Live API
    # This uses the RealtimeModel which supports:
    # - Native audio input/output
    # - Google Search grounding (via model capabilities)
    # - Low latency streaming
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio",
            voice="Puck",
            temperature=0.8,
            instructions=build_agent_prompt("calm-expert", "Puck"),
        ),
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
    logger.info("Starting Zeno AI Agent Worker with Gemini Live API...")
    agents.cli.run_app(server)
