import ActivityKit
import SwiftUI
import WidgetKit

struct AgentLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            LockScreenActivityView(context: context)
                .activityBackgroundTint(Color(red: 0.06, green: 0.06, blue: 0.07))
                .activitySystemActionForegroundColor(Color(red: 0.95, green: 0.32, blue: 0.22))
                .widgetURL(URL(string: "visionvoiceagent://agent/\(context.attributes.sessionID)"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label(context.state.phase, systemImage: "waveform.circle.fill")
                        .font(.caption.weight(.semibold))
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.attributes.voiceName)
                        .font(.caption.weight(.medium))
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(context.state.status)
                            .font(.caption)
                            .lineLimit(1)
                        if !context.state.transcriptTail.isEmpty {
                            Text(context.state.transcriptTail)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                }
            } compactLeading: {
                Image(systemName: "waveform")
                    .foregroundStyle(Color(red: 0.95, green: 0.32, blue: 0.22))
            } compactTrailing: {
                Text(shortPhase(context.state.phase))
                    .font(.caption2.weight(.bold))
            } minimal: {
                Image(systemName: "mic.fill")
                    .foregroundStyle(Color(red: 0.95, green: 0.32, blue: 0.22))
            }
            .widgetURL(URL(string: "visionvoiceagent://agent/\(context.attributes.sessionID)"))
        }
    }

    private func shortPhase(_ phase: String) -> String {
        switch phase {
        case "Listening":
            "L"
        case "Speaking":
            "S"
        case "Connecting":
            "C"
        default:
            "A"
        }
    }
}

private struct LockScreenActivityView: View {
    let context: ActivityViewContext<AgentActivityAttributes>

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "waveform.circle.fill")
                .font(.title2)
                .foregroundStyle(Color(red: 0.95, green: 0.32, blue: 0.22))

            VStack(alignment: .leading, spacing: 4) {
                Text("Zeno AI")
                    .font(.headline)
                Text(context.state.status)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if !context.state.transcriptTail.isEmpty {
                    Text(context.state.transcriptTail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer()

            Text(context.state.phase)
                .font(.caption.weight(.semibold))
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(Color.white.opacity(0.10), in: Capsule())
        }
        .padding(.vertical, 4)
    }
}
