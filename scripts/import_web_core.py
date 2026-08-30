#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

PINNED_WEB_SHA = "0d8c86d8c12a3740cbc526806826f35498436201"

FILES = (
    "database.py",
    "settings.py",
    "jobs/processing.py",
    "security/uploads.py",
    "security/masking.py",
    "security/local_redaction.py",
    "ai/schemas.py",
    "ai/providers.py",
    "services/analyses.py",
    "services/assessment_gate.py",
    "services/uploads.py",
    "services/risk_evaluation.py",
    "services/deletion.py",
    "services/dashboard.py",
)

DIRECTORIES = (
    "domain",
    "models",
    "storage",
    "parsers",
    "matching",
    "rules",
)

FORBIDDEN_RUNTIME_PATHS = (
    "jobs/queue.py",
    "worker.py",
    "security/access_session.py",
    "main.py",
)


def git_head(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def copy_file(source_root: Path, destination_root: Path, relative: str) -> None:
    source = source_root / relative
    if not source.is_file():
        raise FileNotFoundError(f"WEB_CORE_SOURCE_MISSING:{relative}")
    target = destination_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_directory(source_root: Path, destination_root: Path, relative: str) -> None:
    source = source_root / relative
    if not source.is_dir():
        raise FileNotFoundError(f"WEB_CORE_SOURCE_MISSING:{relative}")
    target = destination_root / relative
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def import_core(web_checkout: Path, desktop_root: Path) -> None:
    if git_head(web_checkout) != PINNED_WEB_SHA:
        raise RuntimeError("WEB_BASELINE_SHA_MISMATCH")

    source_root = web_checkout / "apps" / "api" / "src" / "qian_labor"
    destination_root = desktop_root / "python" / "src" / "qian_labor"

    for relative in FILES:
        copy_file(source_root, destination_root, relative)
    for relative in DIRECTORIES:
        copy_directory(source_root, destination_root, relative)

    for relative in FORBIDDEN_RUNTIME_PATHS:
        if (destination_root / relative).exists():
            raise RuntimeError(f"WEB_SERVER_RUNTIME_COPIED:{relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("web_checkout", type=Path)
    parser.add_argument(
        "--desktop-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    import_core(args.web_checkout.resolve(), args.desktop_root.resolve())
    print(f"WEB_CORE_IMPORTED={PINNED_WEB_SHA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
