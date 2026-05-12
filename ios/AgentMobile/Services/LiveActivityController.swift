import ActivityKit
import Foundation

@MainActor
final class LiveActivityController {
    private var currentActivity: Activity<AgentActivityAttributes>?

    var isRunning: Bool {
        currentActivity != nil || !Activity<AgentActivityAttributes>.activities.isEmpty
    }

    func startOrUpdate(sessionID: String, phase: AgentPhase, status: String, transcriptTail: String) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }

        let state = AgentActivityAttributes.ContentState(
            phase: phase.rawValue,
            status: status,
            startedAt: Date(),
            transcriptTail: transcriptTail
        )

        if let activity = currentActivity ?? Activity<AgentActivityAttributes>.activities.first(where: { $0.attributes.sessionID == sessionID }) {
            currentActivity = activity
            await activity.update(ActivityContent(state: state, staleDate: nil))
            return
        }

        let attributes = AgentActivityAttributes(
            sessionID: sessionID,
            personaName: AgentDefaults.defaultPersonaName,
            voiceName: AgentDefaults.defaultVoiceID
        )

        do {
            currentActivity = try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil
            )
        } catch {
            currentActivity = nil
        }
    }

    func end(status: String = "Session ended") async {
        let activities = Activity<AgentActivityAttributes>.activities
        let state = AgentActivityAttributes.ContentState(
            phase: AgentPhase.idle.rawValue,
            status: status,
            startedAt: Date(),
            transcriptTail: ""
        )

        for activity in activities {
            await activity.end(ActivityContent(state: state, staleDate: nil), dismissalPolicy: .immediate)
        }
        currentActivity = nil
    }
}
