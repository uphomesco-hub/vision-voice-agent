# Realtime Repair Assistant - Step 1

A voice-first web application for device troubleshooting using Gemini Live API with Google Search grounding and LiveKit for realtime transport.

## Step 1 Features

✅ Voice-only interface (no camera/vision yet)
✅ Gemini Live API with Google Search grounding
✅ LiveKit for browser-to-agent realtime audio transport
✅ SQLite persistence for sessions and turns
✅ Personas and voice selection
✅ Live transcript display
✅ Cinematic "Sentinel" UI design

## Prerequisites

- Python 3.10+
- Node.js 16+ (for LiveKit server if needed)
- Google API key with Gemini access (or Emergent Universal Key)

## Environment Variables

Create `/app/backend/.env` with:

```
# MongoDB (template default)
MONGO_URL=mongodb://localhost:27017
DB_NAME=test_database
CORS_ORIGINS=*

# LiveKit (local development)
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

# Gemini API
GOOGLE_API_KEY=your_google_api_key_here

# Application
AGENT_NAME=repair-assistant
LOG_LEVEL=INFO
MAX_SESSION_DURATION=3600
```

## Local Setup Instructions

### 1. Install LiveKit Server

**Option A: Homebrew (macOS)**
```bash
brew install livekit
```

**Option B: Shell Script (Linux/macOS)**
```bash
curl -sSL https://get.livekit.io | bash
```

**Option C: Download Binary**
Download from https://github.com/livekit/livekit/releases

### 2. Start LiveKit Server

```bash
livekit-server --dev
```

This starts the server with default credentials:
- API Key: `devkey`
- API Secret: `secret`
- WebSocket URL: `ws://localhost:7880`

### 3. Install Python Dependencies

```bash
cd /app/backend
pip install -r requirements.txt
```

### 4. Start Backend Server

```bash
cd /app/backend
python server.py
```

The FastAPI server will run on http://localhost:8001

### 5. Start Agent Worker

In a new terminal:

```bash
cd /app/backend
python agent.py dev
```

The agent worker will connect to LiveKit and await dispatch.

### 6. Access the Application

Open your browser to:
```
http://localhost:8001
```

## Usage Flow

1. **Select Persona**: Choose the assistant personality
2. **Select Voice**: Choose the voice style
3. **Start Session**: Click "Start Session" and grant microphone permission
4. **Talk Naturally**: Speak to the assistant about your device problem
5. **View Transcript**: See the conversation in real-time
6. **End Session**: Click the end call button when done

## API Endpoints

### Health Check
```
GET /api/health
```

### List Personas
```
GET /api/personas
```

### List Voices
```
GET /api/voices
```

### Create Session
```
POST /api/sessions
Body: { "persona_id": "calm-expert", "voice_id": "puck" }
```

### Get Session Token
```
POST /api/sessions/{session_id}/token
```

### Get Session State
```
GET /api/sessions/{session_id}/state
```

### End Session
```
POST /api/sessions/{session_id}/end
```

## Architecture

```
Browser (HTML/CSS/JS)
    ↓
    ↓ HTTP/API calls
    ↓
FastAPI Backend (server.py)
    ↓
    ↓ Token generation
    ↓
LiveKit Server (local)
    ↓
    ↓ WebRTC audio
    ↓
Agent Worker (agent.py)
    ↓
    ↓ Gemini Live API
    ↓
Google Search Grounding
```

## Database Schema

### sessions
- id (str, primary key)
- persona_id (str)
- voice_id (str)
- status (str: active/ended)
- room_name (str, unique)
- created_at (datetime)
- updated_at (datetime)

### session_turns
- id (str, primary key)
- session_id (str)
- role (str: user/assistant)
- content (text)
- timestamp (datetime)

### session_snapshots
- id (str, primary key)
- session_id (str)
- state_data (text/json)
- timestamp (datetime)

## Agent Behavior

The agent in Step 1:
- ✅ Listens to user speech via microphone
- ✅ Transcribes using Gemini STT
- ✅ Processes with Gemini Live model
- ✅ Uses Google Search grounding for current information
- ✅ Responds with natural speech
- ❌ Cannot see the device (camera disabled)
- ❌ Cannot access device manuals (no manual lookup)

## Step 2 Roadmap

The next step will add:
- 📹 Camera input from browser
- 👁️ Vision understanding with Gemini
- 🔍 Semantic vision watcher for device state
- 📚 Manual lookup tool
- ⚠️ Warnings and repair observations panel
- 🔧 Structured repair step guidance

## Troubleshooting

### Microphone not working
- Ensure browser has microphone permission
- Check browser console for errors
- Verify microphone is not used by other apps

### Cannot connect to LiveKit
- Verify LiveKit server is running (`livekit-server --dev`)
- Check LIVEKIT_URL in .env matches server address
- Ensure no firewall blocks port 7880

### Agent not responding
- Check agent worker is running (`python agent.py dev`)
- Verify GOOGLE_API_KEY is valid
- Check agent logs for errors

### No audio playback
- Ensure speakers/headphones are connected
- Check browser audio settings
- Verify AudioContext is not blocked by browser

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, SQLAlchemy
- **Database**: SQLite with async support
- **Frontend**: Plain HTML, CSS, JavaScript
- **Realtime**: LiveKit (WebRTC)
- **AI**: Gemini Live API with Google Search grounding
- **Agent Framework**: livekit-agents
- **Design**: Cinematic Sentinel aesthetic

## License

Internal development project.
