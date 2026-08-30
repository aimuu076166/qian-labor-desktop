#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sensitive_patterns import PATTERNS, is_binary_content


class ScanError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise ScanError("GIT_COMMAND_FAILED")
    return completed.stdout


def _parse_repo_argument(argv: list[str]) -> Path:
    if not argv:
        return Path.cwd()
    if len(argv) == 2 and argv[0] == "--repo":
        return Path(argv[1])
    raise ScanError("INVALID_ARGUMENTS")


def _validate_repository(repo: Path) -> None:
    if not repo.exists():
        raise ScanError("REPOSITORY_NOT_FOUND")
    if not repo.is_dir():
        raise ScanError("NOT_GIT_REPOSITORY")
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
    )
    if completed.returncode or completed.stdout.strip() != b"true":
        raise ScanError("NOT_GIT_REPOSITORY")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise ScanError("SHALLOW_REPOSITORY")


def _safe_path(raw_path: bytes) -> str:
    if not raw_path:
        return "<unknown-path>"
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        return "<unsafe-path>"
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "<unsafe-path>"
    if any(pattern.search(raw_path) for pattern in PATTERNS.values()):
        return "<unsafe-path>"
    return path


def _matches(content: bytes) -> list[str]:
    return [name for name, pattern in PATTERNS.items() if pattern.search(content)]


def _object_paths(repo: Path) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}
    for line in _git(repo, "rev-list", "--objects", "--all").splitlines():
        oid, separator, path = line.partition(b" ")
        if not separator and not oid:
            raise ScanError("OBJECT_ENUMERATION_FAILED")
        try:
            oid_text = oid.decode("ascii")
        except UnicodeDecodeError as error:
            raise ScanError("OBJECT_ENUMERATION_FAILED") from error
        if not oid_text or any(character not in "0123456789abcdef" for character in oid_text):
            raise ScanError("OBJECT_ENUMERATION_FAILED")
        objects.setdefault(oid_text, path)
    return objects


def _blob_findings(repo: Path) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    for oid, path in _object_paths(repo).items():
        if _git(repo, "cat-file", "-t", oid).strip() != b"blob":
            continue
        content = _git(repo, "cat-file", "blob", oid)
        if is_binary_content(content):
            continue
        for name in _matches(content):
            findings.append((name, oid[:12], _safe_path(path)))
    return findings


def _commit_findings(repo: Path) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    for oid_bytes in _git(repo, "rev-list", "--all").splitlines():
        oid = oid_bytes.decode("ascii")
        commit = _git(repo, "cat-file", "commit", oid)
        _, separator, message = commit.partition(b"\n\n")
        if not separator:
            raise ScanError("OBJECT_READ_FAILED")
        for name in _matches(message):
            findings.append((name, oid[:12], "COMMIT_MESSAGE"))
    return findings


def _annotated_tag_findings(repo: Path) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    references = _git(repo, "for-each-ref", "--format=%(objecttype)%00%(objectname)", "refs/tags")
    for reference in references.splitlines():
        object_type, separator, oid_bytes = reference.partition(b"\0")
        if not separator or object_type != b"tag":
            continue
        oid = oid_bytes.decode("ascii")
        tag = _git(repo, "cat-file", "tag", oid)
        _, separator, message = tag.partition(b"\n\n")
        if not separator:
            raise ScanError("OBJECT_READ_FAILED")
        for name in _matches(message):
            findings.append((name, oid[:12], "ANNOTATED_TAG_MESSAGE"))
    return findings


def scan_repository(repo: Path) -> list[tuple[str, str, str]]:
    _validate_repository(repo)
    return _blob_findings(repo) + _commit_findings(repo) + _annotated_tag_findings(repo)


def main(argv: list[str] | None = None) -> int:
    try:
        repo = _parse_repo_argument(sys.argv[1:] if argv is None else argv)
        findings = scan_repository(repo)
    except ScanError as error:
        print("PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL")
        print(f"REASON={error.reason}")
        return 2

    if findings:
        for name, oid, location in findings:
            print(f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL={name}:{oid}:{location}", file=sys.stderr)
        return 1
    print("PUBLIC_HISTORY_SENSITIVE_SCAN=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
