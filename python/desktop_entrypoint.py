#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from qian_labor.desktop.app import create_desktop_app

HOST = "127.0.0.1"
READY_PREFIX = "QIAN_DESKTOP_READY="


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name}_REQUIRED")
    return value


def _configured_port() -> int:
    raw = os.environ.get("QIAN_DESKTOP_PORT", "0").strip() or "0"
    try:
        port = int(raw)
    except ValueError as error:
        raise RuntimeError("QIAN_DESKTOP_PORT_INVALID") from error
    if not 0 <= port <= 65535:
        raise RuntimeError("QIAN_DESKTOP_PORT_INVALID")
    return port


def _bind_loopback(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, port))
    listener.listen(2048)
    return listener


def main() -> int:
    data_dir = Path(_required_env("QIAN_DESKTOP_DATA_DIR"))
    token = _required_env("QIAN_DESKTOP_TOKEN")
    listener = _bind_loopback(_configured_port())
    port = int(listener.getsockname()[1])

    app = create_desktop_app(data_dir=data_dir, launch_token=token)
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="qian-desktop-sidecar",
    )
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("DESKTOP_SIDECAR_START_FAILED")

    print(
        READY_PREFIX
        + json.dumps(
            {"host": HOST, "port": port, "pid": os.getpid()},
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )

    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - process boundary prints code only
        print(f"QIAN_DESKTOP_ERROR={type(error).__name__}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
