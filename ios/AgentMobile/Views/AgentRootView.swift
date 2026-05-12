import SwiftUI

struct AgentRootView: View {
    @Bindable var client: AgentSessionClient
    @State private var showingSettings = false

    var body: some View {
        NavigationStack {
            ZStack {
                LinearGradient(
                    colors: [Color(red: 0.05, green: 0.06, blue: 0.07), Color(red: 0.01, green: 0.01, blue: 0.012)],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .ignoresSafeArea()

                VStack(spacing: 24) {
                    header
                    statusPanel
                    transcriptList
                    controls
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 16)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $showingSettings) {
                SettingsView(client: client)
                    .presentationDetents([.medium])
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Vision Voice")
                        .font(.largeTitle.weight(.semibold))
                        .foregroundStyle(.white)
                    Text("Default agent: Marcus + Puck")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.65))
                }
                Spacer()
                phaseBadge
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var phaseBadge: some View {
        Label(client.phase.rawValue, systemImage: phaseIcon)
            .font(.caption.weight(.semibold))
            .foregroundStyle(phaseColor)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(phaseColor.opacity(0.14), in: Capsule())
            .accessibilityLabel("Agent status \(client.phase.rawValue)")
    }

    private var statusPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: "waveform.circle.fill")
                    .font(.system(size: 42))
                    .foregroundStyle(phaseColor)
                VStack(alignment: .leading, spacing: 4) {
                    Text(client.statusText)
                        .font(.headline)
                        .foregroundStyle(.white)
                    Text(client.sessionID ?? "No active session")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.55))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer()
            }

            if let error = client.errorMessage {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(Color(red: 1.0, green: 0.45, blue: 0.38))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.white.opacity(0.10), lineWidth: 1)
        )
    }

    private var transcriptList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if client.transcript.isEmpty {
                        ContentUnavailableView {
                            Label("Agent idle", systemImage: "mic.circle")
                        } description: {
                            Text("Start from the button here, the app shortcut, or Back Tap.")
                        }
                        .foregroundStyle(.white.opacity(0.7))
                        .padding(.top, 36)
                    }

                    ForEach(client.transcript) { line in
                        TranscriptRow(line: line)
                            .id(line.id)
                    }
                }
                .padding(.vertical, 8)
            }
            .onChange(of: client.transcript.count) { _, _ in
                guard let last = client.transcript.last else { return }
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button {
                Task {
                    await client.startDefaultAgent()
                }
            } label: {
                Label("Start Agent", systemImage: "bolt.circle.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.95, green: 0.32, blue: 0.22))
            .disabled(client.isRunning)

            Button(role: .destructive) {
                Task {
                    await client.stop()
                }
            } label: {
                Image(systemName: "phone.down.fill")
                    .frame(width: 48, height: 36)
            }
            .buttonStyle(.bordered)
            .disabled(!client.isRunning)
            .accessibilityLabel("End agent session")
        }
    }

    private var phaseIcon: String {
        switch client.phase {
        case .idle:
            "circle"
        case .connecting:
            "dot.radiowaves.left.and.right"
        case .listening:
            "mic.fill"
        case .speaking:
            "speaker.wave.2.fill"
        case .thinking:
            "sparkles"
        case .error:
            "exclamationmark.triangle.fill"
        }
    }

    private var phaseColor: Color {
        switch client.phase {
        case .idle:
            .white.opacity(0.7)
        case .connecting, .thinking:
            Color(red: 1.0, green: 0.75, blue: 0.38)
        case .listening:
            Color(red: 0.3, green: 0.86, blue: 0.55)
        case .speaking:
            Color(red: 0.35, green: 0.68, blue: 1.0)
        case .error:
            Color(red: 1.0, green: 0.45, blue: 0.38)
        }
    }
}

private struct TranscriptRow: View {
    let line: AgentTranscriptLine

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption.weight(.bold))
                .foregroundStyle(.white.opacity(0.52))
            Text(line.text)
                .font(.body)
                .foregroundStyle(.white.opacity(0.9))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var label: String {
        switch line.role {
        case "user":
            "YOU"
        case "system":
            "SYSTEM"
        default:
            "AGENT"
        }
    }

    private var background: Color {
        switch line.role {
        case "user":
            Color.white.opacity(0.09)
        case "system":
            Color(red: 0.95, green: 0.32, blue: 0.22).opacity(0.15)
        default:
            Color.white.opacity(0.055)
        }
    }
}

#Preview {
    AgentRootView(client: AgentSessionClient())
}
