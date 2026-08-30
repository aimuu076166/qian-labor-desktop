from pathlib import Path
import tomllib

from qian_labor.rules.catalog import RULE_IDS

EXPECTED_RULE_IDS = {
    "CONTRACT_MISSING_ACTIVE",
    "CONTRACT_EXPIRING_30D",
    "CONTRACT_EXPIRED_STILL_ACTIVE",
    "CONTRACT_ENTITY_MISMATCH",
    "CONTRACT_TERM_MISSING_OR_UNREADABLE",
    "PROBATION_TERM_MISMATCH",
    "PROBATION_DUPLICATE_SUSPECT",
    "PROBATION_ENDING_NO_ASSESSMENT",
    "PAY_CONTRACT_ACTUAL_MISMATCH",
    "ACTIVE_NOT_IN_SOCIAL_INSURANCE",
    "SOCIAL_INSURANCE_ENTITY_MISMATCH",
    "SOCIAL_INSURANCE_WAIVER_LANGUAGE",
    "OVERTIME_WITHOUT_PAY_EVIDENCE",
    "ATTENDANCE_PAYROLL_MISMATCH",
    "ACTIVE_WITHOUT_ATTENDANCE_RECORD",
    "TERMINATED_STILL_PAID_OR_INSURED",
    "TERMINATION_MISSING_NOTICE_OR_DELIVERY",
    "TERMINATION_MISSING_SETTLEMENT_RECORD",
    "EMPLOYEE_IDENTITY_AMBIGUOUS",
    "MATERIAL_COVERAGE_LOW",
}


def test_desktop_starts_with_exact_web_r01_r20_catalog() -> None:
    assert len(RULE_IDS) == 20
    assert set(RULE_IDS) == EXPECTED_RULE_IDS


def test_desktop_runtime_has_no_server_queue_or_postgres_driver_dependency() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    dependencies = {item.lower() for item in project["dependencies"]}
    assert not any(item.startswith("redis") for item in dependencies)
    assert not any(item.startswith("rq") for item in dependencies)
    assert not any(item.startswith("psycopg") for item in dependencies)
