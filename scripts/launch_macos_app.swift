#!/usr/bin/env swift
import AppKit
import Darwin
import Foundation

final class LaunchState: @unchecked Sendable {
    private let lock = NSLock()
    private var result: (finished: Bool, pid: pid_t?, error: Error?) = (false, nil, nil)

    func finish(application: NSRunningApplication?, error: Error?) {
        lock.lock()
        result = (true, application?.processIdentifier, error)
        lock.unlock()
    }

    func snapshot() -> (finished: Bool, pid: pid_t?, error: Error?) {
        lock.lock()
        defer { lock.unlock() }
        return result
    }
}

func writeError(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

guard CommandLine.arguments.count == 2 else {
    writeError("USAGE: launch_macos_app.swift APP_PATH")
    exit(2)
}

let appURL = URL(fileURLWithPath: CommandLine.arguments[1])
let configuration = NSWorkspace.OpenConfiguration()
configuration.createsNewApplicationInstance = true
let state = LaunchState()

NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) {
    application, error in
    state.finish(application: application, error: error)
}

let deadline = Date().addingTimeInterval(15)
while !state.snapshot().finished && Date() < deadline {
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}

let result = state.snapshot()
guard result.finished else {
    writeError("LAUNCH_TIMEOUT")
    exit(1)
}
guard result.error == nil, let pid = result.pid, pid > 0 else {
    writeError("LAUNCH_FAILED")
    exit(1)
}

print("PID=\(pid)")
