from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


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


def _load_built_verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("offline_built_verifier", VERIFY_BUILT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_built_verifier_checks_actual_macos_binary_ocr_before_server(monkeypatch, tmp_path, capsys, platform):
    module = _load_built_verifier(monkeypatch)
    binary = tmp_path / "qian-sidecar"
    binary.touch()
    binary.chmod(0o700)
    monkeypatch.setattr(sys, "argv", ["verifier", "--binary", str(binary)])
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/synthetic-development-tools")
    calls = []
    def run(command, **kwargs):
        calls.append("ocr")
        assert command == [str(binary), "--self-test-local-ocr"]
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        assert kwargs["timeout"] == 60 and kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, 0, b"LOCAL_OCR=PASS\n", b"")
    def server(*args, **kwargs):
        assert calls == (["ocr"] if platform == "darwin" else [])
        calls.append("server")
        return sorted(EXPECTED_MARKERS)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(module, "verify_command", server)
    assert module.main() == 0
    expected = EXPECTED_MARKERS | {"BUILT_SIDECAR_VERIFY=PASS"}
    if platform == "darwin": expected |= {"LOCAL_OCR=PASS"}
    assert set(capsys.readouterr().out.splitlines()) == expected


@pytest.mark.parametrize("fault", ["exit", "timeout", "missing", "stdout", "stderr"])
def test_built_verifier_rejects_ocr_failure_without_printing_child_output(monkeypatch, tmp_path, capsys, fault):
    module = _load_built_verifier(monkeypatch)
    binary = tmp_path / "qian-sidecar"
    binary.touch()
    binary.chmod(0o700)
    monkeypatch.setattr(sys, "argv", ["verifier", "--binary", str(binary)])
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(module, "verify_command", lambda *a, **k: pytest.fail("OCR failure must stop verifier"))
    def run(*args, **kwargs):
        if fault == "timeout": raise subprocess.TimeoutExpired("synthetic-private-command", 60)
        if fault == "missing": raise OSError("synthetic-private-path")
        return subprocess.CompletedProcess(args, 1 if fault == "exit" else 0,
            b"synthetic-private-output" if fault == "stdout" else b"LOCAL_OCR=PASS\n",
            b"synthetic-private-stderr" if fault == "stderr" else b"")
    monkeypatch.setattr(subprocess, "run", run)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "BUILT_SIDECAR_VERIFY=FAIL:LOCAL_OCR_FAILED\n"


def _trace_case():
    import tempfile
    from docx import Document
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    with tempfile.TemporaryDirectory() as folder:
        fixture = Path(folder) / "fixture.docx"
        _load_harness()._write_fixture(fixture)
        excerpt = Document(fixture).paragraphs[1].text
    sources = [{"id": f"source-{i}", "file_id": "fixture-file",
                "file_name": "fictional-contract.docx", "locator_type": "paragraph",
                "location": {"paragraph": 2}, "excerpt": excerpt,
                "provenance": "locally_located"} for i in range(2)]
    bindings = {source["id"]: {
        **source, "source_analysis_id": "analysis", "fact_analysis_id": "analysis",
        "location": {"paragraph": 2, "_grounding": {"version": EXTRACTION_VERSION, "status": "locally_located", "requires_review": False}},
        "file_analysis_id": "analysis", "employee_id": "employee",
        "fact_file_id": "fixture-file", "fact_type": kind, "value": value,
    } for source, (kind, value) in zip(sources, [
        ("employment.status", "active"), ("employment.contract.exists", False),
    ])}
    details = [
        {"id": "missing", "rule_id": "PROBATION_DUPLICATE_SUSPECT",
         "assessment_status": "insufficient_data", "sources": []},
        {"id": "r01", "rule_id": "CONTRACT_MISSING_ACTIVE",
         "assessment_status": "suspected_risk", "sources": sources},
    ]
    return details, bindings


def _verify_trace(harness, details, bindings):
    return harness._verify_source_trace(details, bindings, analysis_id="analysis",
                                        employee_id="employee", fixture_file_id="fixture-file",
                                        fixture_name="fictional-contract.docx")


def test_source_trace_accepts_missing_data_first_but_requires_specific_r01_evidence():
    harness = _load_harness()
    details, bindings = _trace_case()
    assert _verify_trace(harness, details, bindings) == "r01"
    assert _verify_trace(harness, list(reversed(details)), bindings) == "r01"


