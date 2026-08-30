from __future__ import annotations

import hashlib
import importlib.util
import os
import struct
import subprocess
import sys
import unicodedata
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


def _scan(
    repo: Path | str, *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
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


def _assert_only_ascii_newline_delimiters(value: str) -> None:
    for character in value:
        if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}:
            assert character == "\n"


def _assert_head_is_not_named_by_a_ref(repo: Path) -> str:
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    refs = _git(repo, "for-each-ref", "--format=%(objectname)", "refs/").stdout.splitlines()
    assert head.encode() not in refs
    return head


def _truncate_tip_parent_in_commit_graph(repo: Path, tip_oid: str) -> None:
    _git(repo, "commit-graph", "write", "--reachable")
    graph_path = Path(
        _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects/info/commit-graph",
        )
        .stdout.decode()
        .removesuffix("\n")
    )
    graph_path.chmod(graph_path.stat().st_mode | 0o200)
    data = bytearray(graph_path.read_bytes())
    assert data[:4] == b"CGPH"
    assert data[4] == 1
    hash_version = data[5]
    hash_length = {1: 20, 2: 32}[hash_version]
    chunk_count = data[6]
    chunks: dict[bytes, int] = {}
    for index in range(chunk_count + 1):
        chunk_id, chunk_offset = struct.unpack_from(">4sQ", data, 8 + (index * 12))
        chunks[chunk_id] = chunk_offset

    oid_lookup_offset = chunks[b"OIDL"]
    commit_data_offset = chunks[b"CDAT"]
    commit_count = (commit_data_offset - oid_lookup_offset) // hash_length
    tip_bytes = bytes.fromhex(tip_oid)
    tip_positions = [
        index
        for index in range(commit_count)
        if data[
            oid_lookup_offset + (index * hash_length) :
            oid_lookup_offset + ((index + 1) * hash_length)
        ]
        == tip_bytes
    ]
    assert len(tip_positions) == 1
    parent_offset = (
        commit_data_offset
        + (tip_positions[0] * (hash_length + 16))
        + hash_length
    )
    struct.pack_into(">II", data, parent_offset, 0x70000000, 0x70000000)
    payload = data[:-hash_length]
    digest = hashlib.new(
        "sha1" if hash_version == 1 else "sha256",
        payload,
        usedforsecurity=False,
    ).digest()
    data[-hash_length:] = digest
    graph_path.write_bytes(data)


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


def test_credential_blob_in_unreferenced_detached_head_commit_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "base.txt", "clean")
    _git(repo, "checkout", "-q", "--detach")
    secret = _credential()
    _commit(repo, "detached.txt", secret, "detached content")
    _assert_head_is_not_named_by_a_ref(repo)
    blob_oid = _git(repo, "hash-object", "detached.txt").stdout.decode().strip()[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{blob_oid}:detached.txt\n"
    _assert_safe_output(result, secret)


def test_credential_message_in_unreferenced_detached_head_commit_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "base.txt", "clean")
    _git(repo, "checkout", "-q", "--detach")
    secret = _credential()
    _commit(repo, "detached.txt", "clean", "detached message " + secret)
    commit_oid = _assert_head_is_not_named_by_a_ref(repo)[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{commit_oid}:COMMIT_MESSAGE\n"
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


def test_inherited_git_dir_cannot_redirect_the_scanned_repository(tmp_path: Path) -> None:
    secret_parent = tmp_path / "secret-parent"
    secret_parent.mkdir()
    repo = _repo(secret_parent)
    secret = _credential()
    _commit(repo, "redirected.txt", secret)
    blob_oid = _git(repo, "hash-object", "redirected.txt").stdout.decode().strip()[:12]

    clean_parent = tmp_path / "clean-parent"
    clean_parent.mkdir()
    clean_repo = _repo(clean_parent)
    _commit(clean_repo, "clean.txt", "ordinary content")
    environment = os.environ.copy()
    environment["GIT_DIR"] = str(clean_repo / ".git")

    result = _scan(repo, environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{blob_oid}:redirected.txt\n"
    )
    _assert_safe_output(result, secret)


def test_commit_replacement_cannot_hide_a_credential_message(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "clean.txt", "ordinary content", "secret message " + secret)
    original_commit = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    original_tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    replacement_commit = (
        _git(repo, "commit-tree", original_tree, input=b"safe replacement message\n")
        .stdout.decode()
        .strip()
    )
    _git(repo, "replace", original_commit, replacement_commit)

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:"
        f"{original_commit[:12]}:COMMIT_MESSAGE\n"
    )
    _assert_safe_output(result, secret)


