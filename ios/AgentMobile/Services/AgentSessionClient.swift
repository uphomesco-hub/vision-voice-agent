import AVFoundation
import Foundation
import Observation

@Observable
@MainActor
final class AgentSessionClient {
    var backendBaseURL: String {
        didSet {
            defaults.set(backendBaseURL, forKey: AgentDefaults.backendBaseURLKey)
        }
    }
    var phase: AgentPhase = .idle
    var statusText = "Ready"
    var sessionID: String?
    var transcript: [AgentTranscriptLine] = []
    var isRunning = false
    var errorMessage: String?
    var backTapSetupMarked: Bool {
        didSet {
            defaults.set(backTapSetupMarked, forKey: AgentDefaults.backTapSetupMarkedKey)
        }
    }
    var shortcutSetupMarked: Bool {
        didSet {
            defaults.set(shortcutSetupMarked, forKey: AgentDefaults.shortcutSetupMarkedKey)
        }
    }
    var shortcutLaunchObserved: Bool {
        didSet {
            defaults.set(shortcutLaunchObserved, forKey: AgentDefaults.shortcutLaunchObservedKey)
        }
    }

    private let defaults = UserDefaults(suiteName: AgentDefaults.appGroup) ?? .standard
    private let liveActivity = LiveActivityController()
    private var webSocketTask: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private let audioEngine = AVAudioEngine()
    private let playbackEngine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var playbackConfigured = false

    init() {
        backendBaseURL = defaults.string(forKey: AgentDefaults.backendBaseURLKey) ?? AgentDefaults.defaultBackendBaseURL
        backTapSetupMarked = defaults.bool(forKey: AgentDefaults.backTapSetupMarkedKey)
        shortcutSetupMarked = defaults.bool(forKey: AgentDefaults.shortcutSetupMarkedKey)
        shortcutLaunchObserved = defaults.bool(forKey: AgentDefaults.shortcutLaunchObservedKey)
    }

    func startDefaultAgent(trigger: AgentStartTrigger = .manual) async {
        if trigger == .shortcut {
            shortcutLaunchObserved = true
            shortcutSetupMarked = true
        }

        if isRunning {
            await updateLiveActivity(status: "Agent already running")
            return
        }

        errorMessage = nil
        transcript.removeAll()
        setPhase(.connecting, status: "Starting default agent...")
        isRunning = true

        do {
            try await connectSocket()
            try await startMicrophone()
            appendSystemLine(for: trigger)
            setPhase(.listening, status: "Listening...")
            await updateLiveActivity(status: "Listening through \(AgentDefaults.defaultPersonaName)")
        } catch {
            errorMessage = error.localizedDescription
            setPhase(.error, status: error.localizedDescription)
            await stop()
        }
    }

    func stop() async {
        receiveTask?.cancel()
        receiveTask = nil
        heartbeatTask?.cancel()
        heartbeatTask = nil

        if let webSocketTask {
            try? await sendJSON(["type": "end"])
            webSocketTask.cancel(with: .normalClosure, reason: nil)
        }
        webSocketTask = nil

        stopMicrophone()
        stopPlayback()
        isRunning = false
        sessionID = nil
        setPhase(.idle, status: "Ready")
        await liveActivity.end()
    }

