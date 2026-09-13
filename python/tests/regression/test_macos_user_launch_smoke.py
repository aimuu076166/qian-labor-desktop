from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "smoke_macos_user_launch.py"


def _load():
    spec = importlib.util.spec_from_file_location("smoke_macos_user_launch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cleanup_never_claims_a_later_same_path_process(monkeypatch) -> None:
    smoke = _load()
    sent: list[tuple[int, signal.Signals]] = []

    def matching_processes(_executable: Path) -> set[int]:
        return {101, 202}

    monkeypatch.setattr(smoke, "_matching_processes", matching_processes)
    monkeypatch.setattr(smoke.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    with pytest.raises(smoke.MacOSUserLaunchSmokeError):
        smoke._stop_processes(
            Path("/Applications/Qian.app/Contents/MacOS/qian"), {101}, timeout=0
        )

    assert sent == [(101, signal.SIGTERM), (101, signal.SIGKILL)]


def test_cleanup_accepts_a_successful_sigterm(monkeypatch) -> None:
    smoke = _load()
    terminated = False
    sent: list[tuple[int, signal.Signals]] = []

    def matching_processes(_executable: Path) -> set[int]:
        return set() if terminated else {101}

    def send_signal(pid: int, sig: signal.Signals) -> None:
        nonlocal terminated
        sent.append((pid, sig))
        if sig == signal.SIGTERM:
            terminated = True

    monkeypatch.setattr(smoke, "_matching_processes", matching_processes)
    monkeypatch.setattr(smoke.os, "kill", send_signal)

    smoke._stop_processes(
        Path("/Applications/Qian.app/Contents/MacOS/qian"), {101}, timeout=0
    )

    assert sent == [(101, signal.SIGTERM)]


def test_cleanup_accepts_a_successful_sigkill_fallback(monkeypatch) -> None:
    smoke = _load()
    killed = False
    sent: list[tuple[int, signal.Signals]] = []

    def matching_processes(_executable: Path) -> set[int]:
        return set() if killed else {101}

    def send_signal(pid: int, sig: signal.Signals) -> None:
        nonlocal killed
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            killed = True

    monkeypatch.setattr(smoke, "_matching_processes", matching_processes)
    monkeypatch.setattr(smoke.os, "kill", send_signal)

    smoke._stop_processes(
        Path("/Applications/Qian.app/Contents/MacOS/qian"), {101}, timeout=0
    )

    assert sent == [(101, signal.SIGTERM), (101, signal.SIGKILL)]


def test_cleanup_fails_when_sigkill_does_not_stop_the_owned_process(monkeypatch) -> None:
    smoke = _load()
    sent: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(smoke, "_matching_processes", lambda _executable: {101})
    monkeypatch.setattr(smoke.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    with pytest.raises(smoke.MacOSUserLaunchSmokeError, match="APP_PROCESS_CLEANUP_FAILED"):
        smoke._stop_processes(
            Path("/Applications/Qian.app/Contents/MacOS/qian"), {101}, timeout=0
        )

    assert sent == [(101, signal.SIGTERM), (101, signal.SIGKILL)]


def test_native_launcher_returns_the_only_pid_that_can_be_owned(monkeypatch) -> None:
    smoke = _load()
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="PID=101\n", stderr="")

    monkeypatch.setattr(smoke.subprocess, "run", run)

    pid = smoke._launch_application(Path("/Applications/Qian.app"))

    assert pid == 101
    assert calls == [
        [
            "xcrun",
            "swift",
            str(smoke.LAUNCH_HELPER),
            "/Applications/Qian.app",
        ]
    ]


def test_smoke_never_claims_an_unrelated_same_path_pid(monkeypatch) -> None:
    smoke = _load()
    executable = Path("/Applications/Qian.app/Contents/MacOS/qian")
    stopped: list[set[int]] = []
    query_count = 0

    monkeypatch.setattr(smoke, "_app_binary", lambda _app: executable)
    monkeypatch.setattr(smoke, "_launch_application", lambda _app: 101)

    def matching_processes(_executable: Path) -> set[int]:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return set()
        return {202, 101}

    monkeypatch.setattr(smoke, "_matching_processes", matching_processes)
    monkeypatch.setattr(
        smoke,
        "_stop_processes",
        lambda _executable, pids: stopped.append(set(pids)),
    )

    smoke.smoke_macos_user_launch(Path("/Applications/Qian.app"), stable_seconds=0)

    assert stopped == [{101}]
