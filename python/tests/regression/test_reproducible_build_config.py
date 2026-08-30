from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "apps" / "desktop" / "src-tauri" / "Cargo.toml"
LOCKFILE = MANIFEST.with_name("Cargo.lock")
TOOLCHAIN = ROOT / "rust-toolchain.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-ci.yml"
README = ROOT / "README.md"


@dataclass
class WorkflowStep:
    name: str | None = None
    run: str | None = None
    uses: str | None = None
    if_condition: str | None = None
    with_values: dict[str, str | None] = field(default_factory=dict)
    env: dict[str, str | None] = field(default_factory=dict)


@dataclass
class WorkflowJob:
    runs_on: str | None = None
    if_condition: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)


def _key_value(line: str) -> tuple[str, str | None]:
    match = re.fullmatch(r"([A-Za-z0-9_-]+):(.*)", line)
    assert match is not None, f"unsupported workflow mapping: {line!r}"
    return match.group(1), _yaml_scalar(match.group(2))


def _yaml_scalar(raw: str) -> str | None:
    value = raw.strip()
    if not value or value.startswith("#"):
        return None
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        elif character == "#" and quote is None and index and value[index - 1].isspace():
            value = value[:index].rstrip()
            break
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _workflow_jobs(workflow: str | None = None) -> dict[str, WorkflowJob]:
    """Parse only the job/step mapping fields used by this fixed CI workflow."""
    lines = (WORKFLOW.read_text(encoding="utf-8") if workflow is None else workflow).splitlines()
    jobs_start = next(index for index, line in enumerate(lines) if line == "jobs:")
    jobs: dict[str, WorkflowJob] = {}
    current: WorkflowJob | None = None
    section: str | None = None
    nested: str | None = None
    step: WorkflowStep | None = None
    block_target: tuple[WorkflowJob | WorkflowStep, str, int] | None = None
    block_lines: list[str] = []

    def assign_value(target: WorkflowJob | WorkflowStep, key: str, value: str | None) -> None:
        attribute = {"if": "if_condition", "runs-on": "runs_on", "with": "with_values"}.get(
            key, key
        )
        assert hasattr(target, attribute), f"unsupported workflow field: {key!r}"
        setattr(target, attribute, value)

    def finish_block() -> None:
        nonlocal block_target, block_lines
        assert block_target is not None
        target, attribute, _ = block_target
        nonblank = [line for line in block_lines if line.strip()]
        if nonblank:
            baseline = min(len(line) - len(line.lstrip(" ")) for line in nonblank)
            value = "\n".join(line[baseline:] for line in block_lines).rstrip()
        else:
            value = ""
        setattr(target, attribute, value)
        block_target = None
        block_lines = []

    for line in lines[jobs_start + 1 :]:
        indentation = len(line) - len(line.lstrip(" "))
        if block_target is not None:
            _, _, block_indentation = block_target
            if not line.strip():
                block_lines.append("")
                continue
            if indentation > block_indentation:
                block_lines.append(line)
                continue
            finish_block()
        if not line.strip():
            continue
        match = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_-]*):", line)
        if match:
            current = WorkflowJob()
            jobs[match.group(1)] = current
            section = nested = None
            step = None
            continue
        if current is None:
            continue
        content = line.strip()
        if indentation == 4:
            key, value = _key_value(content)
            section = key
            nested = None
            step = None
            if key in {"runs-on", "if"}:
                attribute = "if_condition" if key == "if" else key
                if value in {"|", ">", "|-", ">-"}:
                    block_target = (current, attribute, indentation)
                else:
                    assign_value(current, key, value)
            continue
        if indentation == 6 and section == "env" and step is None:
            key, value = _key_value(content)
            current.env[key] = value
            continue
        if indentation == 6 and section == "steps" and content.startswith("- "):
            step = WorkflowStep()
            current.steps.append(step)
            nested = None
            key, value = _key_value(content[2:])
            attribute = "if_condition" if key == "if" else key
            if value in {"|", ">", "|-", ">-"}:
                block_target = (step, attribute, indentation)
            else:
                assign_value(step, key, value)
            continue
        if indentation == 8 and step is not None:
            key, value = _key_value(content)
            if key in {"with", "env"}:
                nested = key
            else:
                attribute = "if_condition" if key == "if" else key
                if value in {"|", ">", "|-", ">-"}:
                    block_target = (step, attribute, indentation)
                else:
                    assign_value(step, key, value)
                nested = None
            continue
        if indentation == 10 and step is not None and nested is not None:
            key, value = _key_value(content)
            target = step.with_values if nested == "with" else step.env
            target[key] = value
    if block_target is not None:
        finish_block()
    return jobs


