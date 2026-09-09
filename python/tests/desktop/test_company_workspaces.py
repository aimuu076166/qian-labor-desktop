"""Durable identities, explicit ownership and recoverable CAS, synthetic only."""
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import AnalysisBatch, Employee
from qian_labor.security.masking import mask_identity

HEADERS = {"X-Qian-Desktop-Token": "synthetic-company-token"}


@pytest.fixture
def api(tmp_path):
    app = create_desktop_app(data_dir=tmp_path, launch_token=HEADERS["X-Qian-Desktop-Token"])
    with TestClient(app) as client:
        client.headers.update(HEADERS)
        yield client, app.state.database


def company(client):
    response = client.post("/api/company-workspaces", json={"id": str(uuid4()), "display_name": "虚构企业"})
    assert response.status_code == 201, response.text
    return response.json()


def test_canonical_company_creation_fixture_matches_real_schema_post_point_and_list(api):
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    from qian_labor.desktop.company_schemas import CompanyCreate
    from qian_labor.models.core import CompanyWorkspace
    fixture = json.loads((Path(__file__).resolve().parents[3] / 'apps/desktop/tests/fixtures/company-create-canonical.json').read_text())
    body = {'id': fixture['request']['id'], 'display_name': ''.join(fixture['request']['name_parts'])}
    accepted = CompanyCreate(**body)
    assert accepted.display_name == '合成企业 联系电话138****8000'
    assert accepted.display_name != body['display_name']
    client, db = api
    response = client.post('/api/company-workspaces', json=body)
    assert response.status_code == 201
    post = response.json()
    point = client.get('/api/company-workspaces/' + body['id']).json()
    listing = client.get('/api/company-workspaces').json()
    assert listing == [point]
    def instant(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    assert instant(post['created_at']) == instant(point['created_at'])
    assert instant(fixture['post']['created_at']) == instant(fixture['point']['created_at'])
    assert fixture['listing'] == [fixture['point']]
    for actual, recorded in [(post, fixture['post']), (point, fixture['point'])]:
        assert {k: v for k, v in actual.items() if k != 'created_at'} == {k: v for k, v in recorded.items() if k != 'created_at'}
        assert actual['display_name'] == accepted.display_name
    assert client.post('/api/company-workspaces', json=body).json()['detail']['code'] == 'WORKSPACE_IDENTITY_CONFLICT'
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(CompanyWorkspace)) == 1
        assert session.get(CompanyWorkspace, body['id']).display_name == accepted.display_name


def record(client, owner, number="SYN-001", version=0):
    response = client.post(f"/api/company-workspaces/{owner}/employees", json={
        "expected_company_version": version, "id": str(uuid4()), "display_name": "完全虚构员工",
        "employee_number": number, "department": "合成部门", "job_title": "合成岗位"})
    assert response.status_code == 201, response.text
    return response.json()


def snapshot(db):
    with db.session() as session:
        batch = AnalysisBatch(name="合成历史", company_display_name="虚构企业", status="completed")
        session.add(batch)
        session.flush()
        employee = Employee(analysis_id=batch.id, masked_name="合成***", normalized_name="合成***", match_status="confirmed")
        session.add(employee)
        session.commit()
        return batch.id, employee.id


def test_company_employee_auth_masking_pagination_and_restart(api, tmp_path):
    client, db = api
    assert client.get("/api/company-workspaces", headers={"X-Qian-Desktop-Token": "wrong"}).status_code == 401
    c = company(client)
    e = record(client, c["id"])
    assert e["masked_name"] == mask_identity("完全虚构员工")
    assert "display_name" not in e
    assert e["version"] == 0 and e["lifecycle_status"] == "active"
    assert client.get(f'/api/company-workspaces/{c["id"]}').json()["version"] == 1
    listing = client.get(f'/api/company-workspaces/{c["id"]}/employees?search=SYN-001&page_size=1').json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == e["id"]
    assert client.get(f'/api/company-workspaces/{c["id"]}/employees?page=0').status_code == 422
    pref = client.get("/api/workspace-preference").json()
    assert pref == {"last_company_id": None, "version": 0}
    assert client.put("/api/workspace-preference", json={"last_company_id": c["id"], "expected_version": 0}).status_code == 200
    assert client.put("/api/workspace-preference", json={"last_company_id": None, "expected_version": 0}).status_code == 409
    # A restart releases the first app's task-owner lifespan before reopening.
    client.__exit__(None, None, None)
    other = create_desktop_app(data_dir=tmp_path, launch_token=HEADERS["X-Qian-Desktop-Token"])
    with TestClient(other) as reopened:
        reopened.headers.update(HEADERS)
        assert reopened.get("/api/workspace-preference").json()["last_company_id"] == c["id"]
        assert reopened.get(f'/api/company-workspaces/{c["id"]}/employees/{e["id"]}').json()["bindings"] == []


