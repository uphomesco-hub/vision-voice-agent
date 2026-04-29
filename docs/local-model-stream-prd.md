# PRD: Provider-Neutral Live Repair Stream

## Purpose

Build our own realtime repair-assistant stream so the product can run without depending only on Gemini Live. The browser should keep one stable websocket connection to our FastAPI backend, while the backend can route each session to Gemini Live, a cloud OpenAI-compatible model, or a local model stack.

This branch starts from the HUD work. The new stream must preserve manual lookup, HUD planning, verified markers, and local marker tracking.

## Problem

The current live path is tightly coupled to Gemini Live:

```text
Browser mic/camera
  -> FastAPI /api/ws/session
  -> Gemini BidiGenerateContent websocket
  -> Gemini audio, text, tool calls, vision
  -> Browser
```

Gemini Live is doing four jobs in one service:

- realtime audio input and turn detection
- conversation reasoning
- vision-frame understanding
- speech/audio output

Most local models do not expose the same all-in-one live API. To support local and multiple models, the backend needs to become a realtime orchestrator that composes these pieces from separate adapters.

## Goals

- Keep the existing browser websocket protocol mostly stable.
- Add a model/provider abstraction behind `/api/ws/session`.
- Support at least these backend modes:
  - `gemini_live`: existing Gemini Live path.
  - `local_composed`: local STT + local/cloud VLM/LLM + local/cloud TTS.
  - `openai_compatible`: any OpenAI-compatible text/vision endpoint for reasoning.
  - `ollama`: local Ollama vision/text models through HTTP.
- Preserve `lookup_manual`, HUD `highlight`, HUD planner, current-step logic, and marker tracking.
- Allow provider choice by env var first, then by session config later.
- Stream partial assistant text to the UI even when audio synthesis is slower.
- Fail gracefully when a provider lacks vision, tools, streaming, or audio.

## Non-Goals

- Do not rebuild the frontend media capture flow in the first implementation.
- Do not require local models to match Gemini Live latency on day one.
- Do not make local model setup automatic across all machines.
- Do not remove Gemini Live. It remains the best baseline and fallback.
- Do not push backend env files or provider keys.

## User Stories

- As a tester, I can set `ASSISTANT_PROVIDER=gemini_live` and get the current behavior.
- As a tester, I can set `ASSISTANT_PROVIDER=ollama` and ask text/vision questions using a local model.
- As a tester, I can show the camera to the app and have the local model reason over recent frames.
- As a tester, I can ask "mark this screw" and still get HUD markers through the same verified highlight flow.
- As a tester, if the selected provider cannot do speech output, I still see streamed text and can optionally use browser/system TTS later.
- As a developer, I can add another provider by implementing a small adapter contract, not by rewriting `server.py`.

## Proposed Architecture

```text
Browser
  - sends config, audio PCM chunks, JPEG frames, heartbeat
  - receives text, audio, transcript, tool status, HUD state

FastAPI /api/ws/session
  - keeps session lifecycle and DB state
  - buffers latest frames
  - collects user audio into utterances
  - routes model events through RealtimeSessionEngine

RealtimeSessionEngine
  - VoiceInputAdapter: audio -> user transcript
  - VisionContextBuffer: latest/relevant frames
  - ReasoningAdapter: transcript + frames + tools -> assistant response/tool calls
  - ToolRouter: lookup_manual, highlight, hud_planner
  - VoiceOutputAdapter: assistant text -> audio chunks

Providers
  - GeminiLiveProvider: current websocket bridge
  - LocalComposedProvider: Whisper/faster-whisper + Ollama/OpenAI-compatible + Piper/other TTS
  - OpenAICompatibleProvider: cloud or local HTTP API for text/vision reasoning
  - OllamaProvider: local HTTP API, image-capable models when available
```

## Provider Contract

Each provider should expose the same backend-facing shape:

