import AppIntents
import Foundation

struct StartDefaultAgentIntent: AppIntent {
    static var title: LocalizedStringResource = "Start Vision Voice Agent"
    static var description = IntentDescription("Starts the default live agent session without asking for persona or voice.")
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        AgentLaunchStore.requestDefaultAgentStart()
        return .result()
    }
}

struct AgentAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartDefaultAgentIntent(),
            phrases: [
                "Start \(.applicationName)",
                "Open the agent in \(.applicationName)",
                "Run Vision Voice in \(.applicationName)"
            ],
            shortTitle: "Start Agent",
            systemImageName: "waveform.circle.fill"
        )
    }
}
