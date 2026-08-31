from __future__ import annotations

import importlib.util
import json
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
CONFIG = ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json"
COMMIT = "a" * 40


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_macho(path: Path, cpu_type: int = 0x0100000C) -> None:
    path.write_bytes(struct.pack("<IiiIIIII", 0xFEEDFACF, cpu_type, 0, 2, 0, 0, 0, 0))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_pe(path: Path, machine: int = 0x8664) -> None:
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    path.write_bytes(payload)


def _mac_app(root: Path) -> Path:
    app = root / "Qian.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "cn.qianlabor.desktop",
                "CFBundleShortVersionString": "0.1.0",
                "CFBundleExecutable": "qian-labor-desktop",
            },
            handle,
        )
    _write_macho(macos / "qian-labor-desktop")
    _write_macho(macos / "qian-sidecar")
    return app


def _codesign_real_macos_app(app: Path, *, hardened_runtime: bool) -> None:
    main = app / "Contents" / "MacOS" / "qian-labor-desktop"
    sidecar = app / "Contents" / "MacOS" / "qian-sidecar"
    for executable in (main, sidecar):
        shutil.copyfile("/usr/bin/true", executable)
        executable.chmod(0o755)
    options = ["--options", "runtime"] if hardened_runtime else []
    for signed_path in (sidecar, main, app):
        subprocess.run(
            ["codesign", "--force", "--sign", "-", *options, str(signed_path)],
            check=True,
            capture_output=True,
        )


def _windows_payload(root: Path) -> Path:
    payload = root / "installed"
    payload.mkdir()
    _write_pe(payload / "企安用工.exe")
    _write_pe(payload / "qian-sidecar.exe")
    return payload


def test_bundle_verifier_accepts_expected_macos_and_windows_architectures(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")

    assert verifier.detect_macho_arch(_mac_app(tmp_path) / "Contents/MacOS/qian-sidecar") == "arm64"
    windows = _windows_payload(tmp_path)
    assert verifier.detect_pe_arch(windows / "qian-sidecar.exe") == "x64"
    verifier.verify_payload(_mac_app(tmp_path / "second"), "macos", CONFIG)
    verifier.verify_payload(windows, "windows", CONFIG, verify_windows_version=False)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS codesign is required")
def test_macos_cli_rejects_a_bundle_without_a_valid_code_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_rc_bundle.py",
            "--platform",
            "macos",
            "--payload",
            str(app),
            "--source-config",
            str(CONFIG),
            "--expected-commit",
            head,
            "--repo-root",
            str(ROOT),
        ],
    )

    assert verifier.main() == 1
    assert capsys.readouterr().err.strip() == "RC_BUNDLE_VERIFY=FAIL:SIGNATURE_INVALID"


