// Listens on the Mac's built-in microphone for voice commands and reports them to the game.
// The AirPods mic is avoided on purpose: using it drops AirPods into low-quality call audio.
import AVFoundation
import CoreAudio
import Foundation
import Speech

let commands = ["start game", "start the game", "stop game"]

final class VoiceCommands {
    let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    let engine = AVAudioEngine()
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var generation = 0
    var lastFired = Date.distantPast

    func start() {
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard status == .authorized else {
                    send("{\"event\":\"voice\",\"status\":\"speech recognition not allowed\"}")
                    return
                }
                self.startEngine()
            }
        }
    }

    private func startEngine() {
        useBuiltInMic()
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buffer, _ in
            self.request?.append(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            send("{\"event\":\"voice\",\"status\":\"microphone unavailable\"}")
            return
        }
        newTask()
        send("{\"event\":\"voice\",\"status\":\"listening\"}")
    }

    // Each phrase gets a fresh task so a command already heard isn't matched again.
    private func newTask() {
        generation += 1
        let gen = generation
        task?.cancel()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.contextualStrings = commands
        if recognizer?.supportsOnDeviceRecognition == true {
            req.requiresOnDeviceRecognition = true
        }
        request = req
        task = recognizer?.recognitionTask(with: req) { result, error in
            DispatchQueue.main.async {
                guard gen == self.generation else { return }  // a task we already replaced
                if let result = result {
                    let text = result.bestTranscription.formattedString.lowercased()
                    let shown = text.replacingOccurrences(of: "\"", with: "'")
                    send("{\"event\":\"heard\",\"text\":\"\(shown)\"}")
                    if let cmd = commands.first(where: { text.contains($0) }) {
                        if Date().timeIntervalSince(self.lastFired) > 1.5 {
                            self.lastFired = Date()
                            let name = cmd == "start the game" ? "start game" : cmd
                            send("{\"event\":\"command\",\"text\":\"\(name)\"}")
                        }
                        self.newTask()
                    } else if result.isFinal {
                        self.newTask()
                    }
                } else if error != nil {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                        if gen == self.generation { self.newTask() }
                    }
                }
            }
        }
    }

    private func useBuiltInMic() {
        guard var id = builtInMic(), let unit = engine.inputNode.audioUnit else { return }
        AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                             &id, UInt32(MemoryLayout<AudioDeviceID>.size))
    }

    private func builtInMic() -> AudioDeviceID? {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
        for id in ids {
            var transport: UInt32 = 0
            var tsize = UInt32(MemoryLayout<UInt32>.size)
            var taddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType,
                                                   mScope: kAudioObjectPropertyScopeGlobal,
                                                   mElement: kAudioObjectPropertyElementMain)
            AudioObjectGetPropertyData(id, &taddr, 0, nil, &tsize, &transport)
            var saddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                   mScope: kAudioDevicePropertyScopeInput,
                                                   mElement: kAudioObjectPropertyElementMain)
            var ssize: UInt32 = 0
            AudioObjectGetPropertyDataSize(id, &saddr, 0, nil, &ssize)
            if transport == kAudioDeviceTransportTypeBuiltIn && ssize > 0 {
                return id
            }
        }
        return nil
    }
}