def test_blob_replacement_cannot_hide_a_credential(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "replaced.txt", secret)
    original_blob = _git(repo, "hash-object", "replaced.txt").stdout.decode().strip()
    replacement_blob = (
        _git(repo, "hash-object", "-w", "--stdin", input=b"ordinary content\n")
        .stdout.decode()
        .strip()
    )
    _git(repo, "replace", original_blob, replacement_blob)

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:"
        f"{original_blob[:12]}:replaced.txt\n"
    )
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


def test_credential_in_tag_reachable_only_through_another_tag_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "clean.txt", "clean")
    secret = _credential()
    _git(repo, "tag", "-a", "inner", "-m", "inner " + secret)
    _git(repo, "tag", "-a", "outer", "inner", "-m", "outer")
    inner_oid = _git(repo, "rev-parse", "inner^{tag}").stdout.decode().strip()[:12]
    _git(repo, "update-ref", "-d", "refs/tags/inner")

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{inner_oid}:ANNOTATED_TAG_MESSAGE\n"
    _assert_safe_output(result, secret)


def test_credential_in_tag_under_non_tag_ref_namespace_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "clean.txt", "clean")
    secret = _credential()
    _git(repo, "tag", "-a", "hidden", "-m", "hidden " + secret)
    tag_oid = _git(repo, "rev-parse", "hidden^{tag}").stdout.decode().strip()
    _git(repo, "update-ref", "refs/remotes/origin/hidden-tag", tag_oid)
    _git(repo, "update-ref", "-d", "refs/tags/hidden")

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stderr == (
        f"PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:{tag_oid[:12]}:ANNOTATED_TAG_MESSAGE\n"
    )
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


def test_grafted_ancestry_fails_closed_without_leaking_hidden_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "hidden.txt", secret, "credential-bearing ancestor")
    _commit(repo, "hidden.txt", "ordinary content", "clean tip")
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    grafts_path = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts")
        .stdout.decode()
        .removesuffix("\n")
    )
    grafts_path.parent.mkdir(parents=True, exist_ok=True)
    grafts_path.write_text(head + "\n", encoding="ascii")

    result = _scan(repo)

    assert result.returncode == 2
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=GIT_GRAFTS_PRESENT\n"
    assert result.stderr == ""
    _assert_safe_output(result, secret)


def test_grafts_metadata_check_error_fails_closed_without_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "clean.txt", "ordinary content")
    history = _load(SCRIPT, "qian_public_history_grafts_check_error")
    secret = _credential()

    def deny_metadata_check(path: str) -> os.stat_result:
        raise PermissionError(secret)

    monkeypatch.setattr(history.os, "lstat", deny_metadata_check)

    assert history.main(["--repo", str(repo)]) == 2
    captured = capsys.readouterr()
    assert captured.out == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=GIT_GRAFTS_CHECK_FAILED\n"
    )
    assert captured.err == ""
    _assert_safe_output(
        subprocess.CompletedProcess([], 2, captured.out, captured.err), secret
    )


def test_commit_graph_cannot_truncate_credential_bearing_ancestry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "hidden.txt", secret, "credential-bearing ancestor")
    secret_blob = _git(repo, "hash-object", "hidden.txt").stdout.decode().strip()
    _commit(repo, "hidden.txt", "ordinary content", "clean tip")
    tip_oid = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    _truncate_tip_parent_in_commit_graph(repo, tip_oid)

    graph_enabled = _git(repo, "--no-replace-objects", "rev-list", "--all")
    graph_disabled = _git(
        repo,
        "--no-replace-objects",
        "-c",
        "core.commitGraph=false",
        "rev-list",
        "--all",
    )
    assert len(graph_enabled.stdout.splitlines()) == 1
    assert len(graph_disabled.stdout.splitlines()) == 2

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:"
        f"{secret_blob[:12]}:hidden.txt\n"
    )
    _assert_safe_output(result, secret)


