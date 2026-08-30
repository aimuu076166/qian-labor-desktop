#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
from pathlib import Path


EXPECTED_VERSION = "0.1.0"
EXPECTED_IDENTIFIER = "cn.qianlabor.desktop"
PROHIBITED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".cache"}
PROHIBITED_NAMES = {".env", "test-output", "test_output"}
PROHIBITED_FRAGMENTS = {"fixture", "synthetic-input", "synthetic_input"}
SENSITIVE_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{16,}", re.I),
)
BUILD_PATH_PATTERNS = (
    b"/Users/runner/",
    b"/home/runner/work/",
    b"/private/var/folders/",
    b"C:\\Users\\runneradmin\\",
    b"D:\\a\\",
    b"\\AppData\\Local\\Temp\\",
)


class BundleVerificationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def detect_macho_arch(path: Path) -> str:
    try:
        header = path.read_bytes()[:32]
    except OSError as error:
        raise BundleVerificationError("EXECUTABLE_UNREADABLE") from error
    if len(header) < 8:
        raise BundleVerificationError("EXECUTABLE_HEADER_INVALID")
    if header[:4] == b"\xcf\xfa\xed\xfe":
        cpu_type = struct.unpack_from("<i", header, 4)[0]
    elif header[:4] == b"\xfe\xed\xfa\xcf":
        cpu_type = struct.unpack_from(">i", header, 4)[0]
    else:
        raise BundleVerificationError("EXECUTABLE_HEADER_INVALID")
    if cpu_type == 0x0100000C:
        return "arm64"
    if cpu_type == 0x01000007:
        return "x64"
    return "unknown"


def detect_pe_arch(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                raise BundleVerificationError("EXECUTABLE_HEADER_INVALID")
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            if pe_offset > 16 * 1024 * 1024:
                raise BundleVerificationError("EXECUTABLE_HEADER_INVALID")
            handle.seek(pe_offset)
            pe_header = handle.read(6)
    except OSError as error:
        raise BundleVerificationError("EXECUTABLE_UNREADABLE") from error
    if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
        raise BundleVerificationError("EXECUTABLE_HEADER_INVALID")
    machine = struct.unpack_from("<H", pe_header, 4)[0]
    if machine == 0x8664:
        return "x64"
    if machine == 0x014C:
        return "x86"
    if machine == 0xAA64:
        return "arm64"
    return "unknown"


def _windows_product_version(path: Path) -> str:
    if os.name != "nt":
        raise BundleVerificationError("WINDOWS_VERSION_UNAVAILABLE")

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("structure_version", ctypes.c_uint32),
            ("file_version_ms", ctypes.c_uint32),
            ("file_version_ls", ctypes.c_uint32),
            ("product_version_ms", ctypes.c_uint32),
            ("product_version_ls", ctypes.c_uint32),
            ("file_flags_mask", ctypes.c_uint32),
            ("file_flags", ctypes.c_uint32),
            ("file_os", ctypes.c_uint32),
            ("file_type", ctypes.c_uint32),
            ("file_subtype", ctypes.c_uint32),
            ("file_date_ms", ctypes.c_uint32),
            ("file_date_ls", ctypes.c_uint32),
        ]

    version = ctypes.windll.version  # type: ignore[attr-defined]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise BundleVerificationError("WINDOWS_VERSION_MISSING")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise BundleVerificationError("WINDOWS_VERSION_MISSING")
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise BundleVerificationError("WINDOWS_VERSION_MISSING")
    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise BundleVerificationError("WINDOWS_VERSION_INVALID")
    return ".".join(
        str(value)
        for value in (
            info.product_version_ms >> 16,
            info.product_version_ms & 0xFFFF,
            info.product_version_ls >> 16,
            info.product_version_ls & 0xFFFF,
        )
    )


