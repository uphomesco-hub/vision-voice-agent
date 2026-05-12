import SwiftUI

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @Bindable var client: AgentSessionClient

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("Backend URL", text: $client.backendBaseURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                    Text("Simulator default: \(AgentDefaults.defaultBackendBaseURL). Use your LAN or deployed backend URL on a physical iPhone.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Default Launch") {
                    LabeledContent("Persona", value: AgentDefaults.defaultPersonaName)
                    LabeledContent("Voice", value: AgentDefaults.defaultVoiceID)
                    LabeledContent("Shortcut", value: "Start Vision Voice Agent")
                }
            }
            .navigationTitle("Agent Settings")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}
