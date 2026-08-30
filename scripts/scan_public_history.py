#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import unicodedata
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
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ScanError("GIT_LAUNCH_FAILED") from error
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
    try:
        inside_work_tree = _git(repo, "rev-parse", "--is-inside-work-tree").strip()
        is_bare_repository = _git(repo, "rev-parse", "--is-bare-repository").strip()
    except ScanError as error:
        if error.reason == "GIT_COMMAND_FAILED":
            raise ScanError("NOT_GIT_REPOSITORY") from error
        raise
    if inside_work_tree != b"true" and is_bare_repository != b"true":
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
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in path):
        return "<unsafe-path>"
    if any(pattern.search(raw_path) for pattern in PATTERNS.values()):
        return "<unsafe-path>"
    return path


def _matches(content: bytes) -> list[str]:
    return [name for name, pattern in PATTERNS.items() if pattern.search(content)]


def _decode_oid(oid: bytes) -> str:
    try:
        oid_text = oid.decode("ascii")
    except UnicodeDecodeError as error:
        raise ScanError("OBJECT_ID_DECODE_FAILED") from error
    if len(oid_text) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in oid_text
    ):
        raise ScanError("OBJECT_ID_INVALID")
    return oid_text


def _history_revisions(repo: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ScanError("GIT_LAUNCH_FAILED") from error
    if completed.returncode == 1:
        return ["--all"]
    if completed.returncode:
        raise ScanError("GIT_COMMAND_FAILED")
    return ["--all", _decode_oid(completed.stdout.strip())]


def _object_paths(repo: Path) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}
    for line in _git(
        repo, "rev-list", "--objects", "--missing=error", *_history_revisions(repo)
    ).splitlines():
        oid, separator, path = line.partition(b" ")
        if not separator and not oid:
            raise ScanError("OBJECT_ENUMERATION_FAILED")
        objects.setdefault(_decode_oid(oid), path)
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
    for oid_bytes in _git(repo, "rev-list", "--missing=error", *_history_revisions(repo)).splitlines():
        oid = _decode_oid(oid_bytes)
        commit = _git(repo, "cat-file", "commit", oid)
        _, separator, message = commit.partition(b"\n\n")
        if not separator:
            raise ScanError("OBJECT_READ_FAILED")
        for name in _matches(message):
            findings.append((name, oid[:12], "COMMIT_MESSAGE"))
    return findings


def _tag_target(tag: bytes) -> str:
    headers, separator, _ = tag.partition(b"\n\n")
    if not separator:
        raise ScanError("OBJECT_READ_FAILED")
    for header in headers.splitlines():
        if header.startswith(b"object "):
            return _decode_oid(header[7:])
    raise ScanError("OBJECT_READ_FAILED")


def _reachable_annotated_tags(repo: Path) -> list[tuple[str, bytes]]:
    roots = _git(repo, "for-each-ref", "--format=%(objectname)", "refs/").splitlines()
    pending = [_decode_oid(root) for root in roots]
    visited: set[str] = set()
    tags: list[tuple[str, bytes]] = []
    while pending:
        oid = pending.pop()
        if oid in visited:
            continue
        visited.add(oid)
        if _git(repo, "cat-file", "-t", oid).strip() != b"tag":
            continue
        tag = _git(repo, "cat-file", "tag", oid)
        tags.append((oid, tag))
        pending.append(_tag_target(tag))
    return tags


def _annotated_tag_findings(repo: Path) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    for oid, tag in _reachable_annotated_tags(repo):
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
