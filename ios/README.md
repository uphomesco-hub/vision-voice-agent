# Vision Voice iOS

SwiftUI iOS client for the existing Vision Voice backend.

## What it adds

- A native iOS app that starts the agent with the default persona `calm-expert` and voice `Puck`.
- A `Start Vision Voice Agent` App Shortcut that opens the app directly into a running agent session. Assign it to iOS Back Tap from Settings > Accessibility > Touch > Back Tap.
- ActivityKit Live Activity support for the Lock Screen and Dynamic Island, with taps deep-linking back to the active agent session.

## Run locally

1. Start the existing backend on the Mac at `http://127.0.0.1:8001`.
2. Open `ios/VisionVoiceAgent.xcodeproj`.
3. Run the `AgentMobile` scheme on an iOS simulator.

For a physical iPhone, set the backend URL in app settings to the Mac LAN address or deployed backend URL.
