#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "OPENAI_STYLE_API_KEY": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GITHUB_CLASSIC_TOKEN": re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    "GITHUB_FINE_GRAINED_TOKEN": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "GOOGLE_API_KEY": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "AWS_ACCESS_KEY_ID": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "ZHIPU_KEY_ASSIGNMENT": re.compile(
        rb"(?im)^\s*(?:export\s+)?(?:AI_API_KEY|ZAI_API_KEY|ZHIPU_API_KEY|BIGMODEL_API_KEY)\s*=\s*(?!<|\$\{|YOUR_|replace|example)([^\s#]{16,})"
    ),
}


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
        if b"\0" in content[:4096]:
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
