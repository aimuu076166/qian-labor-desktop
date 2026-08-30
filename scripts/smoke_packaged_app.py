#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class PackagedSmokeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def validate_smoke_result(root: Path, payload: object) -> int:
    if not isinstance(payload, dict) or set(payload) != {"database_created", "sidecar_pid"}:
        raise PackagedSmokeError("RESULT_SCHEMA_INVALID")
    if payload["database_created"] is not True:
        raise PackagedSmokeError("DATABASE_NOT_CREATED")
    pid = payload["sidecar_pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise PackagedSmokeError("SIDECAR_PID_INVALID")
    if not (root / "app-data" / "qian-labor.db").is_file():
        raise PackagedSmokeError("DATABASE_MISSING")
    return pid


def _popen_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def _validate_binary(path: Path) -> Path:
    if not path.is_file():
        raise PackagedSmokeError("APP_BINARY_MISSING")
    if path.is_symlink():
        raise PackagedSmokeError("APP_BINARY_SYMLINK_REJECTED")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise PackagedSmokeError("APP_BINARY_NOT_EXECUTABLE")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise PackagedSmokeError("APP_BINARY_SUFFIX_INVALID")
    return path.resolve()


def smoke_packaged_app(binary: Path) -> None:
    executable = _validate_binary(binary)
    process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(prefix="qian-rc-smoke-") as temporary:
        root = Path(temporary).resolve()
        env = {
            **os.environ,
            "AI_PROVIDER": "fake",
            "AI_API_KEY": "",
            "QIAN_RC_SMOKE": "1",
            "QIAN_RC_SMOKE_DIR": str(root),
        }
        try:
            process = subprocess.Popen(
                [str(executable)],
                cwd=executable.parent,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_options(),
            )
            try:
                return_code = process.wait(timeout=60)
            except subprocess.TimeoutExpired as error:
                raise PackagedSmokeError("APP_EXIT_TIMEOUT") from error
            if return_code != 0:
                raise PackagedSmokeError("APP_EXIT_FAILED")
            result_path = root / "result.json"
            if not result_path.is_file():
                raise PackagedSmokeError("RESULT_MISSING")
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                raise PackagedSmokeError("RESULT_INVALID") from error
            sidecar_pid = validate_smoke_result(root, payload)
            deadline = time.monotonic() + 15
            while process_is_alive(sidecar_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if process_is_alive(sidecar_pid):
                raise PackagedSmokeError("SIDECAR_RESIDUE")
        except (OSError, subprocess.SubprocessError) as error:
            raise PackagedSmokeError("APP_LAUNCH_FAILED") from error
        finally:
            if process is not None:
                _terminate_process_tree(process)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test one packaged desktop executable.")
    parser.add_argument("--app-binary", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        smoke_packaged_app(args.app_binary)
    except PackagedSmokeError as error:
        print(f"PACKAGED_APP_SMOKE=FAIL:{error.code}", file=sys.stderr)
        return 1
    print("PACKAGED_APP_SMOKE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
