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
    with_values: dict[str, str | None] = field(default_factory=dict)
    env: dict[str, str | None] = field(default_factory=dict)


@dataclass
class WorkflowJob:
    runs_on: str | None = None
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
    for line in lines[jobs_start + 1 :]:
        if not line.strip():
            continue
        match = re.fullmatch(r"  ([a-z0-9-]+):", line)
        if match:
            current = WorkflowJob()
            jobs[match.group(1)] = current
            section = nested = None
            step = None
            continue
        if current is None:
            continue
        indentation = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if indentation == 4:
            key, value = _key_value(content)
            section = key
            nested = None
            step = None
            if key == "runs-on":
                current.runs_on = value
            continue
        if indentation == 6 and section == "steps" and content.startswith("- "):
            step = WorkflowStep()
            current.steps.append(step)
            nested = None
            key, value = _key_value(content[2:])
            setattr(step, key if key != "with" else "with_values", value)
            continue
        if indentation == 8 and step is not None:
            key, value = _key_value(content)
            if key in {"with", "env"}:
                nested = key
            else:
                setattr(step, key if key != "with" else "with_values", value)
                nested = None
            continue
        if indentation == 8 and section == "env":
            key, value = _key_value(content)
            current.env[key] = value
            continue
        if indentation == 10 and step is not None and nested is not None:
            key, value = _key_value(content)
            target = step.with_values if nested == "with" else step.env
            target[key] = value
    return jobs


def _exact_run(steps: list[WorkflowStep], command: str) -> tuple[int, WorkflowStep]:
    matching = [(index, step) for index, step in enumerate(steps) if step.run == command]
    assert len(matching) == 1, f"expected one run command {command!r}, got {matching!r}"
    return matching[0]


def _exact_uses(steps: list[WorkflowStep], action: str) -> tuple[int, WorkflowStep]:
    matching = [(index, step) for index, step in enumerate(steps) if step.uses == action]
    assert len(matching) == 1, f"expected one action {action!r}, got {matching!r}"
    return matching[0]


def _direct_cargo_invocations(command: str | None) -> list[tuple[str, list[str]]]:
    if command is None:
        return []
    tokens = shlex.split(command)
    invocations: list[tuple[str, list[str]]] = []
    for segment in re.split(r"(?:&&|\|\||;|\|)", " ".join(tokens)):
        words = shlex.split(segment)
        if len(words) >= 2 and words[0] == "cargo":
            invocations.append((words[1], words[2:]))
    return invocations


def _has_no_env_or_secret_context(job: WorkflowJob) -> bool:
    values: list[str | None] = [*job.env.values()]
    if job.env:
        return False
    for step in job.steps:
        if step.env:
            return False
        values.extend([step.run, step.uses, *step.with_values.values()])
    return not any(
        value is not None and re.search(r"\$\{\{\s*secrets\.", value, flags=re.IGNORECASE)
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


def test_workflow_parser_rejects_bracket_style_github_secret_context() -> None:
    jobs = _workflow_jobs(
        """jobs:
  public-history-security:
    steps:
      - uses: ${{ secrets.PUBLIC_HISTORY_ACTION }}
"""
    )

    assert not _has_no_env_or_secret_context(jobs["public-history-security"])


def test_readme_documents_scoped_locked_dependency_verification() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "Rust 1.98.0 是当前已验证的候选固定工具链，仍待新的三平台 CI 结果确认。" in readme
    assert "`Cargo.lock` 已提交；所有 Cargo 验证中的依赖解析均使用 `--locked`，以固定 Cargo 的依赖解析。" in readme
    assert "不表示完整构建或安装包达到逐位可复现，也不表示已经完成签名生产发布。" in readme
    assert "完整抓取的本地 Git 历史" in readme
    assert "当前所有本地 ref 可达的提交消息、文件对象，以及从这些 ref 可达的附注标签消息" in readme
    assert "未抓取到本地的远端历史不在扫描范围内" in readme

    for command in (
        "python -m compileall -q python/src python/tests scripts",
        "python scripts/scan_public_history.py --repo .",
        "cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check",
        "cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked",
        "cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked",
    ):
        assert command in readme
