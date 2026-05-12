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
                    if client.isRunning {
                        cameraSurface
                    }
                    if !client.setupGuideComplete {
                        setupGuide
                    }
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

    private var setupGuide: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "hand.tap.fill")
                    .foregroundStyle(Color(red: 0.95, green: 0.32, blue: 0.22))
                Text("Quick setup")
                    .font(.headline)
                    .foregroundStyle(.white)
                Spacer()
            }

            SetupStepRow(
                title: "Back Tap",
                detail: "Settings > Accessibility > Touch > Back Tap > Double Tap > Open My Agent.",
                isDone: client.backTapSetupMarked,
                doneLabel: "Mark Done"
            ) {
                client.backTapSetupMarked.toggle()
            }

            Divider()
                .overlay(Color.white.opacity(0.12))

            SetupStepRow(
                title: "Siri",
                detail: "Say: \"Hey Siri, open my agent in Vision Voice.\"",
                isDone: client.shortcutSetupMarked || client.shortcutLaunchObserved,
                doneLabel: client.shortcutLaunchObserved ? "Verified" : "Mark Done"
            ) {
                client.shortcutSetupMarked.toggle()
            }

            Text("iOS does not let apps verify the Back Tap setting directly. Siri is marked verified after it opens this shortcut once.")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.52))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .background(Color.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.white.opacity(0.10), lineWidth: 1)
        )
    }

    private var cameraSurface: some View {
        ZStack {
            if client.cameraRunning {
                CameraPreviewView(session: client.cameraSession)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "video.slash.fill")
                        .font(.title2)
                        .foregroundStyle(.white.opacity(0.5))
                    Text("I can't see anything right now.")
                        .font(.headline)
                        .foregroundStyle(.white)
                    Text(client.cameraError ?? "Turn on camera access in iPhone Settings for Vision Voice.")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.62))
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        Task {
                            await client.retryCamera()
                        }
                    } label: {
                        Label("Retry Camera", systemImage: "arrow.clockwise")
                    }
                    .font(.caption.weight(.semibold))
                    .buttonStyle(.bordered)
                    .tint(Color(red: 0.95, green: 0.32, blue: 0.22))
                }
                .padding(20)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black)
            }

            VStack {
                HStack {
                    Spacer()
                    Label(client.cameraRunning ? "Camera on" : "Camera off", systemImage: client.cameraRunning ? "video.fill" : "video.slash.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.black.opacity(0.55), in: Capsule())
                }
                Spacer()
            }
            .padding(10)
        }
        .frame(height: client.setupGuideComplete ? 330 : 210)
        .background(Color.black, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.white.opacity(0.10), lineWidth: 1)
        )
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

private struct SetupStepRow: View {
    let title: String
    let detail: String
    let isDone: Bool
    let doneLabel: String
    let onToggleDone: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: isDone ? "checkmark.circle.fill" : "circle")
                .font(.title3)
                .foregroundStyle(isDone ? Color(red: 0.3, green: 0.86, blue: 0.55) : .white.opacity(0.45))
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.62))
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)

            Button(doneLabel) {
                onToggleDone()
            }
            .font(.caption.weight(.semibold))
            .buttonStyle(.bordered)
            .tint(isDone ? Color(red: 0.3, green: 0.86, blue: 0.55) : Color(red: 0.95, green: 0.32, blue: 0.22))
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
