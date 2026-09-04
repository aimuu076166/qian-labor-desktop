#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from pathlib import Path

from rc_manifest import MAC_APP_NAME, MAC_DMG_NAME, WINDOWS_NSIS_NAME


class StagingError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _validate_input(path: Path, *, directory: bool) -> Path:
    if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
        raise StagingError("STAGING_INPUT_INVALID")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise StagingError("STAGING_INPUT_INVALID") from error


def _validate_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except OSError as error:
                raise StagingError("STAGING_SYMLINK_INVALID") from error
            if not target.is_relative_to(root):
                raise StagingError("STAGING_SYMLINK_ESCAPE")


def _output_directory(path: Path) -> Path:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise StagingError("STAGING_OUTPUT_NOT_EMPTY")
    else:
        path.mkdir(parents=True)
    return path.resolve()


def stage_macos(app: Path, dmg: Path, output_dir: Path) -> list[Path]:
    application = _validate_input(app, directory=True)
    disk_image = _validate_input(dmg, directory=False)
    if application.suffix != ".app" or disk_image.suffix.lower() != ".dmg":
        raise StagingError("STAGING_INPUT_INVALID")
    _validate_tree(application)
    output = _output_directory(output_dir)
    app_archive = output / MAC_APP_NAME
    with tarfile.open(app_archive, "w:gz", format=tarfile.PAX_FORMAT, dereference=False) as archive:
        archive.add(application, arcname=application.name, recursive=True)
    staged_dmg = output / MAC_DMG_NAME
    shutil.copy2(disk_image, staged_dmg)
    return [app_archive, staged_dmg]


def stage_windows(installer: Path, output_dir: Path) -> list[Path]:
    source = _validate_input(installer, directory=False)
    if source.suffix.lower() != ".exe":
        raise StagingError("STAGING_INPUT_INVALID")
    output = _output_directory(output_dir)
    staged = output / WINDOWS_NSIS_NAME
    shutil.copy2(source, staged)
    return [staged]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage unsigned RC artifacts with ASCII names.")
    commands = parser.add_subparsers(dest="command", required=True)
    mac = commands.add_parser("macos")
    mac.add_argument("--app", type=Path, required=True)
    mac.add_argument("--dmg", type=Path, required=True)
    mac.add_argument("--output-dir", type=Path, required=True)
    windows = commands.add_parser("windows")
    windows.add_argument("--installer", type=Path, required=True)
    windows.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "macos":
            staged = stage_macos(args.app, args.dmg, args.output_dir)
        else:
            staged = stage_windows(args.installer, args.output_dir)
    except StagingError as error:
        print(f"RC_ARTIFACT_STAGING=FAIL:{error.code}", file=sys.stderr)
        return 1
    for path in staged:
        print(path.name)
    print("RC_ARTIFACT_STAGING=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
