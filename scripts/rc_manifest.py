#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


APP_VERSION = "0.1.0"
RC_LABEL = "0.1.0-rc.1"
MAC_APP_NAME = "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.app.tar.gz"
MAC_DMG_NAME = "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.dmg"
WINDOWS_NSIS_NAME = "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
EXPECTED_NAMES = {MAC_APP_NAME, MAC_DMG_NAME, WINDOWS_NSIS_NAME}
REQUIRED_ARTIFACT_FIELDS = {
    "platform",
    "architecture",
    "artifact_name",
    "artifact_size",
    "sha256",
    "built_sidecar_smoke",
    "packaged_app_smoke",
    "built_at",
}


class ManifestError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ManifestError("ARTIFACT_UNREADABLE") from error
    return digest.hexdigest()


def _validate_ascii_name(name: str) -> None:
    if name not in EXPECTED_NAMES or not name.isascii() or Path(name).name != name:
        raise ManifestError("ARTIFACT_NAME_INVALID")


def _artifact_entry(
    path: Path,
    platform: str,
    architecture: str,
    built_sidecar_smoke: str,
    packaged_app_smoke: str,
    built_at: str,
) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ManifestError("ARTIFACT_MISSING")
    _validate_ascii_name(path.name)
    size = path.stat().st_size
    if size <= 0:
        raise ManifestError("ARTIFACT_EMPTY")
    return {
        "platform": platform,
        "architecture": architecture,
        "artifact_name": path.name,
        "artifact_size": size,
        "sha256": _sha256(path),
        "built_sidecar_smoke": built_sidecar_smoke,
        "packaged_app_smoke": packaged_app_smoke,
        "built_at": built_at,
    }


def _validate_platform_values(platform: str, architecture: str, artifact_names: set[str]) -> None:
    expected = {
        ("macos", "arm64"): {MAC_APP_NAME, MAC_DMG_NAME},
        ("windows", "x64"): {WINDOWS_NSIS_NAME},
    }.get((platform, architecture))
    if expected is None:
        raise ManifestError("PLATFORM_ARCHITECTURE_INVALID")
    if artifact_names != expected:
        raise ManifestError("PLATFORM_ARTIFACT_SET_INVALID")


