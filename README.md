# Zeno AI

Zeno AI is a live voice-and-vision repair assistant. It uses the camera as the source of truth, talks back in realtime, can look up internal repair manuals, and keeps the web and iOS clients pointed at the same backend.

## Live Links

- Web app / direct app link: https://vision-voice-agent.netlify.app/
- Legacy app route: https://vision-voice-agent.netlify.app/repair redirects to `/`
- Hosted backend: https://16-16-11-190.sslip.io
- Backend health check: https://16-16-11-190.sslip.io/api/health
- GitHub repo: https://github.com/uphomesco-hub/vision-voice-agent
- iOS branch: https://github.com/uphomesco-hub/vision-voice-agent/tree/ios-agent-app

The current web root is the app itself, not a marketing landing page. It opens the Zeno AI session UI directly.

## What Works Now

- Realtime voice session over WebSocket using Gemini Live.
- Live camera frames from web and iOS.
- Mobile web UI with camera, mic, transcript, tool/manual status, and camera flipping.
- Native SwiftUI iOS app on the `ios-agent-app` branch.
- iOS App Shortcut: "Start Zeno AI Agent".
- iOS Back Tap flow through Shortcuts.
- iOS Live Activities for Lock Screen and Dynamic Island.
- Default iOS startup skips persona/voice selection and starts `calm-expert` with voice `Puck`.
- Manual lookup for known devices, with backend safeguards so generic words like "trimmer" are not treated as visual proof.
- Anti-hallucination camera handling: if no usable frame exists, Zeno should say it cannot see anything and ask for camera access or a better view.
- English-only responses/transcripts from the app layer.

## Repo Layout

```text
backend/   FastAPI backend, Gemini Live websocket bridge, prompts, manuals, session store
frontend/  React web app deployed to Netlify
ios/       SwiftUI iOS app, App Intent shortcut, Live Activity extension
deploy/    systemd service files for the backend host
docs/      AWS/Lightsail backend deployment notes
```

## Required Secrets

Do not commit secrets. Keep them in backend environment variables or host/dashboard settings.

Backend:

```bash
GOOGLE_API_KEY=your_gemini_key
CORS_ORIGINS=*
PORT=8001
```

Frontend:

```bash
REACT_APP_BACKEND_URL=https://16-16-11-190.sslip.io
```

The frontend and iOS app may expose the backend URL. The Gemini key must stay only on the backend.

## Run Backend Locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# create backend/.env with GOOGLE_API_KEY and any deploy-specific settings
python server.py
```

The backend runs on:

```text
http://127.0.0.1:8001
```

Check it:

```bash
curl http://127.0.0.1:8001/api/health
```

## Run Web App Locally

```bash
cd frontend
yarn install
REACT_APP_BACKEND_URL=http://127.0.0.1:8001 yarn start
```

Open:

```text
http://localhost:3000
```

For hosted backend testing:

```bash
REACT_APP_BACKEND_URL=https://16-16-11-190.sslip.io yarn start
```

Build:

```bash
cd frontend
CI= yarn build
```

## Run iOS App

1. Open `ios/VisionVoiceAgent.xcodeproj` in Xcode.
2. Select the `AgentMobile` scheme.
3. Configure your Apple Development Team for signing.
4. Run on an iPhone or simulator.

The iOS app defaults to:

```text
https://16-16-11-190.sslip.io
```

You can change the backend from the gear/settings button in the app.

### iOS Shortcut and Back Tap

The app exposes a `Start Zeno AI Agent` App Shortcut. To use Back Tap:

1. Open iPhone Settings.
2. Go to Accessibility > Touch > Back Tap.
3. Choose Double Tap or Triple Tap.
4. Select the Zeno AI shortcut.

Siri phrase:

```text
Hey Siri, open my agent in Zeno AI
```

iOS deep link:

```text
visionvoiceagent://agent
```

## Deploy Frontend to Netlify

Netlify build settings:

```text
Base directory: frontend
Build command: CI= yarn build
Publish directory: frontend/build
```

Required Netlify environment variable:

```text
REACT_APP_BACKEND_URL=https://16-16-11-190.sslip.io
```

Current production frontend:

```text
https://vision-voice-agent.netlify.app/
```

## Deploy Backend to EC2

On the EC2 instance:

```bash
cd /opt/repair-assistant
git fetch origin main
git checkout main
git pull --ff-only origin main
git rev-parse --short HEAD
cd backend
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart repair-assistant-backend
sudo systemctl status repair-assistant-backend
curl http://127.0.0.1:8001/api/health
```

If deploying the iOS branch backend changes before merging to `main`, replace `main` with `ios-agent-app`.

Useful service files and notes:

- `deploy/lightsail/repair-assistant-backend.service`
- `docs/aws-lightsail-backend.md`

## API Endpoints

```text
GET  /api/health
GET  /api/personas
GET  /api/voices
GET  /api/sessions/{session_id}/state
POST /api/sessions
POST /api/sessions/{session_id}/end
WS   /api/ws/session
```

## Branches

Current GitHub branches:

- `main`
- `ios-agent-app`
- `local-model-stream-prd`
- `testing-hud`

## Notes

- Pushing backend code to GitHub does not update AWS by itself. The EC2 service must pull the commit and restart.
- Pushing frontend code to `main` triggers Netlify if the site is connected to that branch.
- Do not push `.env`, SSH keys, Gemini keys, AWS keys, local Xcode `xcuserdata`, or backend database files.
