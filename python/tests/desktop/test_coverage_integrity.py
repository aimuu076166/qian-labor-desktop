from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import EmploymentFact
from qian_labor.services.dashboard import DashboardService
from test_dashboard_api import _seed_completed_analysis, HEADERS, TOKEN


def fact(value, verification="confirmed", kind="employment.status"):
    return SimpleNamespace(employee_id="synthetic", normalized_value_json=value,
        verification_status=verification, fact_type=kind, created_at=datetime(2026, 1, 1),
        file_id="synthetic-file", classified_kind="contract")


def employee(facts):
    return SimpleNamespace(id="synthetic", employment_status="active", facts=facts)


@pytest.mark.parametrize("rows", [
    [fact("active"), fact("terminated")],
    [fact("terminated"), fact("active")],
    [fact("active", "needs_human_confirmation")],
    [fact(None)],
])
def test_unresolved_status_is_consistently_unknown_not_not_applicable(rows):
    person = employee(rows)
    statuses, _ = DashboardService._coverage_state({person.id: person}, rows)
    assert statuses[person.id] == "unknown"
    assert DashboardService._employee_status(person) == "unknown"
    coverage = DashboardService._material_coverage({person.id: person}, rows)
    assert coverage["scope_pending"] is True
    assert coverage["unknown_employee_count"] == 1
    assert all(not item["not_applicable"] for item in coverage["items"])


def test_empty_employee_set_is_not_evidence_that_materials_are_inapplicable():
    coverage = DashboardService._material_coverage({}, [])
    assert coverage["scope_pending"] is True
    assert all(not item["not_applicable"] for item in coverage["items"])


@pytest.mark.parametrize("presence", [
    [fact(True, "pending_review", "employment.contract.exists")],
    [fact(True, kind="employment.contract.exists"), fact(False, kind="employment.contract.exists")],
])
def test_uncertain_or_conflicting_presence_is_not_counted_as_covered(presence):
    rows = [fact("active"), *presence]
    person = employee(rows)
    _, covered = DashboardService._coverage_state({person.id: person}, rows)
    assert "contract" not in covered[person.id]


def test_consistent_confirmed_status_and_presence_remain_covered():
    rows = [fact("active"), fact("active"), fact(True, kind="employment.contract.exists")]
    person = employee(rows)
    coverage = DashboardService._material_coverage({person.id: person}, rows)
    assert coverage["scope_pending"] is False
    assert coverage["items"][0]["covered"] == 1


def test_cross_file_presence_conflict_is_not_counted_as_confirmed_coverage():
    exists = fact(True, kind="employment.contract.exists")
    missing = fact(False, kind="employment.contract.exists")
    missing.file_id = "other-synthetic-file"
    rows = [fact("active"), exists, missing]
    person = employee(rows)
    _, covered = DashboardService._coverage_state({person.id: person}, rows)
    assert "contract" not in covered[person.id]


def test_presence_in_a_roster_cannot_substantiate_a_different_contract_file():
    status = fact("active")
    exists = fact(True, kind="employment.contract.exists")
    exists.file_id, exists.classified_kind = "synthetic-roster", "roster"
    person = employee([status, exists])
    _, covered = DashboardService._coverage_state({person.id: person}, person.facts)
    assert "contract" not in covered[person.id]


def test_database_backed_views_agree_on_pending_status_and_coverage(tmp_path):
    app = create_desktop_app(data_dir=tmp_path, launch_token=TOKEN)
    ids = _seed_completed_analysis(app)
    with app.state.database.session() as session:
        stored = session.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == ids["analysis_id"]))
        stored.verification_status = "pending_review"
        session.commit()
    base = f"/api/analyses/{ids['analysis_id']}"
    with TestClient(app) as client:
        dashboard = client.get(base + "/dashboard", headers=HEADERS).json()
        ledger = client.get(base + "/employees", headers=HEADERS).json()
        detail = client.get(base + f"/employees/{ids['employee_id']}", headers=HEADERS).json()
        report = client.get(base + "/report", headers=HEADERS).json()
    assert ledger["items"][0]["employment_status"] == detail["employee"]["employment_status"] == "unknown"
    assert dashboard["overview"]["material_coverage"] == report["material_coverage"]
    assert report["material_coverage"]["scope_pending"] is True