def test_explicit_binding_cas_reconciliation_and_deletion_boundary(api):
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    endpoint = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding'
    assert client.get(endpoint).json()["bound"] is False
    assert client.get(f'/api/company-workspaces/{c["id"]}/employees').json()["total"] == 0
    rid = str(uuid4())
    body = {"expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
        {"snapshot_id": sid, "action": "create", "record": {"id": rid, "display_name": "完全虚构员工", "employee_number": "SYN-002"}}]}
    result = client.put(endpoint, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["role"] == "historical"
    assert result.json()["bindings"][0]["employee_record_id"] == rid
    assert client.put(endpoint, json=body).status_code == 409
    assert client.get(endpoint).json() == result.json()
    with db.session() as session:
        assert session.get(Employee, sid).normalized_name == "合成***"
        assert session.get(AnalysisBatch, aid).assessment_profile == "legacy_full_v1"
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    detail = client.get(f'/api/company-workspaces/{c["id"]}/employees/{rid}').json()
    assert detail["id"] == rid and detail["bindings"] == []
    assert client.get(f'/api/company-workspaces/{c["id"]}').status_code == 200


def test_invalid_decisions_rollback_and_cross_company_links_rejected(api):
    client, db = api
    a, b = company(client), company(client)
    target = record(client, b["id"])
    aid, sid = snapshot(db)
    endpoint = f'/api/company-workspaces/{a["id"]}/analyses/{aid}/binding'
    body = {"expected_company_version": 0, "expected_analysis_version": 0, "decisions": []}
    assert client.put(endpoint, json=body).status_code == 409
    body["decisions"] = [{"snapshot_id": sid, "action": "link", "employee_record_id": target["id"], "expected_record_version": 0}]
    assert client.put(endpoint, json=body).status_code == 409
    assert client.get(f'/api/company-workspaces/{a["id"]}').json()["version"] == 0
    assert client.get(endpoint).json()["bound"] is False
    own = record(client, a["id"])
    body["expected_company_version"] = 1
    body["decisions"][0]["employee_record_id"] = own["id"]
    assert client.put(endpoint, json=body).status_code == 200
    assert client.put(f'/api/company-workspaces/{b["id"]}/analyses/{aid}/binding', json={**body, "expected_company_version": 1}).status_code == 409


def test_unique_employee_number_and_safe_validation_errors(api):
    client, _ = api
    c = company(client)
    record(client, c["id"])
    body = {"id": str(uuid4()), "display_name": "完全虚构员工", "employee_number": " SYN-001 ", "expected_company_version": 1}
    assert client.post(f'/api/company-workspaces/{c["id"]}/employees', json=body).status_code == 409
    assert client.get(f'/api/company-workspaces/{c["id"]}').json()["version"] == 1
    bad = client.post(f'/api/company-workspaces/{c["id"]}/employees', json={**body, "display_name": " "})
    assert bad.status_code == 422
    assert "完全虚构员工" not in bad.text


def test_pending_recovery_blocks_mutations_but_gets_are_read_only(api, tmp_path):
    client, db = api
    c = company(client)
    before = c["version"]
    (tmp_path / ".qian-migration-recovery").mkdir()
    assert client.get(f'/api/company-workspaces/{c["id"]}').json()["version"] == before
    assert client.put("/api/workspace-preference", json={"last_company_id": c["id"], "expected_version": 0}).status_code == 409


@pytest.mark.parametrize("state", ["created", "processing", "matching_review", "deleting"])
def test_nonterminal_analyses_cannot_be_adopted(api, state):
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    with db.session() as s:
        s.get(AnalysisBatch, aid).status = state
        s.commit()
    result = client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 0, "expected_analysis_version": 0, "decisions": []})
    assert result.status_code == (404 if state == "deleting" else 409)
    assert client.get(f'/api/company-workspaces/{c["id"]}').json()["version"] == 0


def test_queue_reservation_excludes_adoption(api):
    client, db = api
    c = company(client)
    aid, _ = snapshot(db)
    with client.app.state.processing_queue.mutation(aid):
        result = client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
            "expected_company_version": 0, "expected_analysis_version": 0, "decisions": []})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "DESKTOP_ANALYSIS_BUSY"


