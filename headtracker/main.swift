// Streams AirPods head orientation and voice commands (voice.swift) to the game as JSON over UDP on 127.0.0.1.
// Usage: HeadTracker <port> <parent pid>   (exits when the parent process is gone)
import CoreMotion
import Foundation

let args = CommandLine.arguments
let port = UInt16(args.count > 1 ? args[1] : "47811") ?? 47811
let parentPid = Int32(args.count > 2 ? args[2] : "0") ?? 0

let sock = socket(AF_INET, SOCK_DGRAM, 0)
var dest = sockaddr_in()
dest.sin_family = sa_family_t(AF_INET)
dest.sin_port = port.bigEndian
dest.sin_addr.s_addr = inet_addr("127.0.0.1")

func send(_ msg: String) {
    msg.withCString { p in
        withUnsafePointer(to: &dest) { d in
            d.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                _ = sendto(sock, p, strlen(p), 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }
}

final class Delegate: NSObject, CMHeadphoneMotionManagerDelegate {
    func headphoneMotionManagerDidConnect(_ manager: CMHeadphoneMotionManager) {
        send("{\"event\":\"connected\"}")
    }
    func headphoneMotionManagerDidDisconnect(_ manager: CMHeadphoneMotionManager) {
        send("{\"event\":\"disconnected\"}")
    }
}

let manager = CMHeadphoneMotionManager()
let delegate = Delegate()
manager.delegate = delegate

func authName() -> String {
    switch CMHeadphoneMotionManager.authorizationStatus() {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

send("{\"event\":\"started\",\"available\":\(manager.isDeviceMotionAvailable),\"auth\":\"\(authName())\"}")

manager.startDeviceMotionUpdates(to: OperationQueue()) { motion, error in
    if let error = error {
        send("{\"event\":\"error\",\"message\":\"\(error.localizedDescription.replacingOccurrences(of: "\"", with: "'"))\"}")
        return
    }
    guard let m = motion else { return }
    let a = m.attitude
    send(String(format: "{\"yaw\":%.5f,\"pitch\":%.5f,\"roll\":%.5f}", a.yaw, a.pitch, a.roll))
}

let voice = VoiceCommands()
voice.start()

// Heartbeat with status, and quit once the game has exited.
Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
    if parentPid > 0 && kill(parentPid, 0) != 0 {
        manager.stopDeviceMotionUpdates()
        exit(0)
    }
    send("{\"event\":\"status\",\"active\":\(manager.isDeviceMotionActive),\"auth\":\"\(authName())\"}")
}

RunLoop.main.run()
