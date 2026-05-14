import AVFoundation
import Foundation
import Observation
import UIKit

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
    var cameraRunning = false
    var cameraError: String?
    var microphoneRunning = false
    var microphoneError: String?
    var cameraFacingFront = false
    var setupGuideComplete: Bool {
        backTapSetupMarked && (shortcutSetupMarked || shortcutLaunchObserved)
    }
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
    let cameraSession = AVCaptureSession()
    @ObservationIgnored private lazy var cameraStreamer = CameraFrameStreamer(session: cameraSession) { [weak self] jpegData in
        Task { @MainActor [weak self] in
            guard let self, self.webSocketTask != nil else { return }
            try? await self.sendJSON(["type": "video", "data": jpegData.base64EncodedString()])
        }
    } onStatus: { [weak self] running, message in
        Task { @MainActor [weak self] in
            self?.cameraRunning = running
            self?.cameraError = message
        }
    }
    private var webSocketTask: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private let audioEngine = AVAudioEngine()
    private let playbackEngine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var playbackConfigured = false
    private var audioSessionConfigured = false
    private var assistantAudioActive = false
    private var pendingPlaybackBuffers = 0
    private var suppressMicrophoneUntil = Date.distantPast
    private var bargeInCandidateFrames = 0
    private var dropAssistantAudioForBargeIn = false
    private var lastBargeInAt = Date.distantPast
    private let bargeInRMS: Float = 0.045
    private let bargeInConsecutiveFrames = 2
    private let transcriptMergeWindow: TimeInterval = 30

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
        microphoneError = nil
        transcript.removeAll()
        setPhase(.connecting, status: "Starting default agent...")
        isRunning = true

        do {
            try await connectSocket()
            do {
                try await startMicrophone()
            } catch {
                microphoneError = error.localizedDescription
            }
            await startCamera()
            appendSystemLine(for: trigger)
            setPhase(.listening, status: microphoneRunning ? "Listening..." : "Listening — microphone off")
            await updateLiveActivity(status: microphoneRunning ? "Listening through \(AgentDefaults.defaultPersonaName)" : "Camera running, microphone off")
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

        stopMicrophone(deactivateSession: true)
        stopCamera()
        stopPlayback()
        dropAssistantAudioForBargeIn = false
        bargeInCandidateFrames = 0
        audioSessionConfigured = false
        isRunning = false
        sessionID = nil
        setPhase(.idle, status: "Ready")
        await liveActivity.end()
    }

    func retryCamera() async {
        await startCamera()
    }

    func toggleMicrophone() async {
        guard isRunning else { return }
        if microphoneRunning {
            stopMicrophone()
            setPhase(.listening, status: "Microphone off")
            await updateLiveActivity(status: "Microphone off")
            return
        }

        do {
            try await startMicrophone()
            setPhase(.listening, status: "Listening...")
            await updateLiveActivity(status: "Listening through \(AgentDefaults.defaultPersonaName)")
        } catch {
            microphoneError = error.localizedDescription
            errorMessage = error.localizedDescription
            setPhase(.listening, status: "Microphone unavailable")
            await updateLiveActivity(status: "Microphone unavailable")
        }
    }

    func toggleCamera() async {
        guard isRunning else { return }
        if cameraRunning {
            stopCamera()
        } else {
            await startCamera()
        }
    }

    func flipCamera() async {
        guard isRunning, cameraRunning else { return }
        do {
            cameraFacingFront = try await cameraStreamer.flipCamera()
        } catch {
            let message = error.localizedDescription
            cameraError = message
            errorMessage = message
            await sendCameraStatus("unavailable", reason: message)
        }
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
            "voice_id": AgentDefaults.defaultVoiceID,
            "language": "en-US"
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

    private func sendCameraStatus(_ state: String, reason: String = "") async {
        try? await sendJSON(["type": "camera_status", "state": state, "reason": reason])
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
                dropAssistantAudioForBargeIn = false
                endAssistantAudioSuppressionIfIdle()
                setPhase(.listening, status: "Listening...")
            } else if state == "speaking" {
                beginAssistantAudioSuppression()
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
            appendTranscript(role: role, text: lineText)
            Task {
                await updateLiveActivity(status: statusText)
            }
        case "audio":
            if dropAssistantAudioForBargeIn {
                return
            }
            if let encoded = object["data"] as? String {
                playAudio(base64: encoded)
            }
        case "turn_complete":
            finalizeLatestTranscriptLine()
            dropAssistantAudioForBargeIn = false
            endAssistantAudioSuppressionIfIdle()
            setPhase(.listening, status: microphoneRunning ? "Listening..." : "Microphone off")
            Task {
                await updateLiveActivity(status: statusText)
            }
        case "interrupted":
            dropAssistantAudioForBargeIn = false
            stopPlayback()
            setPhase(.listening, status: microphoneRunning ? "Listening..." : "Microphone off")
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
        guard !microphoneRunning else { return }
        let granted = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
        guard granted else {
            throw NSError(domain: "AgentSessionClient", code: 1, userInfo: [NSLocalizedDescriptionKey: "Microphone permission is required"])
        }

        try configureSharedAudioSession()

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let rms = Self.rmsLevel(buffer: buffer)
            let pcmData = Self.makePCM16MonoData(buffer: buffer, sourceFormat: format, targetSampleRate: 16_000)
            guard !pcmData.isEmpty else { return }
            Task { @MainActor in
                if self.shouldTriggerBargeIn(rms: rms) {
                    self.handleLocalBargeIn()
                    try? await self.sendJSON(["type": "audio", "data": pcmData.base64EncodedString()])
                    return
                }
                guard self.shouldSendMicrophoneAudio else { return }
                try? await self.sendJSON(["type": "audio", "data": pcmData.base64EncodedString()])
            }
        }

        audioEngine.prepare()
        try audioEngine.start()
        microphoneRunning = true
        microphoneError = nil
    }

    private func stopMicrophone(deactivateSession: Bool = false) {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        if deactivateSession {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
        microphoneRunning = false
    }

    private func startCamera() async {
        do {
            try await cameraStreamer.start()
            cameraRunning = true
            cameraError = nil
            cameraFacingFront = cameraStreamer.isFrontCamera
        } catch {
            let message = error.localizedDescription
            cameraRunning = false
            cameraError = message
            await sendCameraStatus("unavailable", reason: message)
        }
    }

    private func stopCamera() {
        cameraStreamer.stop()
        cameraRunning = false
        Task {
            await sendCameraStatus("off", reason: "Camera was turned off in the app.")
        }
    }

    private func playAudio(base64: String) {
        guard let data = Data(base64Encoded: base64), !data.isEmpty else { return }
        configurePlaybackIfNeeded()
        beginAssistantAudioSuppression()

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

        pendingPlaybackBuffers += 1
        playerNode.scheduleBuffer(buffer) { [weak self] in
            Task { @MainActor [weak self] in
                self?.finishAssistantAudioBuffer()
            }
        }
        if !playerNode.isPlaying {
            playerNode.play()
        }
    }

    private func configurePlaybackIfNeeded() {
        try? configureSharedAudioSession()
        if !playbackConfigured {
            playbackEngine.attach(playerNode)
            let format = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)
            playbackEngine.connect(playerNode, to: playbackEngine.mainMixerNode, format: format)
            playbackEngine.prepare()
            playbackConfigured = true
        }
        if !playbackEngine.isRunning {
            try? playbackEngine.start()
            if !playerNode.isPlaying {
                playerNode.play()
            }
        }
    }

    private func configureSharedAudioSession() throws {
        if audioSessionConfigured {
            return
        }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try? session.setPreferredSampleRate(48_000)
        try? session.setPreferredIOBufferDuration(0.02)
        try session.setActive(true)
        try? session.overrideOutputAudioPort(.speaker)
        audioSessionConfigured = true
    }

    private func stopPlayback(resumeMicrophoneImmediately: Bool = false) {
        playerNode.stop()
        playbackEngine.stop()
        pendingPlaybackBuffers = 0
        assistantAudioActive = false
        suppressMicrophoneUntil = resumeMicrophoneImmediately ? .distantPast : Date().addingTimeInterval(0.3)
    }

    private var shouldSendMicrophoneAudio: Bool {
        microphoneRunning && !assistantAudioActive && Date() >= suppressMicrophoneUntil
    }

    private func shouldTriggerBargeIn(rms: Float) -> Bool {
        guard microphoneRunning, assistantAudioActive else {
            bargeInCandidateFrames = 0
            return false
        }

        guard Date().timeIntervalSince(lastBargeInAt) > 0.8 else {
            return false
        }

        if rms >= bargeInRMS {
            bargeInCandidateFrames += 1
        } else {
            bargeInCandidateFrames = 0
        }

        return bargeInCandidateFrames >= bargeInConsecutiveFrames
    }

    private func handleLocalBargeIn() {
        dropAssistantAudioForBargeIn = true
        lastBargeInAt = Date()
        bargeInCandidateFrames = 0
        stopPlayback(resumeMicrophoneImmediately: true)
        setPhase(.listening, status: "Listening...")
    }

    private func beginAssistantAudioSuppression() {
        assistantAudioActive = true
        suppressMicrophoneUntil = Date().addingTimeInterval(0.8)
    }

    private func finishAssistantAudioBuffer() {
        pendingPlaybackBuffers = max(pendingPlaybackBuffers - 1, 0)
        if pendingPlaybackBuffers == 0 {
            assistantAudioActive = false
            suppressMicrophoneUntil = Date().addingTimeInterval(0.6)
        }
    }

    private func endAssistantAudioSuppressionIfIdle() {
        if pendingPlaybackBuffers == 0 {
            assistantAudioActive = false
            suppressMicrophoneUntil = Date().addingTimeInterval(0.6)
        }
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
        transcript.append(AgentTranscriptLine(role: "system", text: message, createdAt: Date(), final: true))
    }

    private func appendTranscript(role: String, text: String) {
        let cleaned = normalizedTranscriptText(text)
        guard !cleaned.isEmpty, isEnglishLike(cleaned) else { return }

        let now = Date()
        if let lastIndex = transcript.indices.last,
           transcript[lastIndex].role == role,
           now.timeIntervalSince(transcript[lastIndex].createdAt) <= transcriptMergeWindow {
            transcript[lastIndex].text = mergeTranscriptText(transcript[lastIndex].text, cleaned)
            transcript[lastIndex].final = false
        } else {
            transcript.append(AgentTranscriptLine(role: role, text: cleaned, createdAt: now))
        }
        trimTranscript()
    }

    private func finalizeLatestTranscriptLine() {
        guard let lastIndex = transcript.indices.last else { return }
        transcript[lastIndex].final = true
    }

    private func normalizedTranscriptText(_ text: String) -> String {
        text.replacingOccurrences(of: "\n", with: " ")
            .split(separator: " ")
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func mergeTranscriptText(_ current: String, _ next: String) -> String {
        guard !current.isEmpty else { return next }
        let needsSpace = !current.hasSuffix(" ") && !next.hasPrefix(" ") && !next.firstIsPunctuation
        return current + (needsSpace ? " " : "") + next
    }

    private func isEnglishLike(_ text: String) -> Bool {
        for scalar in text.unicodeScalars {
            if CharacterSet.letters.contains(scalar), !scalar.isLatinLetter {
                return false
            }
        }
        return true
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

    nonisolated private static func rmsLevel(buffer: AVAudioPCMBuffer) -> Float {
        guard let channel = buffer.floatChannelData?[0] else { return 0 }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return 0 }

        var sum: Float = 0
        for index in 0..<frameCount {
            let sample = channel[index]
            sum += sample * sample
        }
        return sqrt(sum / Float(frameCount))
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

private final class CameraFrameStreamer: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let session: AVCaptureSession
    private let queue = DispatchQueue(label: "co.uphomes.visionvoiceagent.camera")
    private let context = CIContext()
    private let onFrame: (Data) -> Void
    private let onStatus: (Bool, String?) -> Void
    private var configured = false
    private var lastFrameDate = Date.distantPast
    private var currentPosition = AVCaptureDevice.Position.back

    var isFrontCamera: Bool {
        currentPosition == .front
    }

    init(session: AVCaptureSession, onFrame: @escaping (Data) -> Void, onStatus: @escaping (Bool, String?) -> Void) {
        self.session = session
        self.onFrame = onFrame
        self.onStatus = onStatus
        super.init()
    }

    func start() async throws {
        let granted = await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: .video) { allowed in
                continuation.resume(returning: allowed)
            }
        }
        guard granted else {
            throw NSError(domain: "CameraFrameStreamer", code: 1, userInfo: [NSLocalizedDescriptionKey: "Camera access is blocked. Enable camera permission in Settings."])
        }

        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    try self.configureIfNeeded()
                    if !self.session.isRunning {
                        self.session.startRunning()
                    }
                    self.onStatus(true, nil)
                    continuation.resume()
                } catch {
                    self.onStatus(false, error.localizedDescription)
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    func stop() {
        queue.async {
            if self.session.isRunning {
                self.session.stopRunning()
            }
            self.onStatus(false, nil)
        }
    }

    func flipCamera() async throws -> Bool {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do {
                    let nextPosition: AVCaptureDevice.Position = self.currentPosition == .front ? .back : .front
                    try self.configureInput(position: nextPosition)
                    continuation.resume(returning: self.currentPosition == .front)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private func configureIfNeeded() throws {
        guard !configured else { return }

        session.beginConfiguration()
        if session.canSetSessionPreset(.hd1920x1080) {
            session.sessionPreset = .hd1920x1080
        } else {
            session.sessionPreset = .high
        }
        defer { session.commitConfiguration() }

        try addInput(position: currentPosition)

        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else {
            throw NSError(domain: "CameraFrameStreamer", code: 4, userInfo: [NSLocalizedDescriptionKey: "Camera output cannot be added."])
        }
        session.addOutput(output)
        output.connection(with: .video)?.videoRotationAngle = 90

        configured = true
    }

    private func configureInput(position: AVCaptureDevice.Position) throws {
        if !configured {
            currentPosition = position
            try configureIfNeeded()
            return
        }

        session.beginConfiguration()
        defer { session.commitConfiguration() }

        for input in session.inputs {
            guard let deviceInput = input as? AVCaptureDeviceInput,
                  deviceInput.device.hasMediaType(.video) else {
                continue
            }
            session.removeInput(deviceInput)
        }

        try addInput(position: position)
        for output in session.outputs {
            output.connection(with: .video)?.videoRotationAngle = 90
        }
    }

    private func addInput(position: AVCaptureDevice.Position) throws {
        guard let device = Self.preferredCamera(for: position) else {
            throw NSError(domain: "CameraFrameStreamer", code: 2, userInfo: [NSLocalizedDescriptionKey: "No camera is available on this device."])
        }

        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            throw NSError(domain: "CameraFrameStreamer", code: 3, userInfo: [NSLocalizedDescriptionKey: "Camera input cannot be added."])
        }
        session.addInput(input)
        currentPosition = device.position == .unspecified ? position : device.position
    }

    private static func preferredCamera(for position: AVCaptureDevice.Position) -> AVCaptureDevice? {
        if position == .back {
            return AVCaptureDevice.default(.builtInTripleCamera, for: .video, position: .back) ??
                AVCaptureDevice.default(.builtInDualWideCamera, for: .video, position: .back) ??
                AVCaptureDevice.default(.builtInDualCamera, for: .video, position: .back) ??
                AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back)
        }

        if position == .front {
            return AVCaptureDevice.default(.builtInTrueDepthCamera, for: .video, position: .front) ??
                AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .front)
        }

        return AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .unspecified)
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard Date().timeIntervalSince(lastFrameDate) >= 2 else { return }
        lastFrameDate = Date()

        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvPixelBuffer: imageBuffer)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let data = context.jpegRepresentation(of: ciImage, colorSpace: colorSpace, options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]) else {
            return
        }
        onFrame(data)
    }
}

extension CameraFrameStreamer: @unchecked Sendable {}

private extension UnicodeScalar {
    var isLatinLetter: Bool {
        switch value {
        case 0x0041...0x005A,
             0x0061...0x007A,
             0x00C0...0x024F,
             0x1E00...0x1EFF:
            return true
        default:
            return false
        }
    }
}

private extension String {
    var firstIsPunctuation: Bool {
        guard let firstScalar = unicodeScalars.first else { return false }
        return CharacterSet.punctuationCharacters.contains(firstScalar)
    }
}