def test_macos_ad_hoc_config_disables_hardened_runtime() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    macos = config["bundle"]["macOS"]

    assert macos["signingIdentity"] == "-"
    assert macos["hardenedRuntime"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS codesign is required")
def test_macos_signature_gate_rejects_hardened_runtime_sidecar(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _codesign_real_macos_app(app, hardened_runtime=True)

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_macos_code_signature(app)

    assert captured.value.code == "HARDENED_RUNTIME_UNEXPECTED"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS codesign is required")
def test_macos_signature_gate_accepts_non_hardened_ad_hoc_bundle(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _codesign_real_macos_app(app, hardened_runtime=False)

    verifier.verify_macos_code_signature(app)


def _mock_codesign_descriptions(
    verifier, monkeypatch: pytest.MonkeyPatch, details: str
) -> None:
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", details)

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)


def test_macos_signature_gate_rejects_missing_code_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _mock_codesign_descriptions(
        verifier,
        monkeypatch,
        "Executable=/tmp/Qian.app\nSignature=adhoc\nTeamIdentifier=not set\n",
    )

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_macos_code_signature(app)

    assert captured.value.code == "SIGNATURE_INVALID"


def test_macos_signature_gate_rejects_runtime_bit_without_runtime_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _mock_codesign_descriptions(
        verifier,
        monkeypatch,
        "CodeDirectory v=20400 size=1 flags=0x10002(adhoc) hashes=1+0 location=embedded\n"
        "Signature=adhoc\nTeamIdentifier=not set\n",
    )

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_macos_code_signature(app)

    assert captured.value.code == "HARDENED_RUNTIME_UNEXPECTED"


@pytest.mark.parametrize(
    ("details", "expected_code"),
    [
        (
            "CodeDirectory v=20400 size=1 flags=not-hex(adhoc) hashes=1+0 location=embedded\n"
            "Signature=adhoc\nTeamIdentifier=not set\n",
            "SIGNATURE_INVALID",
        ),
        (
            "CodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+0 location=embedded\n"
            "Signature=adhoc-extra\nTeamIdentifier=not set\n",
            "ADHOC_SIGNATURE_REQUIRED",
        ),
        (
            "CodeDirectory v=20400 size=1 flags=0x2(adhoc) hashes=1+0 location=embedded\n"
            "Signature=adhoc\nTeamIdentifier=not set-extra\n",
            "ADHOC_SIGNATURE_REQUIRED",
        ),
    ],
)
def test_macos_signature_gate_rejects_malformed_codesign_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    details: str,
    expected_code: str,
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _mock_codesign_descriptions(verifier, monkeypatch, details)

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_macos_code_signature(app)

    assert captured.value.code == expected_code


@pytest.mark.parametrize(
    ("platform", "machine", "expected_code"),
    [("macos", 0x01000007, "ARCHITECTURE_INVALID"), ("windows", 0x014C, "ARCHITECTURE_INVALID")],
)
def test_bundle_verifier_rejects_wrong_architecture(
    tmp_path: Path, platform: str, machine: int, expected_code: str
) -> None:
    verifier = _load("verify_rc_bundle")
    payload = _mac_app(tmp_path) if platform == "macos" else _windows_payload(tmp_path)
    sidecar = next(path for path in payload.rglob("qian-sidecar*") if path.is_file())
    if platform == "macos":
        _write_macho(sidecar, machine)
    else:
        _write_pe(sidecar, machine)

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(payload, platform, CONFIG, verify_windows_version=False)

    assert captured.value.code == expected_code


def test_bundle_verifier_rejects_a_missing_sidecar(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    (app / "Contents" / "MacOS" / "qian-sidecar").unlink()

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(app, "macos", CONFIG)

    assert captured.value.code == "SIDECAR_COUNT_INVALID"


def test_bundle_verifier_rejects_a_sidecar_for_another_platform(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    _write_pe(app / "Contents" / "MacOS" / "qian-sidecar-x86_64-pc-windows-msvc.exe")

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(app, "macos", CONFIG)

    assert captured.value.code == "SIDECAR_COUNT_INVALID"


def test_windows_bundle_rejects_a_macos_sidecar(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    payload = _windows_payload(tmp_path)
    (payload / "qian-sidecar").write_bytes(b"mac sidecar")

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(payload, "windows", CONFIG, verify_windows_version=False)

    assert captured.value.code == "SIDECAR_COUNT_INVALID"


@pytest.mark.parametrize("filename", [".env", "private.sqlite", "run.log", "state.cache", "fixture.json"])
def test_bundle_verifier_rejects_prohibited_payload_files(
    tmp_path: Path, filename: str
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / filename).write_text("synthetic", encoding="utf-8")

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(app, "macos", CONFIG)

    assert captured.value.code == "PROHIBITED_PAYLOAD_FILE"


@pytest.mark.parametrize(
    "leaked_text",
    [
        "sk-" + "testabcdefghijklmnopqrstuvwx",
        "/Users/local-builder/work/qian-labor-desktop/private-build",
        "C:\\Users\\local-builder\\AppData\\Local\\Temp\\private-build",
    ],
)
def test_bundle_verifier_redacts_sensitive_or_build_path_matches(
    tmp_path: Path, leaked_text: str
) -> None:
    verifier = _load("verify_rc_bundle")
    app = _mac_app(tmp_path)
    leaked = app / "Contents" / "Resources" / "metadata.txt"
    leaked.parent.mkdir()
    leaked.write_text(leaked_text, encoding="utf-8")

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(app, "macos", CONFIG)

    assert captured.value.code in {"SENSITIVE_PAYLOAD", "BUILD_PATH_REFERENCE"}
    assert leaked_text not in str(captured.value)


def test_bundle_error_never_echoes_a_control_character_or_secret_path(tmp_path: Path) -> None:
    verifier = _load("verify_rc_bundle")
    secret_component = "private-secret-name\nsecond-line"
    unsafe = tmp_path / secret_component

    with pytest.raises(verifier.BundleVerificationError) as captured:
        verifier.verify_payload(unsafe, "macos", CONFIG)

    assert captured.value.code == "PAYLOAD_INVALID"
    assert secret_component not in str(captured.value)


def test_platform_manifests_combine_into_sorted_verified_evidence(tmp_path: Path) -> None:
    manifest = _load("rc_manifest")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    mac_app = artifacts / "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.app.tar.gz"
    mac_dmg = artifacts / "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.dmg"
    windows = artifacts / "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
    mac_app.write_bytes(b"app archive")
    mac_dmg.write_bytes(b"dmg image")
    windows.write_bytes(b"nsis installer")
    toolchain = {"node": "v22", "pnpm": "9.15.0", "python": "3.12", "rustc": "1.98.0"}
    workflow = {"repository": "owner/repo", "run_id": "123", "run_attempt": "1"}
    mac_manifest = tmp_path / "mac.json"
    windows_manifest = tmp_path / "windows.json"
    manifest.create_platform_manifest(
        [mac_app, mac_dmg], mac_manifest, "macos", "arm64", COMMIT,
        "PASS", "PASS", toolchain, workflow, "2026-08-31T00:00:00Z",
    )
    manifest.create_platform_manifest(
        [windows], windows_manifest, "windows", "x64", COMMIT,
        "PASS", "PASS", toolchain, workflow, "2026-08-31T00:00:00Z",
    )
    assert (tmp_path / "SHA256SUMS-macos.txt").is_file()
    assert (tmp_path / "SHA256SUMS-windows.txt").is_file()

    output = tmp_path / "combined"
    combined = manifest.combine_manifests([mac_manifest, windows_manifest], artifacts, output)

    assert combined["git_commit"] == COMMIT
    assert combined["product"] == "qian-labor-desktop"
    assert combined["signed"] is False
    assert combined["notarized"] is False
    assert combined["real_provider_smoke"] == "NOT_RUN"
    assert combined["image_input"] == "NOT_RUN"
    assert combined["abnormal_lifecycle_smoke"] == "PASS"
    assert all(
        evidence["abnormal_lifecycle_smoke"] == "PASS"
        for evidence in combined["platform_evidence"]
    )
    names = [entry["artifact_name"] for entry in combined["artifacts"]]
    assert names == sorted(names)
    checksum_names = [line.split("  ", 1)[1] for line in (output / "SHA256SUMS.txt").read_text().splitlines()]
    assert checksum_names == sorted(names)
    manifest.verify_combined_manifest(output / "BUILD-MANIFEST.json", artifacts)


def test_manifest_toolchain_capture_resolves_windows_command_shims(monkeypatch) -> None:
    manifest = _load("rc_manifest")
    observed: list[list[str]] = []

    monkeypatch.setattr(manifest.shutil, "which", lambda name: f"C:/tools/{name}.CMD")

    def fake_run(command, **kwargs):
        observed.append(command)
        return manifest.subprocess.CompletedProcess(command, 0, "9.15.0\n", "")

    monkeypatch.setattr(manifest.subprocess, "run", fake_run)

    assert manifest._command_output(["pnpm", "--version"]) == "9.15.0"
    assert observed == [["C:/tools/pnpm.CMD", "--version"]]


def test_manifest_verification_rejects_checksum_mismatch_without_leaking_content(
    tmp_path: Path,
) -> None:
    manifest = _load("rc_manifest")
    artifact = tmp_path / "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
    artifact.write_bytes(b"first")
    platform_manifest = tmp_path / "windows.json"
    manifest.create_platform_manifest(
        [artifact], platform_manifest, "windows", "x64", COMMIT, "PASS", "PASS",
        {"node": "v22", "pnpm": "9.15.0", "python": "3.12", "rustc": "1.98.0"},
        {"repository": "owner/repo", "run_id": "123", "run_attempt": "1"},
        "2026-08-31T00:00:00Z",
    )
    artifact.write_bytes(b"private changed content")

    with pytest.raises(manifest.ManifestError) as captured:
        manifest.combine_manifests([platform_manifest], tmp_path, tmp_path / "out")

    assert captured.value.code == "ARTIFACT_CHECKSUM_MISMATCH"
    assert "private changed content" not in str(captured.value)


@pytest.mark.parametrize("mutation", ["missing_field", "missing_artifact", "duplicate_name"])
def test_manifest_validation_rejects_invalid_evidence(tmp_path: Path, mutation: str) -> None:
    manifest = _load("rc_manifest")
    artifact = tmp_path / "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
    artifact.write_bytes(b"installer")
    path = tmp_path / "platform.json"
    manifest.create_platform_manifest(
        [artifact], path, "windows", "x64", COMMIT, "PASS", "PASS",
        {"node": "v22", "pnpm": "9.15.0", "python": "3.12", "rustc": "1.98.0"},
        {"repository": "owner/repo", "run_id": "123", "run_attempt": "1"},
        "2026-08-31T00:00:00Z",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing_field":
        del payload["artifacts"][0]["sha256"]
    elif mutation == "missing_artifact":
        payload["artifacts"][0]["artifact_name"] = "missing.exe"
    else:
        payload["artifacts"].append(dict(payload["artifacts"][0]))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(manifest.ManifestError):
        manifest.combine_manifests([path], tmp_path, tmp_path / "out")


def test_not_run_packaged_smoke_requires_a_stable_technical_reason(tmp_path: Path) -> None:
    manifest = _load("rc_manifest")
    artifact = tmp_path / "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
    artifact.write_bytes(b"installer")
    arguments = (
        [artifact],
        tmp_path / "platform.json",
        "windows",
        "x64",
        COMMIT,
        "PASS",
        "NOT_RUN",
        {"node": "v22", "pnpm": "9.15.0", "python": "3.12", "rustc": "1.98.0"},
        {"repository": "owner/repo", "run_id": "123", "run_attempt": "1"},
        "2026-08-31T00:00:00Z",
    )

    with pytest.raises(manifest.ManifestError) as captured:
        manifest.create_platform_manifest(*arguments)

    assert captured.value.code == "PACKAGED_APP_SMOKE_REASON_REQUIRED"
    payload = manifest.create_platform_manifest(
        *arguments,
        packaged_app_smoke_reason="HOSTED_RUNNER_GUI_UNAVAILABLE",
    )
    assert payload["packaged_app_smoke_reason"] == "HOSTED_RUNNER_GUI_UNAVAILABLE"


def test_platform_manifest_rejects_missing_abnormal_lifecycle_pass(tmp_path: Path) -> None:
    manifest = _load("rc_manifest")
    artifact = tmp_path / "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe"
    artifact.write_bytes(b"installer")

    with pytest.raises(manifest.ManifestError) as captured:
        manifest.create_platform_manifest(
            [artifact],
            tmp_path / "platform.json",
            "windows",
            "x64",
            COMMIT,
            "PASS",
            "PASS",
            {"node": "v22", "pnpm": "9.15.0", "python": "3.12", "rustc": "1.98.0"},
            {"repository": "owner/repo", "run_id": "123", "run_attempt": "1"},
            "2026-08-31T00:00:00Z",
            abnormal_lifecycle_smoke="NOT_RUN",
        )

    assert captured.value.code == "ABNORMAL_LIFECYCLE_SMOKE_INVALID"


def test_packaged_smoke_result_requires_database_and_non_sensitive_pid() -> None:
    smoke = _load("smoke_packaged_app")
    with tempfile.TemporaryDirectory(prefix="qian-rc-smoke-test-") as temporary:
        root = Path(temporary)
        data_dir = root / "app-data"
        data_dir.mkdir()
        (data_dir / "qian-labor.db").write_bytes(b"sqlite")

        pid = smoke.validate_smoke_result(
            root,
            {
                "database_created": True,
                "cleanup_complete": True,
                "sidecar_pid": 999_999_999,
            },
        )

        assert pid == 999_999_999


def test_packaged_smoke_started_evidence_contains_only_diagnostic_pid() -> None:
    smoke = _load("smoke_packaged_app")

    assert smoke.validate_smoke_started({"sidecar_pid": 999_999_999}) == 999_999_999
    with pytest.raises(smoke.PackagedSmokeError):
        smoke.validate_smoke_started(
            {"sidecar_pid": 999_999_999, "token": "must-not-exist"}
        )


def test_packaged_smoke_retries_transient_windows_data_directory_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load("smoke_packaged_app")
    root = tmp_path / "smoke"
    root.mkdir()
    attempts = 0

    def transiently_locked(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(32, "file is in use", path / "app-data" / "qian-labor.db")
        path.rmdir()

    monkeypatch.setattr(smoke.shutil, "rmtree", transiently_locked)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    smoke.remove_temporary_tree_when_released(root, timeout=1)

    assert attempts == 3
    assert not root.exists()


def test_packaged_smoke_retries_nested_file_disappearance_while_root_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load("smoke_packaged_app")
    root = tmp_path / "smoke"
    root.mkdir()
    (root / "qian-labor.db").write_bytes(b"sqlite")
    real_rmtree = smoke.shutil.rmtree
    attempts = 0

    def nested_file_disappeared(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError(2, "file disappeared", path / "qian-labor.db-wal")
        real_rmtree(path)

    monkeypatch.setattr(smoke.shutil, "rmtree", nested_file_disappeared)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    smoke.remove_temporary_tree_when_released(root, timeout=1)

    assert attempts == 2
    assert not root.exists()


def test_packaged_smoke_fails_closed_when_data_directory_lock_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load("smoke_packaged_app")
    monotonic_values = iter((0.0, 1.0))

    def persistently_locked(path: Path) -> None:
        raise PermissionError(32, "file is in use", path / "app-data" / "qian-labor.db")

    monkeypatch.setattr(smoke.shutil, "rmtree", persistently_locked)
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(smoke.PackagedSmokeError) as captured:
        smoke.remove_temporary_tree_when_released(tmp_path, timeout=0.1)

    assert captured.value.code == "SIDECAR_DATA_LOCK_RESIDUE"


def test_packaged_smoke_wraps_non_permission_cleanup_errors_in_stable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load("smoke_packaged_app")
    monotonic_values = iter((0.0, 1.0))

    def directory_not_empty(path: Path) -> None:
        raise OSError(145, "directory is not empty", path)

    monkeypatch.setattr(smoke.shutil, "rmtree", directory_not_empty)
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(smoke.PackagedSmokeError) as captured:
        smoke.remove_temporary_tree_when_released(tmp_path, timeout=0.1)

    assert captured.value.code == "SIDECAR_DATA_LOCK_RESIDUE"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"database_created": False, "cleanup_complete": True, "sidecar_pid": 4},
        {"database_created": True, "cleanup_complete": False, "sidecar_pid": 4},
        {"database_created": True, "cleanup_complete": True, "sidecar_pid": "4"},
        {"database_created": True, "sidecar_pid": 4},
        {
            "database_created": True,
            "cleanup_complete": True,
            "sidecar_pid": 4,
            "token": "must-not-exist",
        },
    ],
)
def test_packaged_smoke_result_rejects_unverifiable_evidence(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    smoke = _load("smoke_packaged_app")

    with pytest.raises(smoke.PackagedSmokeError):
        smoke.validate_smoke_result(tmp_path, payload)


def test_packaged_smoke_nonzero_exit_reports_only_a_stable_failure_code(tmp_path: Path) -> None:
    smoke = _load("smoke_packaged_app")
    (tmp_path / "failure.json").write_text(
        '{"code":"DESKTOP_SIDECAR_SPAWN_FAILED"}', encoding="utf-8"
    )

    assert smoke.diagnose_nonzero_exit(tmp_path) == (
        "APP_EXIT_FAILED:DESKTOP_SIDECAR_SPAWN_FAILED"
    )

    (tmp_path / "failure.json").write_text(
        '{"code":"private-path\\nTOKEN_VALUE"}', encoding="utf-8"
    )
    assert smoke.diagnose_nonzero_exit(tmp_path) == "APP_EXIT_FAILED:NO_STABLE_DIAGNOSTIC"


def test_packaged_smoke_distinguishes_nonzero_exit_after_valid_result(tmp_path: Path) -> None:
    smoke = _load("smoke_packaged_app")
    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    (data_dir / "qian-labor.db").write_bytes(b"sqlite")
    (tmp_path / "result.json").write_text(
        '{"database_created":true,"cleanup_complete":true,"sidecar_pid":999999999}',
        encoding="utf-8",
    )

    assert smoke.diagnose_nonzero_exit(tmp_path) == "APP_EXIT_FAILED:AFTER_VALID_RESULT"


def test_staging_uses_exact_ascii_rc_artifact_names(tmp_path: Path) -> None:
    staging = _load("stage_rc_artifacts")
    app = _mac_app(tmp_path)
    dmg = tmp_path / "企安用工_0.1.0_aarch64.dmg"
    dmg.write_bytes(b"disk image")
    mac_output = tmp_path / "mac-output"
    mac_paths = staging.stage_macos(app, dmg, mac_output)

    installer = tmp_path / "企安用工_0.1.0_x64-setup.exe"
    installer.write_bytes(b"installer")
    windows_paths = staging.stage_windows(installer, tmp_path / "windows-output")

    names = [path.name for path in [*mac_paths, *windows_paths]]
    assert names == [
        "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.app.tar.gz",
        "qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.dmg",
        "qian-labor-desktop-0.1.0-rc.1-windows-x64-unsigned-nsis.exe",
    ]
    assert all(name.isascii() for name in names)


def test_staging_refuses_a_nonempty_output_directory(tmp_path: Path) -> None:
    staging = _load("stage_rc_artifacts")
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"installer")
    output = tmp_path / "output"
    output.mkdir()
    (output / "unrelated.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(staging.StagingError) as captured:
        staging.stage_windows(installer, output)

    assert captured.value.code == "STAGING_OUTPUT_NOT_EMPTY"
