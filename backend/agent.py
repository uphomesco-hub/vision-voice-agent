"""
LiveKit Agent Worker for Realtime Repair Assistant
Step 1: Voice only with Gemini Live + Google Search grounding
"""

import asyncio
import logging
from livekit import agents, rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import google
from dotenv import load_dotenv
import os
from pathlib import Path

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

async def entrypoint(ctx: JobContext):
    """
    Main entrypoint for the agent worker.
    Called when a participant joins a room.
    """
    logger.info(f"Agent connecting to room: {ctx.room.name}")
    
    # Connect to the room
    await ctx.connect(auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)
    
    # Get participant info from metadata
    participant = await ctx.wait_for_participant()
    logger.info(f"Participant joined: {participant.identity}")
    
    # Get persona and voice from room metadata (default to calm-expert and puck)
    persona_id = "calm-expert"
    voice_id = "Puck"
    
    # Build system prompt
    system_prompt = build_agent_prompt(persona_id, voice_id)
    
    logger.info(f"Starting voice assistant with persona={persona_id}, voice={voice_id}")
    
    # Create the assistant with Gemini Live
    # Google Search grounding is enabled by default in Gemini 2.0 Live models
    assistant = agents.VoiceAssistant(
        vad=agents.silero.VAD.load(),
        stt=google.STT(
            languages=["en-US"],
        ),
        llm=google.LLM(
            model="gemini-2.0-flash-exp",
            api_key=os.getenv("GOOGLE_API_KEY"),
        ),
        tts=google.TTS(
            voice=voice_id,
            api_key=os.getenv("GOOGLE_API_KEY"),
        ),
        chat_ctx=agents.ChatContext().append(
            role="system",
            text=system_prompt
        )
    )
    
    # Start the assistant
    assistant.start(ctx.room, participant)
    
    # Initial greeting
    await assistant.say(
        "Hello! I'm your repair assistant. What device or problem can I help you troubleshoot today?",
        allow_interruptions=True
    )
    
    logger.info("Assistant started and ready")

if __name__ == "__main__":
    # Run the agent worker
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="repair-assistant"
        )
    )
