# Voice-First Realtime Repair Assistant - PRD

## Original Problem Statement
Build a web-based, voice-first realtime repair assistant with voice and vision capabilities, powered by Gemini Live API. Step 2 adds camera vision, manual lookup tool, session persistence, and side panels.

## Architecture (v2.0 - Direct Gemini Live + Vision)
```
Browser (mic + camera)
    ↕ WebSocket (JSON: audio chunks + JPEG frames + events)
FastAPI Backend (/api/ws/session)
    ↕ Raw WebSocket (google BidiGenerateContent)
Gemini Live API (gemini-2.5-flash-native-audio-latest)
    → Tool calling: lookup_manual → SQLite manuals DB
```

## Tech Stack
- **Frontend**: React 19, CSS (dark theme), browser AudioContext + getUserMedia + camera
- **Backend**: FastAPI, SQLAlchemy (SQLite), raw WebSocket to Gemini
- **Voice**: Gemini Live API (native audio model)
- **Vision**: Camera frames (JPEG 640x480 every 2s) sent via Gemini realtimeInput
- **Tools**: lookup_manual (function calling via Gemini)
- **API Key**: Google API Key (direct)

## DB Schema
- `sessions`: id, persona_id, voice_id, status, active_device_type, active_device_model, active_manual_id, current_step, pending_goal, warnings_given, timestamps
- `session_turns`: id, session_id, role, content, source_type, created_at
- `session_observations`: id, session_id, obs_type, summary, structured_data, confidence, created_at
- `session_tool_runs`: id, session_id, tool_name, input_data, output_data, status, error, timestamps
- `manuals`: id, brand, model, device_type, title, json_data, timestamps
- `session_snapshots`: id, session_id, state_data, created_at

## Key API Endpoints
- `GET /api/health` - Health check (v2.0-step2)
- `GET /api/personas` - List personas (4)
- `GET /api/voices` - List voices (5)
- `POST /api/sessions` - Create session
- `GET /api/sessions/{id}/state` - Full session state (turns, observations, tool_runs)
- `GET /api/sessions/{id}/logs` - Session logs
- `POST /api/sessions/{id}/end` - End session
- `WS /api/ws/session` - Realtime voice + vision session

## WebSocket Protocol
**Client → Server:** config, audio, video, text, heartbeat, end
**Server → Client:** session.ready, status, assistant.state, audio, transcription, turn_complete, interrupted, tool.status, error, heartbeat

## Seed Manuals
1. Stihl FS 56 RC String Trimmer (stihl-fs56rc)
2. Dyson V15 Detect Cordless Vacuum (dyson-v15)

## What's Implemented

### Step 1 (April 16, 2026)
- [x] Voice conversation with Gemini Live API
- [x] Auto-reconnect for K8s 60s limit
- [x] Dark theme UI
- [x] Persona/voice selection

### Step 2 Phase A+B (April 16, 2026)
- [x] Camera feed in UI (live video element, JPEG frames every 2s)
- [x] Video frames sent to Gemini alongside audio
- [x] Expanded DB schema (observations, tool_runs, manuals, snapshots)
- [x] Manual repository with search + ranking
- [x] 2 seed manuals (Stihl trimmer, Dyson vacuum)
- [x] lookup_manual tool via Gemini function calling
- [x] Tool activity shown in UI panel
- [x] Active manual panel
- [x] Warnings panel
- [x] Troubleshooting steps panel
- [x] Voice state aura animations (listening/speaking/thinking)
- [x] Session CRUD API (create, state, logs, end)
- [x] Transcript persistence to DB
- [x] Updated prompt for vision + tools + repair behavior

## Backlog

### P1 - Phase C: Nudge Engine + Smart Behavior
- Semantic vision layer with scene schema + state diffing
- Nudge engine with per-type cooldowns
- Step-by-step tracking from manual
- Proactive but restrained visual observations

### P1 - Phase D: UI Polish
- Transcript drawer toggle
- Vision activity status indicator
- Calm state transitions (damped rapid changes)
- Mobile responsive layout

### P2 - Phase E: Session Persistence + Reconnect
- Full DB-backed session state for reconnect
- Session snapshots for restart recovery
- Structured logging by session_id

### P3 - Future
- Camera/Vision capabilities refinement
- Context window compression for long sessions
