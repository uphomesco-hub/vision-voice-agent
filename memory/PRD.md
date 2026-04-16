# Voice-First Realtime Repair Assistant - PRD

## Original Problem Statement
Build a web-based, voice-first realtime repair assistant with voice and vision capabilities, powered by Gemini Live API.

## Architecture (v2 - Direct Gemini Live)
```
Browser (mic + camera)
    ↕ WebSocket (JSON + base64 audio)
FastAPI Backend (/api/ws/session)
    ↕ WebSocket (google-genai SDK)
Gemini Live API (gemini-2.5-flash-preview-native-audio-dialog)
```

**No LiveKit, no WebRTC** — pure WebSocket streaming. Works through any HTTP proxy/ingress.

## Tech Stack
- **Frontend**: React 19, CSS (dark theme), browser AudioContext + getUserMedia
- **Backend**: FastAPI, google-genai SDK (v1.71.0)
- **Voice**: Gemini Live API (native audio dialog model)
- **API Key**: Google API Key (direct, not Emergent proxy — Live API requires WebSocket which Emergent proxy doesn't support)

## Key API Endpoints
- `GET /api/health` - Health check (returns model name + mode)
- `GET /api/personas` - List assistant personas (4)
- `GET /api/voices` - List voices (Puck, Charon, Kore, Aoede, Fenrir)
- `WS /api/ws/session` - Main voice session WebSocket

## WebSocket Protocol (/api/ws/session)
**Client → Server:**
- `{ "type": "config", "persona_id": "...", "voice_id": "..." }` (first message)
- `{ "type": "audio", "data": "<base64 PCM16 16kHz>" }`
- `{ "type": "video", "data": "<base64 JPEG>" }`
- `{ "type": "text", "content": "..." }`
- `{ "type": "end" }`

**Server → Client:**
- `{ "type": "status", "message": "..." }`
- `{ "type": "audio", "data": "<base64 PCM16 24kHz>" }`
- `{ "type": "transcription", "role": "user|assistant", "text": "..." }`
- `{ "type": "turn_complete" }`
- `{ "type": "interrupted" }`
- `{ "type": "error", "message": "..." }`

## What's Implemented - April 16, 2026

### v2 Rebuild (Direct Gemini Live)
- [x] FastAPI backend with Gemini Live API WebSocket proxy
- [x] Browser mic capture (PCM16 16kHz) via ScriptProcessor
- [x] Audio playback (PCM16 24kHz) via AudioContext
- [x] Real-time transcriptions (input + output)
- [x] Turn detection + interruption handling
- [x] Dark theme UI (cinematic sentinel design)
- [x] Persona and voice selection
- [x] Session lifecycle management

### v1 (Removed)
- ~~LiveKit server~~ (removed - WebRTC not needed)
- ~~Agent worker~~ (removed - Gemini called directly)
- ~~WebSocket/HTTP proxy for LiveKit~~ (removed)

## Backlog

### P1 - UI Enhancements
- Visual state indicators (Listening/Thinking/Speaking animations)
- Camera/Vision toggle for repair inspection

### P2 - Step 2 Features
- Camera frame streaming for visual diagnosis
- Manual lookup tool integration
- Warnings/repair observations panel
- Repair-step panel
- Session history/persistence