    private func connectSocket() async throws {
        guard let wsURL = websocketURL() else {
            throw URLError(.badURL)
        }

        let task = URLSession.shared.webSocketTask(with: wsURL)
        webSocketTask = task
        task.resume()

        try await sendJSON([
            "type": "config",
            "persona_id": AgentDefaults.defaultPersonaID,
            "voice_id": AgentDefaults.defaultVoiceID
        ])

        receiveTask = Task { [weak self] in
            await self?.receiveMessages()
        }

        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                try? await self?.sendJSON(["type": "heartbeat"])
            }
        }
    }

    private func websocketURL() -> URL? {
        var trimmed = backendBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while trimmed.hasSuffix("/") {
            trimmed.removeLast()
        }
        guard var components = URLComponents(string: trimmed) else { return nil }
        if components.scheme == "https" {
            components.scheme = "wss"
        } else {
            components.scheme = "ws"
        }
        components.path = "/api/ws/session"
        return components.url
    }

    private func receiveMessages() async {
        guard let webSocketTask else { return }

        do {
            while !Task.isCancelled {
                let message = try await webSocketTask.receive()
                switch message {
                case .string(let text):
                    handleServerText(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        handleServerText(text)
                    }
                @unknown default:
                    break
                }
            }
        } catch {
            guard isRunning else { return }
            errorMessage = error.localizedDescription
            setPhase(.error, status: "Connection lost")
            await liveActivity.end(status: "Connection lost")
        }
    }

    private func handleServerText(_ text: String) {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else {
            return
        }

        switch type {
        case "session.ready":
            sessionID = object["session_id"] as? String
            Task {
                await updateLiveActivity(status: "Agent connected")
            }
        case "assistant.state":
            let state = object["state"] as? String
            if state == "listening" {
                setPhase(.listening, status: "Listening...")
            } else if state == "speaking" {
                setPhase(.speaking, status: "Speaking...")
            } else if state == "connecting" {
                setPhase(.connecting, status: "Connecting...")
            } else {
                setPhase(.thinking, status: "Thinking...")
            }
            Task {
                await updateLiveActivity(status: statusText)
            }
        case "status":
            if let message = object["message"] as? String {
                statusText = message
                Task {
                    await updateLiveActivity(status: message)
                }
            }
        case "transcription":
            let role = object["role"] as? String ?? "assistant"
            let lineText = object["text"] as? String ?? ""
            guard !lineText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            transcript.append(AgentTranscriptLine(role: role, text: lineText, createdAt: Date()))
            trimTranscript()
            Task {
                await updateLiveActivity(status: statusText)
            }
        case "audio":
            if let encoded = object["data"] as? String {
                playAudio(base64: encoded)
            }
        case "error":
            let message = object["message"] as? String ?? "Unknown error"
            errorMessage = message
            setPhase(.error, status: message)
            Task {
                await updateLiveActivity(status: message)
            }
        default:
            break
        }
    }

    private func startMicrophone() async throws {
        let granted = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
        guard granted else {
            throw NSError(domain: "AgentSessionClient", code: 1, userInfo: [NSLocalizedDescriptionKey: "Microphone permission is required"])
        }

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let pcmData = Self.makePCM16MonoData(buffer: buffer, sourceFormat: format, targetSampleRate: 16_000)
            guard !pcmData.isEmpty else { return }
            Task {
                try? await self.sendJSON(["type": "audio", "data": pcmData.base64EncodedString()])
            }
        }

        audioEngine.prepare()
        try audioEngine.start()
    }

    private func stopMicrophone() {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func playAudio(base64: String) {
        guard let data = Data(base64Encoded: base64), !data.isEmpty else { return }
        configurePlaybackIfNeeded()

        let sampleCount = data.count / MemoryLayout<Int16>.size
        guard let format = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(sampleCount)) else {
            return
        }

        buffer.frameLength = AVAudioFrameCount(sampleCount)
        data.withUnsafeBytes { rawBuffer in
            guard let source = rawBuffer.bindMemory(to: Int16.self).baseAddress,
                  let destination = buffer.floatChannelData?[0] else {
                return
            }
            for index in 0..<sampleCount {
                destination[index] = Float(source[index]) / Float(Int16.max)
            }
        }

        playerNode.scheduleBuffer(buffer)
        if !playerNode.isPlaying {
            playerNode.play()
        }
    }

    private func configurePlaybackIfNeeded() {
        guard !playbackConfigured else { return }
        playbackEngine.attach(playerNode)
        let format = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)
        playbackEngine.connect(playerNode, to: playbackEngine.mainMixerNode, format: format)
        playbackEngine.prepare()
        try? playbackEngine.start()
        playbackConfigured = true
    }

    private func stopPlayback() {
        playerNode.stop()
        playbackEngine.stop()
        playbackConfigured = false
    }

    private func sendJSON(_ object: [String: Any]) async throws {
        guard let webSocketTask else { return }
        let data = try JSONSerialization.data(withJSONObject: object)
        guard let text = String(data: data, encoding: .utf8) else { return }
        try await webSocketTask.send(.string(text))
    }

    private func setPhase(_ phase: AgentPhase, status: String) {
        self.phase = phase
        statusText = status
    }

    private func appendSystemLine(for trigger: AgentStartTrigger) {
        let message: String
        switch trigger {
        case .manual:
            message = "Default agent session started."
        case .shortcut:
            message = "Started from Back Tap or Shortcut."
        case .deepLink:
            message = "Resumed from Live Activity."
        }
        transcript.append(AgentTranscriptLine(role: "system", text: message, createdAt: Date()))
    }

    private func trimTranscript() {
        if transcript.count > 80 {
            transcript.removeFirst(transcript.count - 80)
        }
    }

    private func updateLiveActivity(status: String) async {
        guard let sessionID else { return }
        await liveActivity.startOrUpdate(
            sessionID: sessionID,
            phase: phase,
            status: status,
            transcriptTail: transcript.last?.text ?? ""
        )
    }

    nonisolated private static func makePCM16MonoData(buffer: AVAudioPCMBuffer, sourceFormat: AVAudioFormat, targetSampleRate: Double) -> Data {
        guard let channel = buffer.floatChannelData?[0] else { return Data() }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return Data() }

        let ratio = max(sourceFormat.sampleRate / targetSampleRate, 1)
        let outputCount = max(Int(Double(frameCount) / ratio), 1)
        var samples = [Int16]()
        samples.reserveCapacity(outputCount)

        for outputIndex in 0..<outputCount {
            let sourceIndex = min(Int(Double(outputIndex) * ratio), frameCount - 1)
            let clamped = max(-1, min(1, channel[sourceIndex]))
            samples.append(Int16(clamped * Float(Int16.max)))
        }

        return samples.withUnsafeBufferPointer { pointer in
            Data(buffer: pointer)
        }
    }
}