```python
class RealtimeProvider:
    capabilities: ProviderCapabilities

    async def start(self, session: ProviderSessionConfig) -> None: ...
    async def receive_audio(self, pcm16_b64: str) -> None: ...
    async def receive_frame(self, jpeg_b64: str) -> None: ...
    async def receive_user_text(self, text: str) -> None: ...
    async def end_turn(self) -> None: ...
    async def close(self) -> None: ...
```

Provider output should be normalized into events:

```python
{"type": "user_transcript", "text": "...", "final": true}
{"type": "assistant_text", "text": "...", "final": false}
{"type": "assistant_audio", "data": "...pcm16 or wav chunk..."}
{"type": "tool_call", "name": "highlight", "args": {...}}
{"type": "tool_result", "name": "highlight", "result": {...}}
{"type": "turn_complete"}
{"type": "error", "message": "...", "recoverable": true}
```

## Capabilities

Providers must declare capabilities so the app can degrade cleanly:

```json
{
  "audio_input": "native_stream|stt_adapter|none",
  "audio_output": "native_stream|tts_adapter|none",
  "vision": "native_frames|image_prompt|none",
  "tool_calling": "native|json_instruction|none",
  "streaming_text": true,
  "local": true
}
```

Examples:

- Gemini Live: native audio input, native audio output, native frames, native tools.
- Ollama vision model: no native audio, image prompt vision, JSON-instruction tools, streaming text.
- OpenAI-compatible local server: depends on endpoint; most likely text/vision and streaming text, not native audio.

## Local Composed Mode

Local mode should be a composed pipeline:

```text
Browser audio chunks
  -> AudioUtteranceBuffer + VAD
  -> STT adapter
  -> user text
  -> Reasoning adapter with latest frame(s), manual context, HUD state
  -> tool router if needed
  -> assistant text stream
  -> optional TTS adapter
  -> browser audio chunks
```

Recommended first local mode:

- STT: start with text-only fallback, then add local Whisper/faster-whisper.
- Reasoning/Vision: Ollama adapter or OpenAI-compatible endpoint.
- TTS: optional first; use text response if missing.

This lets us validate the architecture before fighting local speech latency.

## Tool and HUD Behavior

The tool layer should stay provider-neutral.

- Providers do not directly mutate HUD state.
- Providers emit normalized `tool_call` events.
- Backend `ToolRouter` executes:
  - `lookup_manual`
  - `highlight`
  - `hud_planner`
- Existing verified marker placement remains in `backend/hud_runtime.py`.
- Existing local template tracking keeps moving markers between model checks.
- If a provider cannot do native function calling, the prompt must ask for strict JSON tool envelopes.

Tool-call envelope for non-native providers:

```json
{
  "tool_call": {
    "name": "highlight",
    "args": {
      "operation": "create",
      "feature_id": "runtime:auto",
      "target_hint": "left screw",
      "action_type": "inspect",
      "reason": "user_request"
    }
  }
}
```

## Frame Strategy

The frontend can keep sending frames every 2 seconds for now.

Backend should keep:

- latest frame
- last N frames for context, default `3`
- frame timestamps
- a low-cost thumbnail/signature for change detection

Reasoning calls should usually include:

- latest full JPEG
- optional prior frame if scene changed
- current manual step text
- current HUD marker summary
- last user utterance

Do not send every frame to slower local models. Local reasoning should happen on user turns, explicit visual questions, or meaningful frame-change nudges.

## Configuration

Add env-driven config:

```env
ASSISTANT_PROVIDER=gemini_live

# Existing
GOOGLE_API_KEY=

# Generic OpenAI-compatible
MODEL_API_BASE=http://localhost:11434/v1
MODEL_API_KEY=
MODEL_ID=
VISION_MODEL_ID=

# Ollama direct
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=
OLLAMA_VISION_MODEL=

# Local composed audio
STT_PROVIDER=none
STT_MODEL=
TTS_PROVIDER=none
TTS_VOICE=
```

