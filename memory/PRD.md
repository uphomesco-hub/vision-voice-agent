# Voice-First Realtime Repair Assistant - PRD

## Problem Statement
Web-based voice-first repair assistant with voice + vision + manual lookup. User opens site, grants mic+camera, shows device, talks naturally. Assistant hears, sees, looks up manuals, guides troubleshooting step-by-step.

## Architecture
```
Browser (mic + camera + UI)
    ↕ WebSocket (JSON: audio/video/events)
FastAPI Backend (/api/ws/session)
    ↕ Raw WebSocket (Gemini BidiGenerateContent)
Gemini Live API (gemini-2.5-flash-native-audio-latest)
    → Tool: lookup_manual → SQLite
```

## Implemented (April 16, 2026)

### Step 1 — Voice
- [x] Voice conversation with Gemini Live API (raw WebSocket)
- [x] PCM16 16kHz audio in, 24kHz audio out
- [x] Auto-reconnect (60s K8s ingress limit)
- [x] Dark theme UI, persona/voice selection

### Step 2 Phase A — Camera + Vision
- [x] Live camera feed (640x480, JPEG frames every 2s)
- [x] Frames forwarded to Gemini realtimeInput
- [x] Voice aura animations (listening/speaking/thinking)

### Step 2 Phase B — Manuals + Tools
- [x] Expanded DB: sessions, turns, observations, tool_runs, manuals, snapshots
- [x] Manual repository with fuzzy search + ranking
- [x] 2 seed manuals (Stihl FS 56 RC trimmer, Dyson V15 vacuum)
- [x] lookup_manual tool via Gemini function calling
- [x] Tool activity panel in UI
- [x] Active manual, warnings, troubleshooting steps panels

### Step 2 Phase C — Nudge Engine
- [x] NudgeEngine with per-type cooldowns (safety_risk=10s, angle_hint=90s)
- [x] User speech grace window (5s)
- [x] Duplicate summary suppression
- [x] SceneState structured model with state diffing
- [x] VisionTracker for frame-by-frame analysis decisions

### Step 2 Phase D — UI Polish
- [x] Transcript drawer (toggle show/hide)
- [x] Tool spinner animation for running tools
- [x] Google Search queries panel
- [x] Vision status indicator
- [x] Session ID display in header
- [x] Calm state transitions

### Step 2 Phase E — Session Persistence + Reconnect
- [x] Reconnect-with-history (resume_session_id in config)
- [x] Previous conversation injected as system context on reconnect
- [x] Active manual context restored on reconnect
- [x] Session snapshots saved on disconnect
- [x] Comprehensive console logging (MIC, CAM, WS, SESSION, TOOL, SPEECH, etc.)

## Console Log Categories
`[INIT]` `[DATA]` `[SESSION]` `[WS]` `[MIC]` `[CAM]` `[STATE]` `[STATUS]` `[SPEECH]` `[TURN]` `[TOOL]` `[SEARCH]` `[MANUAL]` `[WARN]` `[ERROR]` `[CLEANUP]`

## Key Endpoints
- `GET /api/health` — version 2.0-step2
- `GET /api/personas` / `GET /api/voices`
- `POST /api/sessions` → {id, status}
- `GET /api/sessions/{id}/state` → full state
- `GET /api/sessions/{id}/logs` → turns + tool_runs
- `POST /api/sessions/{id}/end`
- `WS /api/ws/session` — realtime voice+vision

## Testing
- Iteration 4: 25/25 backend, 100% frontend
- Reconnect feature tested: resume, invalid, ended sessions

## Environment
- Google API Key: Required (GOOGLE_API_KEY in backend/.env)
- SQLite: Auto-created sessions.db
- Manuals: Auto-seeded from backend/manuals/*.json

## Backlog
- P2: Semantic vision ML-powered scene analysis (currently Gemini handles via frames)
- P2: Mobile responsive layout
- P3: Multi-user session support
