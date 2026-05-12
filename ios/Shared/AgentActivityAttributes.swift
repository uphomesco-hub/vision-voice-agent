import ActivityKit
import Foundation

struct AgentActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var phase: String
        var status: String
        var startedAt: Date
        var transcriptTail: String
    }

    var sessionID: String
    var personaName: String
    var voiceName: String
}
