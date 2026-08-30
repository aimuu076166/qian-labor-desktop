from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PREPARE_ASSETS = ROOT / "scripts" / "prepare_tauri_assets.py"
SOURCE_ICON = ROOT / "apps" / "desktop" / "src-tauri" / "icons" / "icon.png"


def test_windows_resource_icon_is_generated_from_committed_png(tmp_path: Path) -> None:
    output = tmp_path / "icon.ico"
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_ASSETS),
            "--source",
            str(SOURCE_ICON),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = output.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", payload, 0)
    assert (reserved, image_type, count) == (0, 1, 1)

    width, height, color_count, reserved_byte, planes, bit_count, size, offset = struct.unpack_from(
        "<BBBBHHII", payload, 6
    )
    assert (width, height) == (64, 64)
    assert (color_count, reserved_byte, planes, bit_count) == (0, 0, 1, 32)
    assert offset == 22
    assert size == len(payload) - offset
    assert payload[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