def test_complete_bare_repository_passes(tmp_path: Path) -> None:
    source_parent = tmp_path / "bare-source-parent"
    source_parent.mkdir()
    source = _repo(source_parent)
    _commit(source, "clean.txt", "clean")
    bare = tmp_path / "history.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
    )

    result = _scan(bare)

    assert result.returncode == 0
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=PASS\n"
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


@pytest.mark.parametrize("marker", ["\u0085", "\u202e", "\u2066"])
def test_unicode_control_or_format_filename_is_redacted(tmp_path: Path, marker: str) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    filename = "unicode-" + marker + ".txt"
    _commit(repo, filename, secret)

    result = _scan(repo)

    assert result.returncode == 1
    assert "<unsafe-path>" in result.stderr
    _assert_safe_output(result, secret)
    _assert_only_ascii_newline_delimiters(result.stdout + result.stderr)


@pytest.mark.parametrize("marker", ["\u2028", "\u2029"], ids=["line", "paragraph"])
def test_unicode_line_or_paragraph_separator_filename_is_redacted(
    tmp_path: Path, marker: str
) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    filename = "unicode-" + marker + ".txt"
    _commit(repo, filename, secret)
    blob_oid = _git(repo, "hash-object", filename).stdout.decode().strip()[:12]

    result = _scan(repo)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=OPENAI_STYLE_API_KEY:"
        f"{blob_oid}:<unsafe-path>\n"
    )
    _assert_safe_output(result, secret)
    _assert_only_ascii_newline_delimiters(result.stderr)


def test_git_launch_failure_has_stable_safe_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "clean.txt", "clean")
    secret = _credential()
    environment = {"PATH": "", "SCAN_TEST_MARKER": secret}

    result = _scan(repo, environment=environment)

    assert result.returncode == 2
    assert result.stdout == "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=GIT_LAUNCH_FAILED\n"
    assert result.stderr == ""
    _assert_safe_output(result, secret)


def test_missing_reachable_loose_blob_fails_closed_without_leaks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = _credential()
    _commit(repo, "runtime-setting.txt", "credential=" + secret)
    blob_oid = _git(repo, "hash-object", "runtime-setting.txt").stdout.decode().strip()
    loose_blob = repo / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    assert loose_blob.is_file()
    loose_blob.unlink()

    result = _scan(repo)

    assert result.returncode == 2
    assert result.stdout == (
        "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=GIT_COMMAND_FAILED\n"
    )
    assert "PUBLIC_HISTORY_SENSITIVE_SCAN=PASS" not in result.stdout
    assert result.stderr == ""
    assert blob_oid not in result.stdout
    _assert_safe_output(result, secret)


def test_invalid_object_identifier_bytes_have_stable_safe_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    history = _load(SCRIPT, "qian_public_history_invalid_object")
    secret = _credential()

    monkeypatch.setattr(history, "_validate_repository", lambda repo: None)
    monkeypatch.setattr(history, "_git", lambda repo, *args: b"\xff\n")

    assert history.main(["--repo", "safe-repository"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=OBJECT_ID_DECODE_FAILED\n"
    assert captured.err == ""
    _assert_safe_output(
        subprocess.CompletedProcess([], 2, captured.out, captured.err), secret
    )


def test_ascii_hex_object_identifier_with_invalid_length_has_stable_safe_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    history = _load(SCRIPT, "qian_public_history_invalid_oid_length")
    secret = _credential()

    monkeypatch.setattr(history, "_validate_repository", lambda repo: None)
    monkeypatch.setattr(history, "_git", lambda repo, *args: b"deadbeef\n")

    assert history.main(["--repo", "safe-repository"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "PUBLIC_HISTORY_SENSITIVE_SCAN=FAIL\nREASON=OBJECT_ID_INVALID\n"
    assert captured.err == ""
    _assert_safe_output(
        subprocess.CompletedProcess([], 2, captured.out, captured.err), secret
    )


def test_current_and_history_scanners_expose_identical_pattern_names() -> None:
    current = _load(CURRENT_SCRIPT, "qian_current_sensitive_scan")
    history = _load(SCRIPT, "qian_public_history_scan")

    assert tuple(current.PATTERNS) == tuple(history.PATTERNS)
