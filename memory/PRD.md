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
- [x] Auto-reconnect (60s K8s limit) with history resume
- [x] Dark theme UI, persona/voice selection

### Step 2 — Vision + Manuals + Tools + Polish
- [x] Live camera feed (JPEG frames every 2s to Gemini)
- [x] **Welcome greeting** — assistant proactively greets on session start
- [x] **Proactive vision** — reacts to visual changes, tracks manual steps, warns about wrong tools
- [x] 3 seed manuals: Stihl FS 56 RC, Dyson V15 Detect, **Morphy Richards Trimmer** (rich format with repair playbooks, screws, hidden clips)
- [x] lookup_manual tool with Gemini function calling
- [x] NudgeEngine with per-type cooldowns (no spam)
- [x] UI panels: tool activity, manual, warnings, steps, transcript, search
- [x] Voice aura animations (listening/speaking/thinking)
- [x] **English transcription** enforced in system prompt
- [x] **Console logging** — all events: [WS] [MIC] [CAM] [SPEECH] [TOOL] [SEARCH] [SESSION] [STATE] [ERROR]
- [x] Session persistence + snapshots + reconnect with history

## Seed Manuals
1. `stihl-fs56rc` — Stihl FS 56 RC String Trimmer
2. `dyson-v15` — Dyson V15 Detect Cordless Vacuum
3. `morphy-richards-trimmer-generic-v2` — Morphy Richards Trimmer (with teardown, repair playbooks, hidden screws, clips)

## Key Endpoints
- `GET /api/health`, `GET /api/personas`, `GET /api/voices`
- `POST /api/sessions` → {id, status}
- `GET /api/sessions/{id}/state` → full state
- `GET /api/sessions/{id}/logs` → turns + tool_runs
- `POST /api/sessions/{id}/end`
- `WS /api/ws/session` — realtime voice+vision

## Environment
- Google API Key: GOOGLE_API_KEY in backend/.env
- SQLite: Auto-created sessions.db
- Manuals: Auto-seeded from backend/manuals/*.json

## Backlog
- P2: ML-powered scene analysis
- P2: Mobile responsive
- P3: Multi-user sessions