@pytest.mark.parametrize("fault", [
    "unsourced_positive", "missing_r01", "r01_insufficient", "missing_contract_source",
    "wrong_fact_same_location", "wrong_filename", "wrong_location", "wrong_employee",
    "wrong_fact_file", "wrong_analysis", "wrong_value", "unknown_source_id",
])
def test_source_trace_rejects_unrelated_or_incomplete_evidence(fault):
    harness = _load_harness()
    details, bindings = _trace_case()
    if fault == "unsourced_positive":
        details[0]["assessment_status"] = "suspected_risk"
    elif fault == "missing_r01":
        details.pop()
    elif fault == "r01_insufficient":
        details[1]["assessment_status"] = "insufficient_data"
    elif fault == "missing_contract_source":
        details[1]["sources"] = details[1]["sources"][:1]
    elif fault == "wrong_fact_same_location":
        bindings["source-1"]["fact_type"] = "employment.identity.match_status"
    elif fault == "wrong_filename":
        details[1]["sources"][1]["file_name"] = "unrelated.docx"
    elif fault == "wrong_location":
        details[1]["sources"][1]["location"] = {"row": 999}
    elif fault == "wrong_employee":
        bindings["source-1"]["employee_id"] = "other"
    elif fault == "wrong_fact_file":
        bindings["source-1"]["fact_file_id"] = "other"
    elif fault == "wrong_analysis":
        bindings["source-1"]["source_analysis_id"] = "other"
    elif fault == "wrong_value":
        bindings["source-1"]["value"] = True
    else:
        details[1]["sources"][1]["id"] = "unknown"
    with pytest.raises(harness.VerificationError):
        _verify_trace(harness, details, bindings)


def test_report_source_references_must_match_each_verified_finding():
    harness = _load_harness()
    details, _ = _trace_case()
    report_findings = deepcopy(details)
    for finding in report_findings:
        finding["sources"] = [{key: source[key] for key in ("file_name", "locator_type", "location", "provenance", "excerpt")}
                              for source in finding["sources"]]
    harness._verify_report_sources(report_findings, details)
    report_findings[1]["sources"][0]["location"] = {"row": 999}
    with pytest.raises(harness.VerificationError, match="REPORT_INCONSISTENT"):
        harness._verify_report_sources(report_findings, details)


@pytest.mark.parametrize("fault", ["missing_proof", "forged_version", "unlocated", "paragraph", "excerpt", "public_provenance"])
def test_verifier_rejects_unproved_or_mislocated_source(fault):
    harness = _load_harness()
    details, bindings = _trace_case()
    binding = bindings["source-1"]
    if fault == "missing_proof":
        del binding["location"]["_grounding"]
    elif fault == "forged_version":
        binding["location"]["_grounding"]["version"] = "model-asserted-proof"
    elif fault == "unlocated":
        binding["location"]["_grounding"]["status"] = "unlocated_needs_review"
    elif fault == "paragraph":
        binding["location"]["paragraph"] = 99
        details[1]["sources"][1]["location"] = {"paragraph": 99}
    elif fault == "excerpt":
        binding["excerpt"] = details[1]["sources"][1]["excerpt"] = "invented model text"
    else:
        details[1]["sources"][1]["provenance"] = "legacy_unverified"
    with pytest.raises(harness.VerificationError, match="SOURCE_TRACE_INVALID"):
        _verify_trace(harness, details, bindings)


def _scoped_trace_case():
    details, bindings = _trace_case()
    for binding in bindings.values():
        binding.update(assessment_profile="labor_materials_v1", classified_kind="contract", verification_status="confirmed")
    details.append({"id": "r20", "rule_id": "MATERIAL_COVERAGE_LOW", "assessment_status": "insufficient_data",
                    "sources": deepcopy(details[1]["sources"])})
    return details, bindings


def test_scoped_r20_verifier_accepts_actual_contract_presence_and_status_sources():
    harness = _load_harness()
    details, bindings = _scoped_trace_case()
    assert harness._verify_source_trace(details, bindings, analysis_id="analysis", employee_id="employee",
        fixture_file_id="fixture-file", fixture_name="fictional-contract.docx", assessment_profile="labor_materials_v1") == "r01"


@pytest.mark.parametrize("fault", ["percentage", "note", "wrong_classification", "legacy_binding", "missing", "pending", "wrong_source"])
def test_scoped_r20_verifier_rejects_arbitrary_or_incomplete_derived_sources(fault):
    harness = _load_harness()
    details, bindings = _scoped_trace_case()
    if fault in {"percentage", "note"}:
        added = {**bindings["source-1"], "id": "unrelated", "fact_type": "employment.material_coverage" if fault == "percentage" else "employment.contract.note"}
        bindings["unrelated"] = added
        details[-1]["sources"][1] = {**details[-1]["sources"][1], "id": "unrelated"}
    elif fault == "wrong_classification": bindings["source-1"]["classified_kind"] = "payroll"
    elif fault == "legacy_binding": bindings["source-1"]["assessment_profile"] = "legacy_full_v1"
    elif fault == "missing": details[-1]["sources"] = []
    elif fault == "pending": bindings["source-1"]["verification_status"] = "pending_review"
    else: details[-1]["sources"][1]["id"] = "absent"
    with pytest.raises(harness.VerificationError):
        harness._verify_source_trace(details, bindings, analysis_id="analysis", employee_id="employee",
            fixture_file_id="fixture-file", fixture_name="fictional-contract.docx", assessment_profile="labor_materials_v1")