def _exact_run(steps: list[WorkflowStep], command: str) -> tuple[int, WorkflowStep]:
    matching = [(index, step) for index, step in enumerate(steps) if step.run == command]
    assert len(matching) == 1, f"expected one run command {command!r}, got {matching!r}"
    return matching[0]


def _exact_uses(steps: list[WorkflowStep], action: str) -> tuple[int, WorkflowStep]:
    matching = [(index, step) for index, step in enumerate(steps) if step.uses == action]
    assert len(matching) == 1, f"expected one action {action!r}, got {matching!r}"
    return matching[0]


def _shell_command_segments(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\" and quote != "'":
            current.append(character)
            escaped = True
        elif character in {"'", '"'}:
            current.append(character)
            quote = None if quote == character else character if quote is None else quote
        elif quote is None and character in {"\n", ";"}:
            if "".join(current).strip():
                segments.append("".join(current))
            current = []
        elif quote is None and character in {"&", "|"} and index + 1 < len(command) and command[index + 1] == character:
            if "".join(current).strip():
                segments.append("".join(current))
            current = []
            index += 1
        elif quote is None and character == "|":
            if "".join(current).strip():
                segments.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1
    if "".join(current).strip():
        segments.append("".join(current))
    return segments


def _direct_cargo_invocations(command: str | None) -> list[tuple[str, list[str]]]:
    if command is None:
        return []
    invocations: list[tuple[str, list[str]]] = []
    for segment in _shell_command_segments(command):
        words = shlex.split(segment, comments=True)
        if len(words) >= 2 and words[0] == "cargo":
            invocations.append((words[1], words[2:]))
    return invocations


def _has_no_env_or_secret_context(job: WorkflowJob) -> bool:
    values: list[str | None] = [job.if_condition, *job.env.values()]
    if job.env:
        return False
    for step in job.steps:
        if step.env:
            return False
        values.extend([step.if_condition, step.run, step.uses, *step.with_values.values()])
    return not any(
        value is not None
        and re.search(r"\$\{\{\s*secrets(?:\.|\s*\[\s*['\"])", value, flags=re.IGNORECASE)
        for value in values
    )


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_git_ignore_check_detects_an_ignored_tracked_lockfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".gitignore").write_text("Cargo.lock\n", encoding="utf-8")
    (repo / "Cargo.lock").write_text("lockfile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-f", "Cargo.lock"], check=True)

    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "check-ignore",
            "--no-index",
            "--quiet",
            "--",
            "Cargo.lock",
        ],
        check=False,
    )

    assert ignored.returncode == 0, "the ignored tracked lockfile must be detected"


def test_desktop_rust_release_inputs_are_tracked_and_resolvable() -> None:
    lockfile_relative = LOCKFILE.relative_to(ROOT).as_posix()

    assert LOCKFILE.is_file(), "desktop Cargo.lock must be committed"

    ignored = _git("check-ignore", "--no-index", "--quiet", "--", lockfile_relative)
    assert ignored.returncode == 1, "desktop Cargo.lock must not be ignored"

    tracked = _git("ls-files", "--error-unmatch", "--", lockfile_relative)
    assert tracked.returncode == 0, tracked.stderr

    metadata = subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(MANIFEST),
            "--locked",
            "--no-deps",
            "--format-version=1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert metadata.returncode == 0, metadata.stderr
    packages = json.loads(metadata.stdout)["packages"]
    assert any(
        package["name"] == "qian-labor-desktop"
        and Path(package["manifest_path"]).resolve() == MANIFEST.resolve()
        for package in packages
    )

    assert TOOLCHAIN.is_file(), "rust-toolchain.toml must pin the release toolchain"
    toolchain = tomllib.loads(TOOLCHAIN.read_text(encoding="utf-8"))["toolchain"]
    assert toolchain["channel"] == "1.98.0"
    assert toolchain["profile"] == "minimal"
    assert toolchain["components"] == ["rustfmt"]


