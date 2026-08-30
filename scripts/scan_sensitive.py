#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sensitive_patterns import PATTERNS, is_binary_content


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[str, str]] = []
    for path in tracked_files():
        try:
            content = path.read_bytes()
        except (OSError, IsADirectoryError):
            continue
        if is_binary_content(content):
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((name, str(path.relative_to(ROOT))))
    if findings:
        for name, path in findings:
            print(f"SENSITIVE_SCAN_FAIL={name}:{path}", file=sys.stderr)
        return 1
    print("SENSITIVE_SCAN=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