def create_platform_manifest(
    artifacts: list[Path],
    output: Path,
    platform: str,
    architecture: str,
    git_commit: str,
    built_sidecar_smoke: str,
    packaged_app_smoke: str,
    toolchain: dict[str, str],
    workflow: dict[str, str],
    built_at: str,
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        raise ManifestError("COMMIT_INVALID")
    if built_sidecar_smoke != "PASS":
        raise ManifestError("BUILT_SIDECAR_SMOKE_INVALID")
    if packaged_app_smoke not in {"PASS", "NOT_RUN"}:
        raise ManifestError("PACKAGED_APP_SMOKE_INVALID")
    if set(toolchain) != {"node", "pnpm", "python", "rustc"} or not all(toolchain.values()):
        raise ManifestError("TOOLCHAIN_INVALID")
    if set(workflow) != {"repository", "run_id", "run_attempt"} or not all(workflow.values()):
        raise ManifestError("WORKFLOW_INVALID")
    names = {path.name for path in artifacts}
    if len(names) != len(artifacts):
        raise ManifestError("ARTIFACT_DUPLICATE")
    _validate_platform_values(platform, architecture, names)
    entries = [
        _artifact_entry(
            path,
            platform,
            architecture,
            built_sidecar_smoke,
            packaged_app_smoke,
            built_at,
        )
        for path in sorted(artifacts, key=lambda value: value.name)
    ]
    payload: dict[str, object] = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "rc_label": RC_LABEL,
        "git_commit": git_commit,
        "signed": False,
        "notarized": False,
        "real_provider_smoke": "NOT_RUN",
        "image_input": "NOT_RUN",
        "platform": platform,
        "architecture": architecture,
        "built_sidecar_smoke": built_sidecar_smoke,
        "packaged_app_smoke": packaged_app_smoke,
        "toolchain": toolchain,
        "workflow": workflow,
        "built_at": built_at,
        "artifacts": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_checksums(entries, output.with_name(f"SHA256SUMS-{platform}.txt"))
    return payload


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError("MANIFEST_INVALID") from error
    if not isinstance(payload, dict):
        raise ManifestError("MANIFEST_INVALID")
    required = {
        "schema_version",
        "app_version",
        "rc_label",
        "git_commit",
        "signed",
        "notarized",
        "real_provider_smoke",
        "image_input",
        "artifacts",
    }
    if not required.issubset(payload):
        raise ManifestError("MANIFEST_FIELD_MISSING")
    if payload["schema_version"] != 1 or payload["app_version"] != APP_VERSION:
        raise ManifestError("MANIFEST_VALUE_INVALID")
    if payload["rc_label"] != RC_LABEL or payload["signed"] is not False:
        raise ManifestError("MANIFEST_VALUE_INVALID")
    if payload["notarized"] is not False or payload["real_provider_smoke"] != "NOT_RUN":
        raise ManifestError("MANIFEST_VALUE_INVALID")
    if payload["image_input"] != "NOT_RUN" or not re.fullmatch(
        r"[0-9a-f]{40}", str(payload["git_commit"])
    ):
        raise ManifestError("MANIFEST_VALUE_INVALID")
    if not isinstance(payload["artifacts"], list) or not payload["artifacts"]:
        raise ManifestError("MANIFEST_ARTIFACTS_INVALID")
    return payload


def _artifact_path(root: Path, name: str) -> Path:
    _validate_ascii_name(name)
    candidates = [path for path in root.rglob(name) if path.is_file() and not path.is_symlink()]
    if len(candidates) != 1:
        raise ManifestError("ARTIFACT_MISSING" if not candidates else "ARTIFACT_DUPLICATE")
    return candidates[0]


def _validate_entry(entry: object, root: Path) -> tuple[dict[str, object], Path]:
    if not isinstance(entry, dict) or not REQUIRED_ARTIFACT_FIELDS.issubset(entry):
        raise ManifestError("MANIFEST_FIELD_MISSING")
    name = entry["artifact_name"]
    if not isinstance(name, str):
        raise ManifestError("ARTIFACT_NAME_INVALID")
    path = _artifact_path(root, name)
    if entry["sha256"] != _sha256(path):
        raise ManifestError("ARTIFACT_CHECKSUM_MISMATCH")
    if entry["artifact_size"] != path.stat().st_size:
        raise ManifestError("ARTIFACT_SIZE_MISMATCH")
    if entry["built_sidecar_smoke"] != "PASS":
        raise ManifestError("BUILT_SIDECAR_SMOKE_INVALID")
    if entry["packaged_app_smoke"] not in {"PASS", "NOT_RUN"}:
        raise ManifestError("PACKAGED_APP_SMOKE_INVALID")
    return entry, path


def _write_checksums(entries: Iterable[dict[str, object]], output: Path) -> None:
    lines = [f"{entry['sha256']}  {entry['artifact_name']}" for entry in entries]
    output.write_text("\n".join(lines) + "\n", encoding="ascii")


def combine_manifests(
    manifest_paths: list[Path], artifact_root: Path, output_dir: Path
) -> dict[str, object]:
    if not manifest_paths:
        raise ManifestError("PLATFORM_MANIFEST_MISSING")
    payloads = [_read_manifest(path) for path in manifest_paths]
    entries_and_paths: list[tuple[dict[str, object], Path]] = []
    commits: set[str] = set()
    for payload in payloads:
        commits.add(str(payload["git_commit"]))
        entries_and_paths.extend(
            _validate_entry(entry, artifact_root) for entry in payload["artifacts"]  # type: ignore[arg-type]
        )
    if len(commits) != 1:
        raise ManifestError("COMMIT_MISMATCH")
    entries = [entry for entry, _ in entries_and_paths]
    names = [str(entry["artifact_name"]) for entry in entries]
    if len(names) != len(set(names)):
        raise ManifestError("ARTIFACT_DUPLICATE")
    if set(names) != EXPECTED_NAMES:
        raise ManifestError("COMBINED_ARTIFACT_SET_INVALID")
    entries_and_paths.sort(key=lambda item: str(item[0]["artifact_name"]))
    output_dir.mkdir(parents=True, exist_ok=False)
    for entry, source in entries_and_paths:
        shutil.copy2(source, output_dir / str(entry["artifact_name"]))
    combined: dict[str, object] = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "rc_label": RC_LABEL,
        "git_commit": next(iter(commits)),
        "signed": False,
        "notarized": False,
        "real_provider_smoke": "NOT_RUN",
        "image_input": "NOT_RUN",
        "artifacts": [entry for entry, _ in entries_and_paths],
        "platform_evidence": [
            {
                "platform": payload.get("platform"),
                "architecture": payload.get("architecture"),
                "toolchain": payload.get("toolchain"),
                "workflow": payload.get("workflow"),
            }
            for payload in payloads
        ],
    }
    manifest_path = output_dir / "BUILD-MANIFEST.json"
    manifest_path.write_text(
        json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_checksums(combined["artifacts"], output_dir / "SHA256SUMS.txt")  # type: ignore[arg-type]
    return combined


def verify_combined_manifest(manifest_path: Path, artifact_root: Path) -> dict[str, object]:
    payload = _read_manifest(manifest_path)
    entries = []
    names: list[str] = []
    for candidate in payload["artifacts"]:  # type: ignore[union-attr]
        entry, _ = _validate_entry(candidate, artifact_root)
        entries.append(entry)
        names.append(str(entry["artifact_name"]))
    if len(names) != len(set(names)) or set(names) != EXPECTED_NAMES:
        raise ManifestError("COMBINED_ARTIFACT_SET_INVALID")
    checksum_path = manifest_path.with_name("SHA256SUMS.txt")
    if checksum_path.is_file():
        expected = "\n".join(
            f"{entry['sha256']}  {entry['artifact_name']}"
            for entry in sorted(entries, key=lambda value: str(value["artifact_name"]))
        ) + "\n"
        try:
            actual = checksum_path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise ManifestError("CHECKSUM_FILE_INVALID") from error
        if actual != expected:
            raise ManifestError("CHECKSUM_FILE_MISMATCH")
    return payload


def _command_output(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ManifestError("TOOLCHAIN_CAPTURE_FAILED")
    return value


def _local_metadata() -> tuple[str, dict[str, str], dict[str, str], str]:
    commit = _command_output(["git", "rev-parse", "HEAD"])
    toolchain = {
        "node": _command_output(["node", "--version"]),
        "pnpm": _command_output(["pnpm", "--version"]),
        "python": sys.version.split()[0],
        "rustc": _command_output(["rustc", "--version"]),
    }
    workflow = {
        "repository": os.environ.get("GITHUB_REPOSITORY", "local/local"),
        "run_id": os.environ.get("GITHUB_RUN_ID", "0"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "0"),
    }
    built_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return commit, toolchain, workflow, built_at


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and verify RC artifact evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-platform")
    create.add_argument("--platform", choices=("macos", "windows"), required=True)
    create.add_argument("--architecture", choices=("arm64", "x64"), required=True)
    create.add_argument("--artifact", type=Path, action="append", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--built-sidecar-smoke", choices=("PASS",), required=True)
    create.add_argument("--packaged-app-smoke", choices=("PASS", "NOT_RUN"), required=True)
    combine = commands.add_parser("combine")
    combine.add_argument("--manifest", type=Path, action="append", required=True)
    combine.add_argument("--artifact-root", type=Path, required=True)
    combine.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--artifact-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "create-platform":
            commit, toolchain, workflow, built_at = _local_metadata()
            create_platform_manifest(
                args.artifact,
                args.output,
                args.platform,
                args.architecture,
                commit,
                args.built_sidecar_smoke,
                args.packaged_app_smoke,
                toolchain,
                workflow,
                built_at,
            )
        elif args.command == "combine":
            combine_manifests(args.manifest, args.artifact_root, args.output_dir)
        else:
            verify_combined_manifest(args.manifest, args.artifact_root)
    except ManifestError as error:
        print(f"RC_MANIFEST=FAIL:{error.code}", file=sys.stderr)
        return 1
    print("RC_MANIFEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