def _load_config(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleVerificationError("SOURCE_CONFIG_INVALID") from error
    if payload.get("version") != EXPECTED_VERSION:
        raise BundleVerificationError("VERSION_INVALID")
    if payload.get("identifier") != EXPECTED_IDENTIFIER:
        raise BundleVerificationError("IDENTIFIER_INVALID")
    if not isinstance(payload.get("productName"), str):
        raise BundleVerificationError("SOURCE_CONFIG_INVALID")
    return payload


def _scan_file(path: Path) -> None:
    overlap = b""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                data = overlap + chunk
                if any(pattern.search(data) for pattern in SENSITIVE_PATTERNS):
                    raise BundleVerificationError("SENSITIVE_PAYLOAD")
                if any(pattern in data for pattern in BUILD_PATH_PATTERNS):
                    raise BundleVerificationError("BUILD_PATH_REFERENCE")
                overlap = data[-512:]
    except BundleVerificationError:
        raise
    except OSError as error:
        raise BundleVerificationError("PAYLOAD_UNREADABLE") from error


def _scan_payload(payload: Path) -> None:
    if payload.is_symlink() or not payload.is_dir():
        raise BundleVerificationError("PAYLOAD_INVALID")
    try:
        root = payload.resolve(strict=True)
    except OSError as error:
        raise BundleVerificationError("PAYLOAD_INVALID") from error
    for path in payload.rglob("*"):
        lowered = path.name.lower()
        if lowered in PROHIBITED_NAMES or path.suffix.lower() in PROHIBITED_SUFFIXES:
            raise BundleVerificationError("PROHIBITED_PAYLOAD_FILE")
        if any(fragment in lowered for fragment in PROHIBITED_FRAGMENTS):
            raise BundleVerificationError("PROHIBITED_PAYLOAD_FILE")
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except OSError as error:
                raise BundleVerificationError("PAYLOAD_SYMLINK_INVALID") from error
            if not target.is_relative_to(root):
                raise BundleVerificationError("PAYLOAD_SYMLINK_ESCAPE")
            continue
        if path.is_file():
            _scan_file(path)


def _verify_macos(payload: Path, config: dict[str, object]) -> None:
    plist_path = payload / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as error:
        raise BundleVerificationError("MACOS_INFO_PLIST_INVALID") from error
    if info.get("CFBundleIdentifier") != config["identifier"]:
        raise BundleVerificationError("IDENTIFIER_INVALID")
    if info.get("CFBundleShortVersionString") != config["version"]:
        raise BundleVerificationError("VERSION_INVALID")
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise BundleVerificationError("MAIN_EXECUTABLE_MISSING")
    main = payload / "Contents" / "MacOS" / executable_name
    sidecars = [path for path in payload.rglob("qian-sidecar*") if path.is_file()]
    if not main.is_file():
        raise BundleVerificationError("MAIN_EXECUTABLE_MISSING")
    if len(sidecars) != 1:
        raise BundleVerificationError("SIDECAR_COUNT_INVALID")
    if detect_macho_arch(main) != "arm64" or detect_macho_arch(sidecars[0]) != "arm64":
        raise BundleVerificationError("ARCHITECTURE_INVALID")


def _verify_windows(
    payload: Path, config: dict[str, object], *, verify_windows_version: bool
) -> None:
    sidecars = [path for path in payload.rglob("qian-sidecar*") if path.is_file()]
    if len(sidecars) != 1 or sidecars[0].suffix.lower() != ".exe":
        raise BundleVerificationError("SIDECAR_COUNT_INVALID")
    candidate_names = {f"{config['productName']}.exe", "qian-labor-desktop.exe"}
    main_candidates = [
        path for path in payload.rglob("*.exe") if path.is_file() and path.name in candidate_names
    ]
    if len(main_candidates) != 1:
        raise BundleVerificationError("MAIN_EXECUTABLE_MISSING")
    main = main_candidates[0]
    if detect_pe_arch(main) != "x64" or detect_pe_arch(sidecars[0]) != "x64":
        raise BundleVerificationError("ARCHITECTURE_INVALID")
    if verify_windows_version and not _windows_product_version(main).startswith(
        f"{config['version']}."
    ):
        raise BundleVerificationError("VERSION_INVALID")


def verify_payload(
    payload: Path,
    platform: str,
    config_path: Path,
    *,
    verify_windows_version: bool = True,
) -> None:
    config = _load_config(config_path)
    _scan_payload(payload)
    if platform == "macos":
        _verify_macos(payload, config)
    elif platform == "windows":
        _verify_windows(payload, config, verify_windows_version=verify_windows_version)
    else:
        raise BundleVerificationError("PLATFORM_INVALID")


def _verify_commit(expected: str, repo_root: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise BundleVerificationError("COMMIT_INVALID")
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise BundleVerificationError("COMMIT_MISMATCH")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an unpacked RC application payload.")
    parser.add_argument("--platform", choices=("macos", "windows"), required=True)
    parser.add_argument("--payload", type=Path, action="append", required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        _verify_commit(args.expected_commit, args.repo_root)
        for payload in args.payload:
            verify_payload(payload, args.platform, args.source_config)
    except BundleVerificationError as error:
        print(f"RC_BUNDLE_VERIFY=FAIL:{error.code}", file=sys.stderr)
        return 1
    for marker in (
        "BUNDLE_STRUCTURE=PASS",
        "BUNDLE_ARCHITECTURE=PASS",
        "BUNDLE_VERSION_IDENTIFIER=PASS",
        "BUNDLE_PAYLOAD_SECURITY=PASS",
        "RC_BUNDLE_VERIFY=PASS",
    ):
        print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
