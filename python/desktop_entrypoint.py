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
READY_FILE_ENV = "QIAN_DESKTOP_READY_FILE"
READY_FILE_PREFIX = ".qian-sidecar-ready-"


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


def _configured_ready_file(data_dir: Path) -> Path | None:
    raw = os.environ.get(READY_FILE_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.parent.resolve() != data_dir.resolve():
        raise RuntimeError("QIAN_DESKTOP_READY_FILE_INVALID")
    if not path.name.startswith(READY_FILE_PREFIX) or path.suffix != ".json":
        raise RuntimeError("QIAN_DESKTOP_READY_FILE_INVALID")
    if path.exists() or path.is_symlink():
        raise RuntimeError("QIAN_DESKTOP_READY_FILE_INVALID")
    return path


def _write_ready_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    data_dir = Path(_required_env("QIAN_DESKTOP_DATA_DIR")).expanduser().resolve()
    token = _required_env("QIAN_DESKTOP_TOKEN")
    ready_file = _configured_ready_file(data_dir)
    listener = _bind_loopback(_configured_port())
    port = int(listener.getsockname()[1])

    shutdown_requested = threading.Event()
    app = create_desktop_app(
        data_dir=data_dir,
        launch_token=token,
        shutdown_callback=shutdown_requested.set,
    )
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

    ready_payload = json.dumps(
        {"host": HOST, "port": port, "pid": os.getpid()},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        if ready_file is not None:
            _write_ready_file(ready_file, ready_payload)
        else:
            print(
                READY_PREFIX + ready_payload.decode("utf-8"),
                flush=True,
            )
    except Exception:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise

    try:
        while thread.is_alive():
            if shutdown_requested.wait(timeout=0.05):
                server.should_exit = True
            thread.join(timeout=0.05)
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
