#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


LAUNCH_HELPER = Path(__file__).with_name("launch_macos_app.swift").resolve()


class MacOSUserLaunchSmokeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _app_binary(app: Path) -> Path:
    bundle = app.resolve()
    if sys.platform != "darwin":
        raise MacOSUserLaunchSmokeError("MACOS_HOST_REQUIRED")
    if not bundle.is_dir() or bundle.is_symlink() or bundle.suffix != ".app":
        raise MacOSUserLaunchSmokeError("APP_BUNDLE_INVALID")
    info_path = bundle / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise MacOSUserLaunchSmokeError("APP_INFO_INVALID") from error
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise MacOSUserLaunchSmokeError("APP_EXECUTABLE_NAME_INVALID")
    executable = bundle / "Contents" / "MacOS" / executable_name
    if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
        raise MacOSUserLaunchSmokeError("APP_EXECUTABLE_INVALID")
    return executable.resolve()


def _matching_processes(executable: Path) -> set[int]:
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MacOSUserLaunchSmokeError("PROCESS_QUERY_FAILED") from error
    expected = str(executable)
    matches: set[int] = set()
    for line in output.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1] == expected:
            try:
                matches.add(int(fields[0]))
            except ValueError:
                continue
    return matches


def _owned_processes(executable: Path, owned_pids: set[int]) -> set[int]:
    return _matching_processes(executable) & owned_pids


def _signal_owned_processes(executable: Path, owned_pids: set[int], sig: signal.Signals) -> None:
    for pid in _owned_processes(executable, owned_pids):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _wait_for_owned_exit(executable: Path, owned_pids: set[int], timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = _owned_processes(executable, owned_pids)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.1)


def _stop_processes(executable: Path, owned_pids: set[int], timeout: float = 15) -> None:
    _signal_owned_processes(executable, owned_pids, signal.SIGTERM)
    remaining = _wait_for_owned_exit(executable, owned_pids, timeout)
    if not remaining:
        return
    _signal_owned_processes(executable, remaining, signal.SIGKILL)
    remaining = _wait_for_owned_exit(executable, remaining, timeout)
    if remaining:
        raise MacOSUserLaunchSmokeError("APP_PROCESS_CLEANUP_FAILED")


def _launch_application(app: Path) -> int:
    try:
        result = subprocess.run(
            ["xcrun", "swift", str(LAUNCH_HELPER), str(app.resolve())],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except subprocess.TimeoutExpired as error:
        raise MacOSUserLaunchSmokeError("APP_OPEN_TIMEOUT") from error
    except OSError as error:
        raise MacOSUserLaunchSmokeError("APP_OPEN_FAILED") from error
    if result.returncode != 0:
        raise MacOSUserLaunchSmokeError("APP_OPEN_FAILED")
    match = re.fullmatch(r"PID=([1-9][0-9]*)\n?", result.stdout)
    if match is None:
        raise MacOSUserLaunchSmokeError("APP_PROCESS_OWNERSHIP_UNPROVABLE")

    return int(match.group(1))


def smoke_macos_user_launch(app: Path, stable_seconds: float = 5) -> None:
    executable = _app_binary(app)
    if _matching_processes(executable):
        raise MacOSUserLaunchSmokeError("APP_PROCESS_ALREADY_RUNNING")
    processes: set[int] = set()
    try:
        processes = {_launch_application(app)}

        launch_deadline = time.monotonic() + 15
        while time.monotonic() < launch_deadline:
            if _owned_processes(executable, processes) == processes:
                break
            time.sleep(0.05)
        else:
            raise MacOSUserLaunchSmokeError("APP_PROCESS_NOT_STARTED")

        stability_deadline = time.monotonic() + stable_seconds
        while time.monotonic() < stability_deadline:
            if _owned_processes(executable, processes) != processes:
                raise MacOSUserLaunchSmokeError("APP_PROCESS_NOT_STABLE")
            time.sleep(0.1)
        if _owned_processes(executable, processes) != processes:
            raise MacOSUserLaunchSmokeError("APP_PROCESS_NOT_STABLE")
    finally:
        _stop_processes(executable, processes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a macOS app through Launch Services.")
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    try:
        smoke_macos_user_launch(args.app)
    except MacOSUserLaunchSmokeError as error:
        print(f"MACOS_USER_LAUNCH_SMOKE=FAIL:{error.code}", file=sys.stderr)
        return 1
    print("MACOS_USER_LAUNCH_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