def test_desktop_ci_enforces_locked_builds_and_complete_history_security() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read\n" in workflow
    jobs = _workflow_jobs(workflow)
    assert set(jobs) == {
        "frontend",
        "python-sidecar",
        "tauri-rust",
        "macos-arm64-build",
        "windows-x64-build",
        "public-history-security",
    }

    history = jobs["public-history-security"]
    assert history.runs_on == "ubuntu-24.04"
    history_steps = history.steps
    _, checkout = _exact_uses(history_steps, "actions/checkout@v4")
    assert checkout.with_values == {"fetch-depth": "0", "persist-credentials": "false"}
    _, python_setup = _exact_uses(history_steps, "actions/setup-python@v5")
    assert python_setup.with_values == {"python-version": "3.12"}
    install_index, _ = _exact_run(history_steps, "python -m pip install -e './python[test]'")
    pytest_index, _ = _exact_run(
        history_steps, "python -m pytest -q python/tests/regression/test_public_history_scan.py"
    )
    assert install_index < pytest_index
    _exact_run(history_steps, "python scripts/scan_public_history.py --repo .")
    assert _has_no_env_or_secret_context(history)

    sidecar_steps = jobs["python-sidecar"].steps
    for command in (
        "python scripts/scan_sensitive.py",
        "pytest python/tests -q",
        "python scripts/verify_desktop.py",
        "python scripts/real_provider_smoke.py",
        "python -m compileall -q python/src python/tests scripts",
    ):
        _exact_run(sidecar_steps, command)
    _, smoke = _exact_run(sidecar_steps, "python scripts/real_provider_smoke.py")
    assert smoke.env == {
        "AI_API_KEY": "",
        "ZAI_API_KEY": "",
        "ZHIPU_API_KEY": "",
        "BIGMODEL_API_KEY": "",
        "REAL_PROVIDER_SMOKE_API_KEY": "",
        "PII_HASH_PEPPER": "",
        "REAL_PROVIDER_SMOKE_PII_HASH_PEPPER": "",
    }

    rust_jobs = ("tauri-rust", "macos-arm64-build", "windows-x64-build")
    for name in rust_jobs:
        steps = jobs[name].steps
        _, toolchain = _exact_uses(steps, "dtolnay/rust-toolchain@stable")
        assert toolchain.with_values == {"toolchain": "1.98.0", "components": "rustfmt"}
        direct = [
            invocation
            for step in steps
            for invocation in _direct_cargo_invocations(step.run)
            if invocation[0] in {"test", "check"}
        ]
        assert direct, f"{name} must execute direct Cargo verification"
        for step in steps:
            for subcommand, arguments in _direct_cargo_invocations(step.run):
                if subcommand in {"test", "check"}:
                    assert "--locked" in arguments, (
                        f"{name} must lock direct Cargo verification: {step.run}"
                    )

    _exact_run(
        jobs["tauri-rust"].steps,
        "cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check",
    )

    for name in ("macos-arm64-build", "windows-x64-build"):
        steps = jobs[name].steps
        package_index, _ = _exact_run(
            steps, "pnpm --dir apps/desktop tauri build --debug --no-bundle -- --locked"
        )
        assert steps[package_index + 1].run == (
            "git diff --exit-code -- apps/desktop/src-tauri/Cargo.lock"
        )


def test_workflow_parser_does_not_treat_step_name_as_a_scanner_command() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    steps:
      - name: python scripts/scan_public_history.py --repo .
