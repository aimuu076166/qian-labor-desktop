from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-rc.yml"


def _job_block(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z_][A-Za-z0-9_-]*:\n|\Z)",
        workflow,
    )
    assert match is not None, f"missing job {name}"
    return match.group("body")


def test_rc_workflow_is_read_only_gated_and_cancellable() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "contents: write" not in workflow
    assert "cancel-in-progress: true" in workflow
    assert "release/" in workflow
    assert "secrets." not in workflow
    assert "secrets[" not in workflow
    assert "gh release" not in workflow.lower()
    assert "softprops/action-gh-release" not in workflow
    assert "git tag" not in workflow.lower()


def test_rc_workflow_has_exact_platform_and_manifest_jobs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job_names = re.findall(r"(?m)^  ([A-Za-z_][A-Za-z0-9_-]*):\n", workflow.split("jobs:\n", 1)[1])

    assert job_names == ["macos-arm64-rc", "rc-manifest"]
    assert "runs-on: macos-15" in _job_block(workflow, "macos-arm64-rc")
    assert "runs-on: windows-latest" not in workflow
    assert "needs:\n      - macos-arm64-rc" in _job_block(workflow, "rc-manifest")


def test_rc_platform_jobs_pin_tools_and_execute_real_acceptance() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for name in ("macos-arm64-rc",):
        block = _job_block(workflow, name)
        assert 'CI: "true"' in block
        assert "--remap-path-prefix=" in block
        for required in (
            "actions/checkout@v4",
            "pnpm/action-setup@v4",
            "version: 9.15.0",
            "actions/setup-node@v4",
            "node-version: 22",
            "actions/setup-python@v5",
            'python-version: "3.12"',
            "dtolnay/rust-toolchain@stable",
            "toolchain: 1.98.0",
            "pnpm install --frozen-lockfile",
            "python scripts/verify_desktop.py",
            "python scripts/build_sidecar.py",
            "python scripts/verify_built_sidecar.py",
            "python scripts/verify_rc_bundle.py",
            "python scripts/smoke_packaged_app.py",
            "python scripts/stage_rc_artifacts.py",
            "python scripts/rc_manifest.py create-platform",
            "cargo test --locked",
            "actions/upload-artifact@v4",
            "retention-days: 14",
        ):
            assert required in block, f"{name} missing {required}"

    macos = _job_block(workflow, "macos-arm64-rc")
    assert "-C link-arg=-Wl,-S" in macos
    assert "-C link-arg=-Wl,-x" in macos
    assert "--bundles app,dmg --ci -- --locked" in macos
    assert "--bundles app,dmg --no-sign" not in macos
    assert "--abnormal-lifecycle" in macos
    assert "--bundles nsis" not in workflow


def test_macos_rc_pins_xcode_26_2_sdk() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    macos = _job_block(workflow, "macos-arm64-rc")

    assert "sudo xcode-select -s /Applications/Xcode_26.2.app" in macos
    assert 'test "$(xcrun --sdk macosx --show-sdk-version)" = "26.2"' in macos


def test_macos_rc_exercises_launch_services_user_startup() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    macos = _job_block(workflow, "macos-arm64-rc")

    assert 'python scripts/smoke_macos_user_launch.py --app "$QIAN_MOUNTED_APP"' in macos
    assert "python/tests/regression/test_macos_user_launch_smoke.py" in macos


def test_rc_manifest_job_downloads_and_reverifies_platform_artifacts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(workflow, "rc-manifest")

    assert block.count("actions/download-artifact@v4") == 1
    assert "python scripts/rc_manifest.py combine" in block
    assert "python scripts/rc_manifest.py verify" in block
    assert "actions/upload-artifact@v4" in block
    assert "retention-days: 14" in block
    for filename in (
        "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.app.tar.gz",
        "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.dmg",
        "BUILD-MANIFEST.json",
        "SHA256SUMS.txt",
    ):
        assert filename in block
