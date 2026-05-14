# Zeno AI iOS

SwiftUI iOS client for the existing Zeno AI backend.

## What it adds

- A native iOS app that starts the agent with the default persona `calm-expert` and voice `Puck`.
- A `Start Zeno AI Agent` App Shortcut that opens the app directly into a running agent session. Assign it to iOS Back Tap from Settings > Accessibility > Touch > Back Tap.
- ActivityKit Live Activity support for the Lock Screen and Dynamic Island, with taps deep-linking back to the active agent session.

## Run locally

1. Use the hosted backend at `https://16-16-11-190.sslip.io`, or start the existing backend on the Mac at `http://127.0.0.1:8001`.
2. Open `ios/VisionVoiceAgent.xcodeproj`.
3. Run the `AgentMobile` scheme on an iOS simulator.

For a physical iPhone, the default is the hosted backend. You can override it from the gear button if you want to point at a local Mac or a different deployed backend.
