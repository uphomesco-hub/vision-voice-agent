import SwiftUI

struct AgentRootView: View {
    @Bindable var client: AgentSessionClient
    @State private var showingSettings = false
    @State private var showTranscript = true

    var body: some View {
        NavigationStack {
            ZStack {
                appBackground
                if client.isRunning {
                    activeSessionScreen
                } else {
                    idleScreen
                }
            }
            .sheet(isPresented: $showingSettings) {
                SettingsView(client: client)
                    .presentationDetents([.medium])
            }
            .toolbar(.hidden, for: .navigationBar)
        }
    }

    private var appBackground: some View {
        LinearGradient(
            colors: [Color(red: 0.05, green: 0.06, blue: 0.07), Color(red: 0.01, green: 0.01, blue: 0.012)],
            startPoint: .top,
            endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    private var idleScreen: some View {
        VStack(spacing: 16) {
            header
            statusPanel
            if !client.setupGuideComplete {
                setupGuide
            }
            transcriptList
            startControls
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 16)
    }

    private var activeSessionScreen: some View {
        ZStack {
            cameraSurface
                .ignoresSafeArea()

            activeTopBar

            if showTranscript {
                VStack {
                    Spacer()
                    transcriptOverlay
                        .padding(.horizontal, 12)
                        .padding(.bottom, 86)
                }
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            VStack {
                Spacer()
                activeControls
            }
        }
        .background(Color.black)
        .ignoresSafeArea()
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
                settingsButton
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var activeTopBar: some View {
        VStack {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Vision Voice")
                        .font(.headline.weight(.semibold))
                        .foregroundStyle(.white)
                    Text(client.statusText)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.62))
                        .lineLimit(1)
                }
                Spacer()
                phaseBadge
                settingsButton
            }
            .padding(.horizontal, 14)
            .padding(.top, 54)
            Spacer()
        }
    }

