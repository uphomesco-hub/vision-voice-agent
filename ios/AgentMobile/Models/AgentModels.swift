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
    static let defaultPersonaID = "calm-expert"
    static let defaultPersonaName = "Marcus"
    static let defaultVoiceID = "Puck"
    static let defaultBackendBaseURL = "http://127.0.0.1:8001"
}
