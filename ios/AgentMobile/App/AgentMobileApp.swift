import SwiftUI

@main
struct AgentMobileApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @State private var sessionClient = AgentSessionClient()

    var body: some Scene {
        WindowGroup {
            AgentRootView(client: sessionClient)
                .onAppear {
                    consumePendingLaunchIfNeeded()
                }
                .onChange(of: scenePhase) { _, newPhase in
                    guard newPhase == .active else { return }
                    consumePendingLaunchIfNeeded()
                }
                .onOpenURL { url in
                    guard AgentLaunchStore.isAgentURL(url) else { return }
                    Task {
                        await sessionClient.startDefaultAgent(trigger: .deepLink)
                    }
                }
        }
    }

    private func consumePendingLaunchIfNeeded() {
        guard AgentLaunchStore.consumePendingStartRequest() else { return }
        Task {
            await sessionClient.startDefaultAgent(trigger: .shortcut)
        }
    }
}
