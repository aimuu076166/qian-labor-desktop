#!/usr/bin/env python3
from __future__ import annotations

import argparse
import struct
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICO_HEADER_SIZE = 6
ICO_DIRECTORY_ENTRY_SIZE = 16


def png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != PNG_SIGNATURE or payload[12:16] != b"IHDR":
        raise RuntimeError("TAURI_ICON_SOURCE_INVALID_PNG")
    width, height = struct.unpack_from(">II", payload, 16)
    if not 1 <= width <= 256 or not 1 <= height <= 256:
        raise RuntimeError("TAURI_ICON_DIMENSIONS_UNSUPPORTED")
    return width, height


def write_windows_ico(source: Path, output: Path) -> Path:
    png = source.read_bytes()
    width, height = png_dimensions(png)
    width_byte = 0 if width == 256 else width
    height_byte = 0 if height == 256 else height
    image_offset = ICO_HEADER_SIZE + ICO_DIRECTORY_ENTRY_SIZE

    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII",
        width_byte,
        height_byte,
        0,
        0,
        1,
        32,
        len(png),
        image_offset,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + entry + png)
    return output


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Prepare generated Tauri desktop assets.")
    parser.add_argument(
        "--source",
        type=Path,
        default=root / "apps" / "desktop" / "src-tauri" / "icons" / "icon.png",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "apps" / "desktop" / "src-tauri" / "icons" / "icon.ico",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = write_windows_ico(args.source, args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
