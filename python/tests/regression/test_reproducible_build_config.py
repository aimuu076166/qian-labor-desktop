from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "apps" / "desktop" / "src-tauri" / "Cargo.toml"
LOCKFILE = MANIFEST.with_name("Cargo.lock")
TOOLCHAIN = ROOT / "rust-toolchain.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-ci.yml"
README = ROOT / "README.md"


def _workflow_job_blocks() -> dict[str, str]:
    """Return the YAML job mappings without letting another job satisfy a gate."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    jobs_start = next(index for index, line in enumerate(lines) if line == "jobs:")
    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[jobs_start + 1 :]:
        match = re.fullmatch(r"  ([a-z0-9-]+):", line)
        if match:
            current = match.group(1)
            jobs[current] = [line]
        elif current is not None:
            jobs[current].append(line)
    return {name: "\n".join(block) for name, block in jobs.items()}


def _run_steps(job: str) -> list[str]:
    """Parse each six-space YAML step so ordering checks apply to one job."""
    steps: list[list[str]] = []
    for line in job.splitlines():
        if line.startswith("      - "):
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    return ["\n".join(step) for step in steps]


def _step_containing(steps: list[str], fragment: str) -> tuple[int, str]:
    matching = [(index, step) for index, step in enumerate(steps) if fragment in step]
    assert len(matching) == 1, f"expected one step containing {fragment!r}, got {matching!r}"
    return matching[0]


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
    jobs = _workflow_job_blocks()
    assert set(jobs) == {
        "frontend",
        "python-sidecar",
        "tauri-rust",
        "macos-arm64-build",
        "windows-x64-build",
        "public-history-security",
    }

    history = jobs["public-history-security"]
    assert "    runs-on: ubuntu-24.04" in history
    history_steps = _run_steps(history)
    _, checkout = _step_containing(history_steps, "uses: actions/checkout@v4")
    assert "fetch-depth: 0" in checkout
    assert "persist-credentials: false" in checkout
    _, python_setup = _step_containing(history_steps, "uses: actions/setup-python@v5")
    assert 'python-version: "3.12"' in python_setup
    _step_containing(
        history_steps,
        "python -m pytest -q python/tests/regression/test_public_history_scan.py",
    )
    _step_containing(history_steps, "python scripts/scan_public_history.py --repo .")
    assert "secrets." not in history

    sidecar_steps = _run_steps(jobs["python-sidecar"])
    for command in (
        "python scripts/scan_sensitive.py",
        "pytest python/tests -q",
        "python scripts/verify_desktop.py",
        "python scripts/real_provider_smoke.py",
        "python -m compileall -q python/src python/tests scripts",
    ):
        _step_containing(sidecar_steps, command)
    _, smoke = _step_containing(sidecar_steps, "python scripts/real_provider_smoke.py")
    for key in (
        "AI_API_KEY",
        "ZAI_API_KEY",
        "ZHIPU_API_KEY",
        "BIGMODEL_API_KEY",
        "REAL_PROVIDER_SMOKE_API_KEY",
        "PII_HASH_PEPPER",
        "REAL_PROVIDER_SMOKE_PII_HASH_PEPPER",
    ):
        assert f'{key}: ""' in smoke

    rust_jobs = ("tauri-rust", "macos-arm64-build", "windows-x64-build")
    for name in rust_jobs:
        steps = _run_steps(jobs[name])
        _, toolchain = _step_containing(steps, "uses: dtolnay/rust-toolchain@stable")
        assert "toolchain: 1.98.0" in toolchain
        assert "components: rustfmt" in toolchain
        for step in steps:
            if "cargo test" in step or "cargo check" in step:
                assert "--locked" in step, f"{name} must lock direct Cargo verification: {step}"

    fmt_steps = _run_steps(jobs["tauri-rust"])
    _step_containing(
        fmt_steps,
        "cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check",
    )

    for name in ("macos-arm64-build", "windows-x64-build"):
        steps = _run_steps(jobs[name])
        package_index, package = _step_containing(
            steps, "pnpm --dir apps/desktop tauri build --debug --no-bundle"
        )
        assert "-- --locked" in package
        assert steps[package_index + 1] == (
            "      - run: git diff --exit-code -- apps/desktop/src-tauri/Cargo.lock"
        )


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