    private var settingsButton: some View {
        Button {
            showingSettings = true
        } label: {
            Image(systemName: "gearshape")
                .font(.system(size: 15, weight: .semibold))
                .frame(width: 34, height: 34)
                .background(Color.white.opacity(0.09), in: Circle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white.opacity(0.82))
        .accessibilityLabel("Settings")
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

    private var activeSessionLayout: some View {
        VStack(spacing: 0) {
            cameraSurface
            activeControls
        }
        .background(Color.black, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color.white.opacity(0.10), lineWidth: 1)
        )
    }

    private var transcriptList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
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

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Conversation")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.white.opacity(0.46))
                .textCase(.uppercase)
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 6)
            transcriptList
                .padding(.horizontal, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(red: 0.07, green: 0.07, blue: 0.075))
        .overlay(alignment: .top) {
            Rectangle()
                .fill(Color.white.opacity(0.10))
                .frame(height: 1)
        }
    }

    private var transcriptOverlay: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Conversation")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.white.opacity(0.46))
                    .textCase(.uppercase)
                Spacer()
                Button {
                    withAnimation(.easeInOut(duration: 0.18)) {
                        showTranscript = false
                    }
                } label: {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 11, weight: .bold))
                        .frame(width: 28, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.white.opacity(0.62))
                .accessibilityLabel("Hide transcript")
            }

            compactTranscriptList
        }
        .padding(.horizontal, 10)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity)
        .frame(maxHeight: 230)
        .background(.black.opacity(0.48), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(.white.opacity(0.10), lineWidth: 1)
        )
    }

    private var compactTranscriptList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(client.transcript.suffix(12)) { line in
                        TranscriptRow(line: line)
                            .id(line.id)
                    }
                }
                .padding(.bottom, 2)
            }
            .scrollIndicators(.hidden)
            .onChange(of: client.transcript.count) { _, _ in
                guard let last = client.transcript.last else { return }
                withAnimation(.easeOut(duration: 0.18)) {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
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
                    .scaleEffect(x: client.cameraFacingFront ? -1 : 1, y: 1)
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

            voiceAura

            VStack {
                Spacer()
                Text(voiceBadgeText)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.78))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .background(.black.opacity(0.62), in: Capsule())
                    .padding(.bottom, showTranscript ? 330 : 108)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    private var voiceAura: some View {
        ZStack {
            Circle()
                .stroke(auraColor.opacity(0.34), lineWidth: 1.5)
                .scaleEffect(client.phase == .speaking ? 1.35 : 1.18)
                .opacity(client.phase == .idle ? 0.18 : 0.55)
            Circle()
                .stroke(auraColor.opacity(0.22), lineWidth: 1.5)
                .scaleEffect(client.phase == .thinking ? 1.1 : 1.0)
            Image(systemName: client.microphoneRunning ? "waveform" : "mic.slash.fill")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(.white.opacity(0.86))
        }
        .frame(width: 58, height: 58)
        .background(.black.opacity(0.35), in: Circle())
        .allowsHitTesting(false)
        .animation(.easeInOut(duration: 0.45), value: client.phase)
    }

    private var activeControls: some View {
        HStack(spacing: 12) {
            ControlCircleButton(
                systemImage: "text.bubble.fill",
                isActive: showTranscript,
                accessibilityLabel: showTranscript ? "Hide transcript" : "Show transcript"
            ) {
                showTranscript.toggle()
            }

            Spacer(minLength: 0)

            HStack(spacing: 10) {
                ControlCircleButton(
                    systemImage: client.microphoneRunning ? "mic.fill" : "mic.slash.fill",
                    isActive: client.microphoneRunning,
                    accessibilityLabel: client.microphoneRunning ? "Turn microphone off" : "Turn microphone on"
                ) {
                    Task {
                        await client.toggleMicrophone()
                    }
                }

                ControlCircleButton(
                    systemImage: "xmark",
                    isActive: false,
                    isDestructive: true,
                    accessibilityLabel: "End agent session"
                ) {
                    Task {
                        await client.stop()
                    }
                }

                ControlCircleButton(
                    systemImage: client.cameraRunning ? "video.fill" : "video.slash.fill",
                    isActive: client.cameraRunning,
                    accessibilityLabel: client.cameraRunning ? "Turn camera off" : "Turn camera on"
                ) {
                    Task {
                        await client.toggleCamera()
                    }
                }

                ControlCircleButton(
                    systemImage: "arrow.triangle.2.circlepath.camera.fill",
                    isActive: false,
                    isDisabled: !client.cameraRunning,
                    accessibilityLabel: "Flip camera"
                ) {
                    Task {
                        await client.flipCamera()
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 30)
        .background(.black.opacity(0.58))
    }

    private var startControls: some View {
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

    private var voiceBadgeText: String {
        if !client.microphoneRunning {
            return "Mic off"
        }
        switch client.phase {
        case .idle:
            return "Idle"
        case .connecting:
            return "Connecting..."
        case .listening:
            return "Listening..."
        case .speaking:
            return "Speaking..."
        case .thinking:
            return "Thinking..."
        case .error:
            return "Needs attention"
        }
    }

    private var auraColor: Color {
        switch client.phase {
        case .speaking:
            Color(red: 0.35, green: 0.68, blue: 1.0)
        case .thinking:
            Color.white.opacity(0.45)
        case .listening:
            Color(red: 0.3, green: 0.86, blue: 0.55)
        case .connecting:
            Color(red: 1.0, green: 0.75, blue: 0.38)
        case .idle, .error:
            .white.opacity(0.5)
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
        inlineText
            .font(.system(size: 11))
            .lineSpacing(1)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
    }

    private var inlineText: Text {
        Text(label)
            .font(.system(size: 8, weight: .bold))
            .foregroundStyle(.white.opacity(0.54))
        + Text("  \(line.text)")
            .foregroundStyle(textColor)
    }

    private var label: String {
        switch line.role {
        case "user":
            "YOU"
        case "system":
            "SYS"
        default:
            "AI"
        }
    }

    private var background: Color {
        switch line.role {
        case "user":
            Color.white.opacity(0.10)
        case "system":
            Color.clear
        default:
            Color.white.opacity(0.06)
        }
    }

    private var textColor: Color {
        switch line.role {
        case "system":
            .white.opacity(0.48)
        case "assistant":
            .white.opacity(0.88)
        default:
            .white.opacity(0.74)
        }
    }
}

private struct ControlCircleButton: View {
    let systemImage: String
    let isActive: Bool
    var isDestructive = false
    var isDisabled = false
    let accessibilityLabel: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 18, weight: .semibold))
                .frame(width: 44, height: 44)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(foreground)
        .background(background, in: Circle())
        .overlay(
            Circle()
                .stroke(Color.white.opacity(isActive ? 0.0 : 0.08), lineWidth: 1)
        )
        .opacity(isDisabled ? 0.28 : 1)
        .disabled(isDisabled)
        .accessibilityLabel(accessibilityLabel)
    }

    private var foreground: Color {
        if isActive {
            return .black
        }
        if isDestructive {
            return Color(red: 1.0, green: 0.42, blue: 0.38)
        }
        return .white.opacity(0.78)
    }

    private var background: Color {
        if isActive {
            return .white
        }
        if isDestructive {
            return Color.white.opacity(0.12)
        }
        return Color.white.opacity(0.09)
    }
}

#Preview {
    AgentRootView(client: AgentSessionClient())
}
