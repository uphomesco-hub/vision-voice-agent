import Foundation

enum AgentPhase: String {
    case idle = "Idle"
    case connecting = "Connecting"
    case listening = "Listening"
    case speaking = "Speaking"
    case thinking = "Thinking"
    case error = "Needs attention"
}

enum AgentStartTrigger {
    case manual
    case shortcut
    case deepLink
}

struct AgentTranscriptLine: Identifiable, Hashable {
    let id = UUID()
    let role: String
    var text: String
    let createdAt: Date
}

struct AgentDefaults {
    static let appGroup = "group.co.uphomes.visionvoiceagent"
    static let pendingStartKey = "pending_default_agent_start"
    static let backendBaseURLKey = "backend_base_url"
    static let backTapSetupMarkedKey = "back_tap_setup_marked"
    static let shortcutSetupMarkedKey = "shortcut_setup_marked"
    static let shortcutLaunchObservedKey = "shortcut_launch_observed"
    static let defaultPersonaID = "calm-expert"
    static let defaultPersonaName = "Marcus"
    static let defaultVoiceID = "Puck"
    static let defaultBackendBaseURL = "https://16-16-11-190.sslip.io"
}
