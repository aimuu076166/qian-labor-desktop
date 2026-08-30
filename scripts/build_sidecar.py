#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from prepare_tauri_assets import write_windows_ico

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
ENTRYPOINT = PYTHON_ROOT / "desktop_entrypoint.py"
TAURI_ROOT = ROOT / "apps" / "desktop" / "src-tauri"
BINARIES = TAURI_ROOT / "binaries"
ICON_SOURCE = TAURI_ROOT / "icons" / "icon.png"
WINDOWS_ICON = TAURI_ROOT / "icons" / "icon.ico"


def target_triple() -> str:
    completed = subprocess.run(
        ["rustc", "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("RUST_TARGET_TRIPLE_INVALID")
    return value


def build() -> Path:
    write_windows_ico(ICON_SOURCE, WINDOWS_ICON)
    triple = target_triple()
    executable_suffix = ".exe" if "windows" in triple else ""
    BINARIES.mkdir(parents=True, exist_ok=True)
    destination = BINARIES / f"qian-sidecar-{triple}{executable_suffix}"

    with tempfile.TemporaryDirectory(prefix="qian-sidecar-build-") as temporary:
        temporary_root = Path(temporary)
        dist = temporary_root / "dist"
        work = temporary_root / "work"
        spec = temporary_root / "spec"
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--collect-data",
            "qian_labor",
            "--name",
            "qian-sidecar",
            "--paths",
            str(PYTHON_ROOT / "src"),
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            str(ENTRYPOINT),
        ]
        subprocess.run(command, cwd=ROOT, check=True)
        built = dist / f"qian-sidecar{executable_suffix}"
        if not built.is_file():
            raise RuntimeError("SIDECAR_BUILD_OUTPUT_MISSING")
        shutil.copy2(built, destination)

    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | 0o111)
    print(destination.relative_to(ROOT))
    return destination


if __name__ == "__main__":
    build()
