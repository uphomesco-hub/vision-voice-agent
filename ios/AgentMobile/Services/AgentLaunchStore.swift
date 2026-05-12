import Foundation

enum AgentLaunchStore {
    private static var defaults: UserDefaults {
        UserDefaults(suiteName: AgentDefaults.appGroup) ?? .standard
    }

    static func requestDefaultAgentStart() {
        defaults.set(true, forKey: AgentDefaults.pendingStartKey)
    }

    static func consumePendingStartRequest() -> Bool {
        let shouldStart = defaults.bool(forKey: AgentDefaults.pendingStartKey)
        defaults.set(false, forKey: AgentDefaults.pendingStartKey)
        return shouldStart
    }

    static func isAgentURL(_ url: URL) -> Bool {
        url.scheme == "visionvoiceagent" && (url.host == "agent" || url.host == "start")
    }
}
