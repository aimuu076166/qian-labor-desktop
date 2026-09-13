"""Scoped company history listing; all fixtures are synthetic."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import (
    AnalysisBatch,
    CompanyAnalysisBinding,
    Employee,
    EmployeeMatchCandidate,
    EmployeeSnapshotBinding,
    ProcessingJob,
)


TOKEN = "synthetic-company-history-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


@pytest.fixture
def api(tmp_path):
    app = create_desktop_app(data_dir=tmp_path, launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers.update(HEADERS)
        yield client, app.state.database


def create_company(client, display_name="同名虚构企业"):
    response = client.post(
        "/api/company-workspaces",
        json={"id": str(uuid4()), "display_name": display_name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_analysis(session, analysis_id, *, created_at, status="completed", deleted_at=None):
    item = AnalysisBatch(
        id=analysis_id,
        name=f"合成批次-{analysis_id}",
        company_display_name="同名虚构企业",
        status=status,
        created_at=created_at,
        deleted_at=deleted_at,
    )
    session.add(item)
    session.flush()
    return item


def test_history_is_company_scoped_before_count_order_and_pagination(api):
    """Catches leakage from current, other-company, deleted, and later-page rows."""
    client, db = api
    company_a = create_company(client)
    company_b = create_company(client)
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    with db.session() as session:
        for analysis_id in ("00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2", "00000000-0000-0000-0000-0000000000a3"):
            add_analysis(session, analysis_id, created_at=stamp)
            session.add(CompanyAnalysisBinding(
                analysis_id=analysis_id, company_id=company_a["id"], role="historical"
            ))
        add_analysis(session, "00000000-0000-0000-0000-0000000000c1", created_at=stamp + timedelta(days=1))
        session.add(CompanyAnalysisBinding(
            analysis_id="00000000-0000-0000-0000-0000000000c1", company_id=company_a["id"], role="current"
        ))
        add_analysis(session, "00000000-0000-0000-0000-0000000000b1", created_at=stamp + timedelta(days=2))
        session.add(CompanyAnalysisBinding(
            analysis_id="00000000-0000-0000-0000-0000000000b1", company_id=company_b["id"], role="historical"
        ))
        add_analysis(
            session,
            "00000000-0000-0000-0000-0000000000d1",
            created_at=stamp + timedelta(days=3),
            deleted_at=stamp + timedelta(days=4),
        )
        session.add(CompanyAnalysisBinding(
            analysis_id="00000000-0000-0000-0000-0000000000d1", company_id=company_a["id"], role="historical"
        ))
        session.commit()

    first = client.get(
        f'/api/company-workspaces/{company_a["id"]}/analyses?page=1&page_size=2'
    )
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 3
    assert first.json()["pages"] == 2
    assert [item["id"] for item in first.json()["items"]] == [
        "00000000-0000-0000-0000-0000000000a3",
        "00000000-0000-0000-0000-0000000000a2",
    ]
    assert all(item["relation"] == "historical" for item in first.json()["items"])
    assert all(item["company_id"] == company_a["id"] for item in first.json()["items"])
    assert all(not item["can_adopt"] for item in first.json()["items"])
    assert all(item["adoption_blocker_code"] == "WORKSPACE_ANALYSIS_ALREADY_BOUND" for item in first.json()["items"])

    second = client.get(
        f'/api/company-workspaces/{company_a["id"]}/analyses?page=2&page_size=2'
    ).json()
    assert [item["id"] for item in second["items"]] == [
        "00000000-0000-0000-0000-0000000000a1"
    ]


def test_unbound_ignores_legacy_name_and_excludes_every_bound_or_deleted_analysis(api):
    """Catches legacy-name ownership inference and bound-analysis leakage."""
    client, db = api
    company_a = create_company(client)
    company_b = create_company(client)
    stamp = datetime(2026, 2, 3, tzinfo=UTC)
    with db.session() as session:
        add_analysis(session, "unbound-other-name", created_at=stamp).company_display_name = "另一虚构名称"
        add_analysis(session, "bound-to-b", created_at=stamp + timedelta(minutes=1))
        session.add(CompanyAnalysisBinding(
            analysis_id="bound-to-b", company_id=company_b["id"], role="historical"
        ))
        add_analysis(session, "unbound-deleting", created_at=stamp + timedelta(minutes=2), status="deleting")
        add_analysis(session, "unbound-deleted", created_at=stamp + timedelta(minutes=3), deleted_at=stamp)
        session.commit()

    response = client.get(
        f'/api/company-workspaces/{company_a["id"]}/analyses?relation=unbound'
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == "unbound-other-name"
    assert item["company_id"] is None
    assert item["relation"] == "unbound"
    assert item["can_adopt"] is True
    assert item["adoption_blocker_code"] is None
    assert "storage_key" not in response.text and "id_number" not in response.text


def test_unbound_adoption_snapshot_uses_queue_authority_and_aggregate_pending_count(api):
    """Catches unresolved/unsettled selection and stale-job queue misrepresentation."""
    client, db = api
    owner = create_company(client)
    stamp = datetime(2026, 3, 4, tzinfo=UTC)
    with db.session() as session:
        add_analysis(session, "pending-identity", created_at=stamp)
        employee = Employee(
            analysis_id="pending-identity",
            masked_name="合成***",
            normalized_name="合成***",
            match_status="unknown",
        )
        session.add(employee)
        session.flush()
        session.add(EmployeeMatchCandidate(
            analysis_id="pending-identity",
            candidate_employee_id=employee.id,
            reason="synthetic",
            status="pending",
        ))
        add_analysis(session, "unsettled", created_at=stamp + timedelta(minutes=1), status="processing")
        add_analysis(session, "active-job", created_at=stamp + timedelta(minutes=2))
        session.add(ProcessingJob(
            analysis_id="active-job",
            job_type="extract",
            input_hash="synthetic",
            unique_key="synthetic-active-job",
            status="running",
        ))
        add_analysis(session, "queue-active", created_at=stamp + timedelta(minutes=3))
        session.commit()

    queue = client.app.state.processing_queue
    with queue._lock:
        queue._submitting_analysis_id = "queue-active"
    try:
        payload = client.get(
            f'/api/company-workspaces/{owner["id"]}/analyses?relation=unbound'
        ).json()
    finally:
        with queue._lock:
            queue._submitting_analysis_id = None

    items = {item["id"]: item for item in payload["items"]}
    assert items["pending-identity"]["pending_identity_count"] == 1
    assert items["pending-identity"]["adoption_blocker_code"] == "WORKSPACE_MATCHING_UNRESOLVED"
    assert items["unsettled"]["adoption_blocker_code"] == "WORKSPACE_ANALYSIS_NOT_SETTLED"
    # Persisted jobs can be stale after process exit; only the live queue snapshot
    # can honestly represent a present reservation before Task 6 recovery work.
    assert items["active-job"]["adoption_blocker_code"] is None
    assert items["active-job"]["can_adopt"] is True
    assert items["queue-active"]["adoption_blocker_code"] == "DESKTOP_ANALYSIS_BUSY"


@pytest.mark.parametrize('unknown,pending,blocked', [(False, True, True), (True, False, True), (False, False, False)])
def test_independent_adoption_identity_predicates(api, unknown, pending, blocked):
    client, db = api; owner = create_company(client); aid = str(uuid4())
    with db.session() as session:
        add_analysis(session, aid, created_at=datetime(2026, 9, 9))
        employee = Employee(analysis_id=aid, masked_name='合成***', normalized_name='合成***',
            match_status='unknown' if unknown else 'confirmed')
        session.add(employee); session.flush(); sid = employee.id
        if pending:
            session.add(EmployeeMatchCandidate(analysis_id=aid, candidate_employee_id=sid, reason='synthetic', status='pending'))
        session.commit()
    items = client.get(f'/api/company-workspaces/{owner["id"]}/analyses?relation=unbound').json()['items']
    item = next(item for item in items if item['id'] == aid)
    assert item['can_adopt'] is not blocked
    assert item['adoption_blocker_code'] == ('WORKSPACE_MATCHING_UNRESOLVED' if blocked else None)
    response = client.put(f'/api/company-workspaces/{owner["id"]}/analyses/{aid}/binding', json={
        'expected_company_version': 0, 'expected_analysis_version': 0, 'decisions': [
            {'snapshot_id': sid, 'action': 'create', 'record': {'id': str(uuid4()), 'display_name': '完全虚构员工'}}]})
    assert response.status_code == (409 if blocked else 200)
    if blocked:
        assert response.json()['detail']['code'] == 'WORKSPACE_MATCHING_UNRESOLVED'
        assert client.get(f'/api/company-workspaces/{owner["id"]}/analyses/{aid}/binding').json()['bound'] is False


def test_company_analysis_listing_validation_auth_and_unknown_company_are_safe(api):
    """Catches unauthenticated access, missing company checks, and echoed bad queries."""
    client, _ = api
    owner = create_company(client)
    endpoint = f'/api/company-workspaces/{owner["id"]}/analyses'
    assert client.get(endpoint, headers={"X-Qian-Desktop-Token": "wrong"}).status_code == 401
    missing = client.get(f'/api/company-workspaces/{uuid4()}/analyses?relation=unbound')
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "WORKSPACE_NOT_FOUND"
    for query in ("relation=secret-invalid-value", "page=0", "page_size=101"):
        response = client.get(f"{endpoint}?{query}")
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "WORKSPACE_REQUEST_INVALID"}}
        assert "secret-invalid-value" not in response.text


def test_company_analysis_listing_is_strictly_read_only(api):
    """Catches accidental GET migration, version bumps, binding, or job creation."""
    client, db = api
    owner = create_company(client)
    stamp = datetime(2026, 4, 5, tzinfo=UTC)
    with db.session() as session:
        analysis = add_analysis(session, "read-only-unbound", created_at=stamp)
        analysis.version = 7
        session.commit()
        before = {
            "company_version": client.get(f'/api/company-workspaces/{owner["id"]}').json()["version"],
            "analysis_version": 7,
            "bindings": session.scalar(select(func.count()).select_from(CompanyAnalysisBinding)),
            "snapshot_bindings": session.scalar(select(func.count()).select_from(EmployeeSnapshotBinding)),
            "jobs": session.scalar(select(func.count()).select_from(ProcessingJob)),
        }

    response = client.get(
        f'/api/company-workspaces/{owner["id"]}/analyses?relation=unbound'
    )
    assert response.status_code == 200, response.text
    with db.session() as session:
        after = {
            "company_version": client.get(f'/api/company-workspaces/{owner["id"]}').json()["version"],
            "analysis_version": session.get(AnalysisBatch, "read-only-unbound").version,
            "bindings": session.scalar(select(func.count()).select_from(CompanyAnalysisBinding)),
            "snapshot_bindings": session.scalar(select(func.count()).select_from(EmployeeSnapshotBinding)),
            "jobs": session.scalar(select(func.count()).select_from(ProcessingJob)),
        }
    assert after == before