"""
    )

    step = jobs["public-history-security"].steps[0]
    assert step.name == "python scripts/scan_public_history.py --repo ."
    assert step.run is None
    assert all(
        candidate.run != "python scripts/scan_public_history.py --repo ."
        for candidate in jobs["public-history-security"].steps
    )


def test_workflow_parser_does_not_treat_echo_as_locked_cargo_execution() -> None:
    assert _direct_cargo_invocations("echo cargo test --locked") == []


def test_workflow_parser_rejects_nonempty_and_comment_only_history_env() -> None:
    for env_value in ("not-empty", "# deliberately omitted"):
        jobs = _workflow_jobs(
            """jobs:
  public-history-security:
    steps:
      - run: python scripts/scan_public_history.py --repo .
        env:
          AI_API_KEY: """
            + env_value
            + "\n"
        )

        assert not _has_no_env_or_secret_context(jobs["public-history-security"])


def test_workflow_parser_rejects_job_level_history_env() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    env:
      AI_API_KEY: nonempty
    steps:
      - run: python scripts/scan_public_history.py --repo .
"""
    )

    assert not _has_no_env_or_secret_context(jobs["public-history-security"])


def test_workflow_parser_rejects_bracket_style_github_secret_context() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    steps:
      - uses: ${{ secrets['PUBLIC_HISTORY_ACTION'] }}
"""
    )

    assert not _has_no_env_or_secret_context(jobs["public-history-security"])


def test_workflow_parser_includes_underscore_job_identifiers() -> None:
    jobs = _workflow_jobs(
        """jobs:
  frontend:
    steps:
      - run: true
  leak_secrets:
    steps:
      - run: true
"""
    )

    assert set(jobs) == {"frontend", "leak_secrets"}


def test_workflow_parser_captures_block_run_for_secret_detection() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    steps:
      - run: |
          echo ${{ secrets["PUBLIC_HISTORY_KEY"] }}
"""
    )

    job = jobs["public-history-security"]
    assert job.steps[0].run == 'echo ${{ secrets["PUBLIC_HISTORY_KEY"] }}'
    assert not _has_no_env_or_secret_context(job)


def test_workflow_parser_captures_block_job_condition() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    if: >-
      github.event_name == 'push'
    steps:
      - run: true
"""
    )

    assert getattr(jobs["public-history-security"], "if_condition", None) == (
        "github.event_name == 'push'"
    )


def test_workflow_parser_rejects_secret_hidden_in_block_job_condition() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    if: >-
      ${{ secrets["PUBLIC_HISTORY_CONDITION"] }}
    steps:
      - run: true
"""
    )

    assert not _has_no_env_or_secret_context(jobs["public-history-security"])


def test_workflow_parser_retains_blank_lines_inside_block_run_secret_context() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    steps:
      - run: |
          echo safe

          ${{ secrets["HIDDEN_AFTER_BLANK"] }}
"""
    )

    job = jobs["public-history-security"]
    assert job.steps[0].run == 'echo safe\n\n${{ secrets["HIDDEN_AFTER_BLANK"] }}'
    assert not _has_no_env_or_secret_context(job)


def test_direct_cargo_parser_keeps_newline_before_echo_locked_separate() -> None:
    invocations = _direct_cargo_invocations("cargo test\necho --locked")

    assert invocations == [("test", [])]


def test_direct_cargo_parser_keeps_newline_separated_unlocked_check() -> None:
    invocations = _direct_cargo_invocations("cargo test --locked\ncargo check")

    assert invocations == [("test", ["--locked"]), ("check", [])]


def test_readme_documents_scoped_locked_dependency_verification() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "Rust 1.98.0 已由 Linux、macOS ARM64 和 Windows x64 CI 验证为当前固定工具链。" in readme
    assert "`Cargo.lock` 已提交；所有 Cargo 验证中的依赖解析均使用 `--locked`，以固定 Cargo 的依赖解析。" in readme
    assert "不表示完整构建或安装包达到逐位可复现，也不表示已经完成签名生产发布。" in readme
    assert "完整抓取的本地 Git 历史" in readme
    assert "本地 HEAD 或任一本地 ref 可达的文本 blob 与提交消息，以及从本地 ref 可达的附注标签消息" in readme
    assert "二进制 blob、未被 ref/HEAD 引用的悬空对象和未抓取到本地的远端历史不在内容匹配范围内" in readme
    for command in (
        "python -m compileall -q python/src python/tests scripts",
        "python scripts/scan_public_history.py --repo .",
        "cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check",
        "cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked",
        "cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked",
    ):
        assert command in readme
