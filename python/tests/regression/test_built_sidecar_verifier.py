from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
ENTRYPOINT = ROOT / "python" / "desktop_entrypoint.py"
VERIFY_BUILT = SCRIPTS / "verify_built_sidecar.py"
EXPECTED_MARKERS = {
    "SIDECAR_BOOT=PASS",
    "LOOPBACK_ONLY=PASS",
    "TOKEN_AUTH=PASS",
    "SQLITE_PERSISTENCE=PASS",
    "SYNTHETIC_IMPORT=PASS",
    "FAKE_PROVIDER_PIPELINE=PASS",
    "MATCHING_REVIEW=PASS",
    "EMPLOYEE_LEDGER=PASS",
    "REPORT=PASS",
    "R01_R20_REGRESSION=PASS",
    "SOURCE_TRACE=PASS",
    "DELETE_CLEANUP=PASS",
    "SIDECAR_SHUTDOWN=PASS",
}


def _load_harness():
    path = SCRIPTS / "desktop_verification.py"
    spec = importlib.util.spec_from_file_location("desktop_verification", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_harness_exercises_the_real_sidecar_contract(capsys) -> None:
    harness = _load_harness()
    known_token = "qian-test-token-that-must-never-be-printed"

    markers = harness.verify_command(
        [sys.executable, str(ENTRYPOINT)],
        token=known_token,
    )

    assert set(markers) == EXPECTED_MARKERS
    captured = capsys.readouterr()
    assert known_token not in captured.out
    assert known_token not in captured.err


def test_built_sidecar_cli_redacts_a_missing_binary_path(tmp_path: Path) -> None:
    missing = tmp_path / "private-candidate-name-qian-sidecar"

    result = subprocess.run(
        [sys.executable, str(VERIFY_BUILT), "--binary", str(missing)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "BUILT_SIDECAR_VERIFY=FAIL:BINARY_MISSING"
    assert str(missing) not in result.stderr


def test_sidecar_build_collects_the_packaged_rule_catalog() -> None:
    build_script = (SCRIPTS / "build_sidecar.py").read_text(encoding="utf-8")

    assert '"--collect-data",\n            "qian_labor",' in build_script
