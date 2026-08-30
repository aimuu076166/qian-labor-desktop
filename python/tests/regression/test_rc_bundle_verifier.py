from __future__ import annotations

import importlib.util
import json
import plistlib
import stat
import struct
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
    names = [entry["artifact_name"] for entry in combined["artifacts"]]
    assert names == sorted(names)
    checksum_names = [line.split("  ", 1)[1] for line in (output / "SHA256SUMS.txt").read_text().splitlines()]
    assert checksum_names == sorted(names)
    manifest.verify_combined_manifest(output / "BUILD-MANIFEST.json", artifacts)


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


def test_packaged_smoke_result_requires_database_and_non_sensitive_pid() -> None:
    smoke = _load("smoke_packaged_app")
    with tempfile.TemporaryDirectory(prefix="qian-rc-smoke-test-") as temporary:
        root = Path(temporary)
        data_dir = root / "app-data"
        data_dir.mkdir()
        (data_dir / "qian-labor.db").write_bytes(b"sqlite")

        pid = smoke.validate_smoke_result(
            root,
            {"database_created": True, "sidecar_pid": 999_999_999},
        )

        assert pid == 999_999_999


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"database_created": False, "sidecar_pid": 4},
        {"database_created": True, "sidecar_pid": "4"},
        {"database_created": True, "sidecar_pid": 4, "token": "must-not-exist"},
    ],
)
def test_packaged_smoke_result_rejects_unverifiable_evidence(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    smoke = _load("smoke_packaged_app")

    with pytest.raises(smoke.PackagedSmokeError):
        smoke.validate_smoke_result(tmp_path, payload)


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
