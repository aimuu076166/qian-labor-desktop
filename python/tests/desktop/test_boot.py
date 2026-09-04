from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def test_desktop_entrypoint_binds_loopback_reports_ready_and_hides_token(tmp_path: Path) -> None:
    token = "boot-test-token-that-must-never-be-printed"
    env = {
        **os.environ,
        "QIAN_DESKTOP_DATA_DIR": str(tmp_path),
        "QIAN_DESKTOP_TOKEN": token,
        "QIAN_DESKTOP_PORT": "0",
    }
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "desktop_entrypoint.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 15
        ready_line = ""
        while time.monotonic() < deadline:
            line = process.stdout.readline().strip()
            if line.startswith("QIAN_DESKTOP_READY="):
                ready_line = line
                break
            if process.poll() is not None:
                break
        assert ready_line, "sidecar did not emit a READY line"
        assert token not in ready_line
        payload = json.loads(ready_line.split("=", 1)[1])
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["port"], int) and payload["port"] > 0
        assert payload["pid"] == process.pid

        response = httpx.get(
            f"http://127.0.0.1:{payload['port']}/health",
            timeout=3,
        )
        assert response.status_code == 200
        assert response.json()["service"] == "qian-labor-desktop-sidecar"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_desktop_entrypoint_writes_an_atomic_token_free_ready_file(tmp_path: Path) -> None:
    token = "ready-file-token-that-must-never-be-written"
    ready_file = tmp_path / ".qian-sidecar-ready-test.json"
    env = {
        **os.environ,
        "QIAN_DESKTOP_DATA_DIR": str(tmp_path),
        "QIAN_DESKTOP_TOKEN": token,
        "QIAN_DESKTOP_PORT": "0",
        "QIAN_DESKTOP_READY_FILE": str(ready_file),
    }
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "desktop_entrypoint.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready_file.is_file() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.is_file(), "sidecar did not create its READY file"
        content = ready_file.read_text(encoding="utf-8")
        assert token not in content
        payload = json.loads(content)
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["port"], int) and payload["port"] > 0
        assert payload["pid"] == process.pid
        assert not ready_file.with_name(ready_file.name + ".tmp").exists()
        if os.name != "nt":
            assert stat.S_IMODE(ready_file.stat().st_mode) == 0o600
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_desktop_entrypoint_authenticated_shutdown_exits_cleanly_without_token_leak(
    tmp_path: Path,
) -> None:
    token = "entrypoint-shutdown-token-that-must-never-be-printed"
    env = {
        **os.environ,
        "QIAN_DESKTOP_DATA_DIR": str(tmp_path),
        "QIAN_DESKTOP_TOKEN": token,
        "QIAN_DESKTOP_PORT": "0",
    }
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "desktop_entrypoint.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 15
        ready_line = ""
        while time.monotonic() < deadline:
            line = process.stdout.readline().strip()
            if line.startswith("QIAN_DESKTOP_READY="):
                ready_line = line
                break
            if process.poll() is not None:
                break
        assert ready_line, "sidecar did not emit a READY line"
        payload = json.loads(ready_line.split("=", 1)[1])
        endpoint = f"http://127.0.0.1:{payload['port']}/api/internal/shutdown"

        assert httpx.post(endpoint, timeout=3).status_code == 401
        assert process.poll() is None
        response = httpx.post(
            endpoint,
            headers={"X-Qian-Desktop-Token": token},
            timeout=3,
        )
        assert response.status_code == 202
        assert response.json() == {"status": "shutdown_requested"}
        assert token not in response.text

        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert token not in process.stderr.read()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