def test_unresolved_matching_and_number_mismatch_fail_without_partial_write(api):
    client, db = api
    c = company(client)
    employee = record(client, c["id"])
    aid, sid = snapshot(db)
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding'
    body = {"expected_company_version": 1, "expected_analysis_version": 0, "decisions": [
        {"snapshot_id": sid, "action": "link", "employee_record_id": employee["id"], "expected_record_version": 0}]}
    with db.session() as s:
        e = s.get(Employee, sid)
        e.match_status = "unknown"
        s.commit()
    assert client.put(url, json=body).json()["detail"]["code"] == "WORKSPACE_MATCHING_UNRESOLVED"
    with db.session() as s:
        e = s.get(Employee, sid)
        e.match_status, e.employee_number = "confirmed", "SYN-DIFFERENT"
        s.commit()
    assert client.put(url, json=body).json()["detail"]["code"] == "WORKSPACE_EMPLOYEE_NUMBER_MISMATCH"
    assert client.get(url).json()["bound"] is False
    assert client.get(f'/api/company-workspaces/{c["id"]}').json()["version"] == 1
    assert client.get(f'/api/company-workspaces/{c["id"]}/employees/{employee["id"]}').json()["version"] == 0


def test_merged_placeholders_are_explicitly_excluded(api):
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    with db.session() as s:
        s.get(Employee, sid).employment_status = "merged"
        s.commit()
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding'
    result = client.put(url, json={"expected_company_version": 0, "expected_analysis_version": 0, "decisions": []})
    assert result.status_code == 200
    assert result.json()["excluded_merged_snapshot_ids"] == [sid]
    assert result.json()["bindings"] == []


def test_simultaneous_same_version_creates_exactly_one_record(api):
    from concurrent.futures import ThreadPoolExecutor
    from qian_labor.desktop.company_schemas import EmployeeCreate
    from qian_labor.services.company_workspaces import CompanyWorkspaceService, WorkspaceError
    client, db = api
    c = company(client)
    def create(index):
        try:
            return CompanyWorkspaceService(db).create_employee(c["id"], EmployeeCreate(
                id=uuid4(), display_name="合成员工", employee_number=f"SYN-{index}", expected_company_version=0)).id
        except WorkspaceError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, range(2)))
    assert outcomes.count("WORKSPACE_VERSION_CONFLICT") == 1
    assert client.get(f'/api/company-workspaces/{c["id"]}/employees').json()["total"] == 1


def test_reconciliation_gets_do_not_insert_preferences_or_adopt(api):
    from qian_labor.models.core import WorkspacePreference, CompanyAnalysisBinding
    client, db = api
    c = company(client)
    aid, _ = snapshot(db)
    for _ in range(2):
        assert client.get("/api/workspace-preference").json() == {"last_company_id": None, "version": 0}
        assert not client.get(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding').json()["bound"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(WorkspacePreference)) == 0
        assert s.scalar(select(func.count()).select_from(CompanyAnalysisBinding)) == 0


def test_binding_survives_restart_and_late_failure_rolls_back_all_records(api, tmp_path):
    from qian_labor.database import create_desktop_database
    from qian_labor.services.company_workspaces import CompanyWorkspaceService
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    with db.session() as s:
        second = Employee(analysis_id=aid, masked_name="合成***", normalized_name="合成***", match_status="confirmed")
        s.add(second)
        s.commit()
        sid2 = second.id
    url = f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding'
    rid = str(uuid4())
    body = {"expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
        {"snapshot_id": sid, "action": "create", "record": {"id": rid, "display_name": "完全虚构员工"}},
        {"snapshot_id": sid2, "action": "link", "employee_record_id": str(uuid4()), "expected_record_version": 0}]}
    assert client.put(url, json=body).status_code == 409
    assert client.get(f'/api/company-workspaces/{c["id"]}/employees').json()["total"] == 0
    assert client.get(url).json()["analysis_version"] == 0
    body["decisions"][1] = {"snapshot_id": sid2, "action": "create", "record": {"id": str(uuid4()), "display_name": "完全虚构员工"}}
    assert client.put(url, json=body).status_code == 200
    reopened = create_desktop_database(tmp_path)
    try:
        binding = CompanyWorkspaceService(reopened).binding(c["id"], aid)
        assert binding["bound"] and len(binding["bindings"]) == 2
        assert {b.employee_record_id for b in binding["bindings"]} == {d["record"]["id"] for d in body["decisions"]}
    finally:
        reopened.dispose()


def test_foreign_keys_and_snapshot_trigger_enforce_binding_company_and_analysis(api):
    from sqlalchemy.exc import IntegrityError
    from qian_labor.models.core import CompanyAnalysisBinding, EmployeeSnapshotBinding
    client, db = api
    a, b = company(client), company(client)
    ra, rb = record(client, a["id"]), record(client, b["id"])
    aid, sid = snapshot(db)
    aid2, sid2 = snapshot(db)
    with db.session() as s:
        s.add(CompanyAnalysisBinding(analysis_id=aid, company_id=a["id"]))
        s.commit()
    for snapshot_id, rid in [(sid, rb["id"]), (sid2, ra["id"])]:
        with pytest.raises(IntegrityError):
            with db.session() as s:
                s.add(EmployeeSnapshotBinding(snapshot_id=snapshot_id, analysis_id=aid, company_id=a["id"], employee_record_id=rid))
                s.commit()
