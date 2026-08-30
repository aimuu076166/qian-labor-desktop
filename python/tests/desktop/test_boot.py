from __future__ import annotations

import json
import os
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
