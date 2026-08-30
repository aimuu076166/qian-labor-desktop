from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "scan_public_history.py"
CURRENT_SCRIPT = ROOT / "scripts" / "scan_sensitive.py"


def _credential() -> str:
    return "sk-" + ("A" * 24)


def _git(repo: Path, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input, check=True, capture_output=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Scanner Test")
    return repo


def _commit(repo: Path, path: str, content: str, message: str = "commit") -> None:
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-q", "-m", message)


def _scan(repo: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )


def _load(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_safe_output(result: subprocess.CompletedProcess[str], secret: str) -> None:
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "\x1b" not in result.stdout
    assert "\x1b" not in result.stderr


def test_clean_complete_history_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "notes.txt", "ordinary content")

    result = _scan(repo)

    assert result.returncode == 0
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=PASS\n"
    assert result.stderr == ""


def test_credential_in_committed_tree_is_reported_without_value(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "settings.txt", "key=" + secret)
    oid = _git(repo, "hash-object", "settings.txt").stdout.decode().strip()[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{oid}:settings.txt\n"
    _assert_safe_output(result, secret)


def test_deleted_credential_remains_reported_from_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "removed.txt", secret, "add temporary setting")
    _commit(repo, "removed.txt", "removed", "remove temporary setting")

    result = _scan(repo)

    assert result.returncode == 1
    assert "OPENAI_STYLE_API_KEY" in result.stderr
    _assert_safe_output(result, secret)


def test_credential_on_secondary_ref_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "main.txt", "clean")
    _git(repo, "checkout", "-q", "-b", "secondary")
    secret = _credential()
    _commit(repo, "secondary.txt", secret)
    _git(repo, "checkout", "-q", "-")

    result = _scan(repo)

    assert result.returncode == 1
    assert "secondary.txt" in result.stderr
    _assert_safe_output(result, secret)


def test_credential_in_commit_message_is_reported_without_value(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "clean.txt", "clean", "message " + secret)
    oid = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{oid}:COMMIT_MESSAGE\n"
    _assert_safe_output(result, secret)


def test_credential_in_annotated_tag_message_is_reported_without_value(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "clean.txt", "clean")
    secret = _credential()
    _git(repo, "tag", "-a", "v1", "-m", "release " + secret)
    tag_oid = _git(repo, "rev-parse", "v1^{tag}").stdout.decode().strip()[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{tag_oid}:ANNOTATED_TAG_MESSAGE\n"
    _assert_safe_output(result, secret)


def test_placeholder_and_synthetic_assignments_are_permitted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    content = "\n".join(
        [
            "AI_API_KEY=<your-api-key>",
            "AI_API_KEY=${AI_API_KEY}",
            "AI_API_KEY=YOUR_API_KEY",
            "AI_API_KEY=synthetic-example-value-that-is-long-enough",
        ]
    )
    _commit(repo, "example.env", content)

    result = _scan(repo)

    assert result.returncode == 0
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=PASS\n"


def test_binary_blob_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential().encode()
    path = repo / "asset.bin"
    path.write_bytes(b"\0" + secret)
    _git(repo, "add", "--", "asset.bin")
    _git(repo, "commit", "-q", "-m", "binary")

    result = _scan(repo)

    assert result.returncode == 0
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=PASS\n"
    _assert_safe_output(result, secret.decode())


def test_duplicate_blob_is_scanned_and_reported_at_most_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "one.txt", secret)
    os.link(repo / "one.txt", repo / "two.txt")
    _git(repo, "add", "--", "two.txt")
    _git(repo, "commit", "-q", "-m", "duplicate blob")

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr.count("OPENAI_STYLE_API_KEY") == 1
    _assert_safe_output(result, secret)


def test_shallow_repository_fails_with_stable_reason(tmp_path: Path) -> None:
    source_parent = tmp_path / "source-parent"
    source_parent.mkdir()
    source = _repo(source_parent)
    _commit(source, "clean.txt", "clean")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{source}", str(shallow)],
        check=True,
        capture_output=True,
    )

    result = _scan(shallow)

    assert result.returncode == 2
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=SHALLOW_REPOSITORY\n"
    assert result.stderr == ""


@pytest.mark.parametrize("kind", ["not-a-repository", "missing"])
def test_invalid_repository_input_fails_without_path_leaks(tmp_path: Path, kind: str) -> None:
    secret = _credential()
    if kind == "not-a-repository":
        repo = tmp_path / ("not-a-repo-" + secret)
        repo.mkdir()
        expected = "NOT_GIT_REPOSITORY"
    else:
        repo = tmp_path / ("missing-" + secret)
        expected = "REPOSITORY_NOT_FOUND"

    result = _scan(repo)

    assert result.returncode == 2
    assert result.stdout == f"PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON={expected}\n"
    assert result.stderr == ""
    _assert_safe_output(result, secret)


def test_unsafe_filename_never_leaks_secret_or_control_character(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    filename = "file-" + secret + "-\x1b[31m.txt"
    _commit(repo, filename, secret)

    result = _scan(repo)

    assert result.returncode == 1
    assert "<unsafe-path>" in result.stderr
    _assert_safe_output(result, secret)


def test_current_and_history_scanners_expose_identical_pattern_names() -> None:
    current = _load(CURRENT_SCRIPT, "qian_current_sensitive_scan")
    history = _load(SCRIPT, "qian_public_history_scan")

    assert tuple(current.PATTERNS) == tuple(history.PATTERNS)
