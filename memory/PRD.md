# Voice-First Realtime Repair Assistant - PRD

## Original Problem Statement
Build a web-based, voice-first realtime repair assistant. The app requires a Python/FastAPI backend, a realtime agent worker using `livekit-agents` with the Gemini Live API (`livekit-plugins-google`) and Google Search grounding, a local LiveKit server, SQLite persistence, and a React frontend with a dark-theme UI.

## Architecture
```
Frontend (React, port 3000) → K8s Ingress → Backend (FastAPI, port 8001)
                                                  ↓ WebSocket proxy
                                            LiveKit Server (port 7880)
                                                  ↓
                                            Agent Worker (Gemini Live API)
```

## Tech Stack
- **Frontend**: React 19, CSS (dark theme), livekit-client
- **Backend**: FastAPI, SQLAlchemy (SQLite), livekit-agents, livekit-plugins-google
- **Voice**: LiveKit Server (local), Gemini Live API via Emergent LLM Key
- **Database**: SQLite (sessions, session_turns)

## Key API Endpoints
- `GET /api/health` - Health check
- `GET /api/personas` - List assistant personas
- `GET /api/voices` - List available voices
- `POST /api/sessions` - Create voice session
- `POST /api/sessions/{id}/token` - Get LiveKit token (returns wss:// proxy URL)
- `GET /api/sessions/{id}/state` - Get session state + transcript
- `POST /api/sessions/{id}/end` - End session
- `WS /api/livekit-ws/{path}` - WebSocket proxy to LiveKit server

## DB Schema
- `sessions`: id, persona_id, voice_id, status, room_name, created_at, updated_at
- `session_turns`: id, session_id, role, text, created_at

## What's Implemented (Step 1 MVP) - April 16, 2026
- [x] FastAPI backend with all API endpoints
- [x] SQLite database with sessions and turns tables
- [x] LiveKit token generation
- [x] Personas and voices registries
- [x] Local LiveKit server (v1.10.1, ARM64)
- [x] Agent worker with Gemini Live API plugin
- [x] React frontend with dark-theme UI (cinematic sentinel design)
- [x] Frontend-backend API connectivity (REACT_APP_BACKEND_URL)
- [x] WebSocket proxy for LiveKit signaling through K8s ingress
- [x] Cleaned up stale vanilla HTML, setupProxy.js, client-side prototype

## Backlog

### P1 - UI State Feedback
- Add "Listening", "Thinking", "Replying" visual indicators based on LiveKit agent state events

### P2 - Step 2 Features (Future)
- Camera/Vision capabilities
- Manual lookup tool
- Warnings/repair observations panel
- Repair-step panel