The app should expose active provider/capabilities in `GET /api/health`.

## API Compatibility

Keep existing websocket messages:

Client to server:

- `config`
- `audio`
- `video`
- `heartbeat`
- `end`

Server to client:

- `session.ready`
- `session.state`
- `assistant.state`
- `transcription`
- `audio`
- `turn_complete`
- `tool.status`
- `hud.state`
- `vision.perception`
- `error`

Add optional server message:

```json
{
  "type": "provider.state",
  "provider": "ollama",
  "capabilities": {
    "audio_input": "none",
    "audio_output": "none",
    "vision": "image_prompt",
    "tool_calling": "json_instruction",
    "streaming_text": true,
    "local": true
  }
}
```

## Implementation Plan

### Phase 1: Extract the Current Gemini Path

- Create `backend/realtime/` package.
- Move Gemini websocket bridge logic out of `server.py` into `GeminiLiveProvider`.
- Keep behavior identical under `ASSISTANT_PROVIDER=gemini_live`.
- Add provider capability reporting.
- Tests: provider selection, health response, existing HUD runtime tests.

### Phase 2: Add Text/Vision Provider Mode

- Add `OpenAICompatibleProvider` with streaming text support.
- Add `OllamaProvider` for local HTTP calls.
- Add image prompt support using the latest frame.
- Add JSON-instruction tool parsing for providers without native tools.
- Text-only output is acceptable in this phase.

### Phase 3: Add Local Audio Composition

- Add utterance buffering and turn detection.
- Add STT adapter interface.
- Add TTS adapter interface.
- Add text-first fallback when STT/TTS are unavailable.
- Keep frontend protocol stable.

### Phase 4: Provider Selection UI

- Add small dev/test-only provider selector.
- Show active provider and degraded capabilities.
- Keep production default controlled by env.

### Phase 5: Hardening

- Add per-provider timeouts and cancellation.
- Add model call tracing without storing secrets.
- Add provider fallback rules.
- Add latency metrics.
- Add test fixtures for local/non-Gemini providers.

## Acceptance Criteria

- `ASSISTANT_PROVIDER=gemini_live` keeps current behavior.
- `ASSISTANT_PROVIDER=ollama` can answer a typed/user-transcribed visual question using the latest frame.
- A non-native tool provider can request `highlight` through JSON and the backend places a verified HUD marker.
- If local model cannot produce audio, the UI still receives streamed assistant text and does not hang.
- If a provider lacks vision, the assistant says it cannot see rather than hallucinating the scene.
- Provider capability state is visible in health/session state.
- No `.env`, API keys, local DB, or runtime artifacts are committed.

## Risks

- Local models may be too slow for natural voice unless we keep responses short and turn-based.
- Local vision models may be weaker at precise spatial grounding than Gemini; HUD verification and local template tracking remain important.
- JSON tool parsing can be brittle; it needs strict extraction and retry prompts.
- Audio output formats vary; browser playback may need a small normalization layer.
- Running STT, VLM, and TTS locally can be heavy on CPU/RAM.

## Open Questions

- Should local mode start as text+vision only, then add STT/TTS after the model router is stable?
- Should provider selection be hidden behind a dev flag or visible in the UI for branch testing?
- Which local default should we optimize first: Ollama direct API or OpenAI-compatible `/v1/chat/completions`?
- Do we want browser-side speech recognition/TTS as a fallback for local mode, or keep all AI media processing backend-side?
- How much session data should be stored for provider traces?

## Recommended First Build Slice

Start with a small vertical slice:

1. Add provider config and capability reporting.
2. Extract Gemini into `GeminiLiveProvider` without behavior changes.
3. Add `OllamaProvider` for text+latest-image responses.
4. Add JSON tool-call parsing so local model can call `highlight`.
5. Keep audio output optional; stream text first.

This gives us a real local/multi-model path while keeping the working Gemini/HUD branch intact.
