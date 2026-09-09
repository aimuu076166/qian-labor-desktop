"""Synthetic current-corpus enrollment and immutable historical boundaries."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import select
import pytest

from test_company_workspaces import api, company, record, snapshot
from qian_labor.models.core import AnalysisBatch, EmployeeRecord, CompanyWorkspace
from qian_labor.ai.providers import FakeAIProvider
from qian_labor.ai.schemas import ExtractionResult, EmploymentFact as ExtractedFact, SourceLocator as ExtractedSource
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.desktop.import_service import DesktopImportService
from qian_labor.models.core import Employee, EmployeeSnapshotBinding, EmploymentFact


class SyntheticMaterialProvider(FakeAIProvider):
    def extract(self, filename, content):
        number, kind = filename.removesuffix(".csv").split("_")
        facts = {"roster": ("employment.status", "active"),
                 "contract": ("employment.contract.exists", True),
                 "social": ("employment.social_insurance.present", True),
                 "termination": ("employment.status", "terminated")}
        field, value = facts[kind]
        return ExtractionResult(document_type={"social": "social_insurance"}.get(kind, kind),
            employee_number=number, employee_name="合成人员", facts=[ExtractedFact(
                employee_id=number, fact_type=field, value=value, confidence=0.99,
                source=ExtractedSource(file_name=filename, row=2, excerpt=f"{number},synthetic-{kind}"))])


def material(tmp_path, number, kind):
    path = tmp_path / f"{number}_{kind}.csv"
    path.write_text(f"employee_number,material\n{number},synthetic-{kind}\n")
    return path


def extract_materials(db, tmp_path, aid, paths):
    imports = DesktopImportService(db, tmp_path)
    imports.import_paths(aid, paths)
    return ProcessingPipeline(db, imports.storage, provider=SyntheticMaterialProvider()).process(aid)


def select_record(client, aid, candidate, rec):
    return client.post(f"/api/analyses/{aid}/matching-decisions", json={
        "candidate_id": candidate["id"], "decision": "create_unknown",
        "display_name": "合成人员", "employee_number": rec["employee_number"],
        "employee_record_id": rec["id"], "expected_record_version": rec["version"]})


def current(client, cid, version=0):
    result = client.post(f"/api/company-workspaces/{cid}/current-analysis", json={
        "id": str(uuid4()), "expected_company_version": version})
    assert result.status_code == 201, result.text
    return result.json()


def test_current_creation_read_only_reconciliation_and_race(api):
    client, db = api
    c = company(client)
    url = f'/api/company-workspaces/{c["id"]}/current-analysis'
    assert client.get(url).json() is None
    with db.session() as s:
        assert list(s.scalars(select(AnalysisBatch))) == []
    requests = [{"id": str(uuid4()), "expected_company_version": 0} for _ in range(2)]
    with ThreadPoolExecutor(2) as executor:
        responses = list(executor.map(lambda body: client.post(url, json=body), requests))
    assert sorted(r.status_code for r in responses) == [201, 409]
    created = next(r.json() for r in responses if r.status_code == 201)
    assert client.get(url).json() == created
    assert created["assessment_profile"] == "labor_materials_v1"
    assert created["status"] == "created" and created["stale"] is True
    assert client.post(url, json=requests[0]).status_code == 409
    with db.session() as s:
        assert len(list(s.scalars(select(AnalysisBatch)))) == 1


def test_current_projection_enrolled_without_evidence_is_pending(api):
    client, _ = api
    c = company(client)
    r = record(client, c["id"])
    state = client.get(f'/api/company-workspaces/{c["id"]}/current').json()
    assert state["enrolled_employee_count"] == 1
    assert state["current_analysis"] is None
    assert state["employees"][0]["id"] == r["id"]
    assert state["employees"][0]["assessment_state"] == "pending_evidence"
    assert state["employees"][0]["current_binding"] is None
    assert state["employees"][0]["assessment"] is None


def test_historical_mutations_refused_and_deletion_versions_atomic(api, tmp_path):
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    rid = str(uuid4())
    assert client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
            {"action": "create", "snapshot_id": sid, "record": {"id": rid, "display_name": "合成员工"}}]}).status_code == 200
    raw = tmp_path / "synthetic.csv"
    raw.write_text("employee_number,name\nSYN-001,合成人员\n")
    for suffix, body in [("import-paths", {"paths": [str(raw)]}), ("process", None),
                         ("matching-decisions", {"candidate_id": "synthetic", "decision": "unmatched"})]:
        response = client.post(f"/api/analyses/{aid}/{suffix}", json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "WORKSPACE_HISTORICAL_READ_ONLY"
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert raw.exists()
    with db.session() as s:
        assert s.get(CompanyWorkspace, c["id"]).version == 2
        assert s.get(EmployeeRecord, rid).version == 1
    detail = client.get(f'/api/company-workspaces/{c["id"]}/employees/{rid}').json()
    assert detail["current_binding"] is None and detail["historical_bindings"] == []
    cur = current(client, c["id"], 2)
    response = client.delete(f'/api/analyses/{cur["analysis_id"]}')
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_CURRENT_ANALYSIS_DELETE_FORBIDDEN"


def test_ten_records_roster_supplements_dedup_restart(api, tmp_path):
    client, db = api
    c = company(client)
    records = [record(client, c["id"], f"SYN-{n:03}", n) for n in range(10)]
    cur = current(client, c["id"], 10)
    aid = cur["analysis_id"]
    paths = [material(tmp_path, r["employee_number"], "roster") for r in records]
    state = extract_materials(db, tmp_path, aid, paths)
    assert state["status"] == "matching_review"
    choices = client.get(f"/api/analyses/{aid}/matching-candidates").json()
    assert choices["current_company_id"] == c["id"]
    assert len(choices["employee_record_options"]) == 10
    by_number = {r["employee_number"]: r for r in records}
    assert len(choices["candidates"]) == 10
    for candidate in choices["candidates"]:
        number = candidate["extracted_fields"]["employee_ids"][0]
        response = select_record(client, aid, candidate, by_number[number])
        assert response.status_code == 200, response.text
    with db.session() as s:
        original = {b.employee_record_id: b.snapshot_id for b in s.scalars(select(EmployeeSnapshotBinding))}
        assert set(original) == {r["id"] for r in records}
    supplements = [material(tmp_path, r["employee_number"], kind) for r in records
                   for kind in ("contract", "social", "termination")]
    extract_materials(db, tmp_path, aid, supplements)
    with db.session() as s:
        fact_count = len(list(s.scalars(select(EmploymentFact))))
        assert len(list(s.scalars(select(Employee)))) == 10
    extract_materials(db, tmp_path, aid, paths + supplements)
    with db.session() as s:
        assert {b.employee_record_id: b.snapshot_id for b in s.scalars(select(EmployeeSnapshotBinding))} == original
        assert len(list(s.scalars(select(EmployeeRecord)))) == 10
        assert len(list(s.scalars(select(EmploymentFact)))) == fact_count
    from qian_labor.desktop.app import create_desktop_app
    from fastapi.testclient import TestClient
    from test_company_workspaces import HEADERS
    restarted = create_desktop_app(data_dir=tmp_path, launch_token=HEADERS["X-Qian-Desktop-Token"])
    client.__exit__(None, None, None)
    with TestClient(restarted) as reopened:
        reopened.headers.update(HEADERS)
        state = reopened.get(f'/api/company-workspaces/{c["id"]}/current?page_size=4').json()
        assert state["enrolled_employee_count"] == 10 and state["pages"] == 3
        assert len(state["employees"]) == 4
        assert all(e["current_binding"]["snapshot_id"] == original[e["id"]] for e in state["employees"])
        assert all(e["lifecycle_status"] == "active" for e in state["employees"])
        from qian_labor.services.dashboard import DashboardService
        shared = {e["id"]: e for e in DashboardService(db).employees(aid)["items"]}
        for employee in state["employees"]:
            summary = shared[employee["snapshot_employee_id"]]
            assert employee["employment_status"] == summary["employment_status"]
            assert employee["assessment"] == {key: summary[key] for key in (
                "risk_counts", "insufficient_data_count", "requires_human_review_count", "material_coverage")}


def test_historical_and_current_same_record_distinct_evidence(api, tmp_path):
    client, db = api
    c = company(client)
    rec = record(client, c["id"])
    aid, sid = snapshot(db)
    assert client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 1, "expected_analysis_version": 0, "decisions": [
            {"action": "link", "snapshot_id": sid, "employee_record_id": rec["id"], "expected_record_version": 0}]}).status_code == 200
    caid = current(client, c["id"], 2)["analysis_id"]
    extract_materials(db, tmp_path, caid, [material(tmp_path, "SYN-001", "roster")])
    candidate = client.get(f"/api/analyses/{caid}/matching-candidates").json()["candidates"][0]
    assert select_record(client, caid, candidate, rec).status_code == 409
    assert select_record(client, caid, candidate, {**rec, "version": 1}).status_code == 200
    detail = client.get(f'/api/company-workspaces/{c["id"]}/employees/{rec["id"]}').json()
    assert len(detail["bindings"]) == 2
    assert detail["historical_bindings"][0]["analysis_id"] == aid
    assert detail["current_binding"]["analysis_id"] == caid
    state = client.get(f'/api/company-workspaces/{c["id"]}/current').json()
    assert state["enrolled_employee_count"] == 1
    assert len(state["employees"]) == 1
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    after = client.get(f'/api/company-workspaces/{c["id"]}/employees/{rec["id"]}').json()
    assert after["version"] == detail["version"] + 1
    assert after["current_binding"] == detail["current_binding"]


def test_new_identity_confirmation_then_second_candidate_reuses_selected_record(api, tmp_path):
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-NEW", kind) for kind in ("roster", "contract")])
    candidates = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"]
    assert len(candidates) == 2
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json={
        "candidate_id": candidates[0]["id"], "decision": "create_unknown",
        "display_name": "合成新增", "employee_number": "SYN-NEW"})
    assert response.status_code == 200, response.text
    rec = client.get(f'/api/company-workspaces/{c["id"]}/employees').json()["items"][0]
    with db.session() as s:
        s.get(Employee, response.json()["target_employee_id"]).match_status = "ambiguous"
        s.commit()
    second = select_record(client, aid, candidates[1], rec)
    assert second.status_code == 200, second.text
    assert second.json()["target_employee_id"] == response.json()["target_employee_id"]
    with db.session() as s:
        assert len(list(s.scalars(select(Employee)))) == 1
        assert len(list(s.scalars(select(EmployeeRecord)))) == 1
        assert s.get(Employee, response.json()["target_employee_id"]).match_status == "confirmed"


def test_direct_historical_services_reject_without_provider_work(api, tmp_path):
    import pytest
    from qian_labor.services.company_workspaces import WorkspaceError
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    assert client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
            {"action": "create", "snapshot_id": sid, "record": {"id": str(uuid4()), "display_name": "合成历史"}}]}).status_code == 200
    imports = DesktopImportService(db, tmp_path)
    with pytest.raises(WorkspaceError, match="WORKSPACE_HISTORICAL_READ_ONLY"):
        imports.import_paths(aid, [material(tmp_path, "SYN-001", "roster")])
    with pytest.raises(WorkspaceError, match="WORKSPACE_HISTORICAL_READ_ONLY"):
        ProcessingPipeline(db, imports.storage, provider=SyntheticMaterialProvider()).process(aid)


def test_deletion_association_cas_rolls_back_together(api):
    from sqlalchemy import event
    import pytest
    from qian_labor.services.deletion import DeletionService
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    rid = str(uuid4())
    assert client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
            {"action": "create", "snapshot_id": sid, "record": {"id": rid, "display_name": "合成历史"}}]}).status_code == 200
    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM analysis_batches"):
            raise RuntimeError("synthetic-delete-failure")
    event.listen(db.engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="synthetic-delete-failure"):
            DeletionService(db, str(db.path.parent / "storage")).delete(aid)
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_delete)
    with db.session() as s:
        assert s.get(CompanyWorkspace, c["id"]).version == 1
        assert s.get(EmployeeRecord, rid).version == 0
        assert s.get(EmployeeSnapshotBinding, sid) is not None


def test_explicit_selection_cross_company_number_conflict_and_new_confirmation(api, tmp_path):
    client, db = api
    a, b = company(client), company(client)
    ra = record(client, a["id"], "SYN-001")
    rb = record(client, b["id"], "SYN-001")
    aid = current(client, a["id"], 1)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    candidate = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    assert select_record(client, aid, candidate, rb).status_code == 409
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json={
        "candidate_id": candidate["id"], "decision": "create_unknown",
        "display_name": "合成人员", "employee_number": "SYN-001"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_EMPLOYEE_NUMBER_EXISTS"
    wrong = {**ra, "employee_number": "SYN-OTHER"}
    assert select_record(client, aid, candidate, wrong).status_code == 409
    good = select_record(client, aid, candidate, ra)
    assert good.status_code == 200, good.text
    assert select_record(client, aid, candidate, ra).status_code == 409
    detail = client.get(f'/api/company-workspaces/{a["id"]}/employees/{ra["id"]}').json()
    assert detail["current_binding"]["snapshot_id"] == good.json()["target_employee_id"]


def test_current_match_recovery_and_queue_reservation(api, tmp_path):
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    candidate = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    body = {"candidate_id": candidate["id"], "decision": "create_unknown", "display_name": "合成", "employee_number": "SYN-001"}
    with client.app.state.processing_queue.mutation(aid):
        assert client.post(f"/api/analyses/{aid}/matching-decisions", json=body).status_code == 409
    (tmp_path / ".qian-migration-recovery").mkdir()
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_DB_RECOVERY_REQUIRED"


def test_bound_merge_is_rejected_and_supplement_makes_assessment_pending(api, tmp_path):
    from qian_labor.models.core import EmployeeMatchCandidate
    client, db = api
    c = company(client)
    recs = [record(client, c["id"], f"SYN-{i:03}", i) for i in range(2)]
    aid = current(client, c["id"], 2)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, r["employee_number"], "roster") for r in recs])
    candidates = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"]
    for cand in candidates:
        rec = next(r for r in recs if r["employee_number"] == cand["extracted_fields"]["employee_ids"][0])
        assert select_record(client, aid, cand, rec).status_code == 200
    with db.session() as s:
        employees = list(s.scalars(select(Employee).where(Employee.analysis_id == aid)))
        source_id, target_id = employees[0].id, employees[1].id
        cand = EmployeeMatchCandidate(analysis_id=aid, candidate_employee_id=source_id,
            reason="synthetic-ambiguity", extracted_fields={"fact_ids": []})
        s.add(cand)
        s.get(AnalysisBatch, aid).status = "matching_review"
        s.commit()
        candidate_id = cand.id
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json={
        "candidate_id": candidate_id, "decision": "merge",
        "source_employee_id": source_id, "target_employee_id": target_id})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_BOUND_EMPLOYEE_MERGE_FORBIDDEN"
    state = client.get(f'/api/company-workspaces/{c["id"]}/current').json()
    assert state["current_analysis"]["stale"] is True
    assert all(e["assessment"] is None and e["assessment_state"] == "pending_analysis" for e in state["employees"])


def test_matching_rejects_sensitive_canonical_number(api, tmp_path):
    client, db = api
    c = company(client)
    aid = current(client, c["id"])["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    candidate = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    # Invalid 32-digit opaque token; no real identifier enters the fixture.
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json={
        "candidate_id": candidate["id"], "decision": "create_unknown",
        "display_name": "合成", "employee_number": "9" * 32})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_IDENTIFIER_INVALID"


def test_current_filters_before_pagination_using_shared_risk_counts(api, tmp_path):
    from qian_labor.models.core import RiskFinding
    client, db = api
    c = company(client)
    recs = [record(client, c["id"], f"SYN-{i:03}", i) for i in range(3)]
    aid = current(client, c["id"], 3)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, recs[-1]["employee_number"], "roster")])
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    matched = select_record(client, aid, cand, recs[-1]).json()
    with db.session() as s:
        s.add(RiskFinding(analysis_id=aid, employee_id=matched["target_employee_id"],
            rule_id="synthetic-filter-fixture", rule_version="v1", category="contract",
            severity="high", assessment_status="suspected_risk", title="Synthetic", summary="Synthetic"))
        s.commit()
    url = f'/api/company-workspaces/{c["id"]}/current'
    result = client.get(url + "?severity=high&page_size=1").json()
    assert result["total"] == 1 and result["pages"] == 1
    assert result["employees"][0]["id"] == recs[-1]["id"]
    result = client.get(url + "?assessment_state=pending_evidence&page_size=1&page=2").json()
    assert result["total"] == 2 and result["pages"] == 2
    assert result["employees"][0]["id"] == recs[1]["id"]
    assert client.get(url + "?severity=low").status_code == 422
    assert client.get(url + "?assessment_state=safe").status_code == 422


def test_excluded_material_alone_is_pending_evidence(api, tmp_path):
    from qian_labor.models.core import UploadedFile
    client, db = api
    c = company(client)
    rec = record(client, c["id"])
    aid = current(client, c["id"], 1)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    assert select_record(client, aid, cand, rec).status_code == 200
    # A synthetic legacy-shaped unsupported fact with a source is not current scoped evidence.
    with db.session() as s:
        fact = s.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))
        fact.fact_type = "employment.payroll.monthly_salary"
        s.get(UploadedFile, fact.file_id).classified_kind = "payroll"
        s.commit()
    result = client.get(f'/api/company-workspaces/{c["id"]}/current').json()["employees"][0]
    assert result["assessment_state"] == "pending_evidence"
    assert result["assessment"] is None


def test_numberless_record_bound_snapshot_still_rejects_other_number(api, tmp_path):
    client, db = api
    c = company(client)
    rec = record(client, c["id"], None)
    aid = current(client, c["id"], 1)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-A", "roster")])
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    first = select_record(client, aid, cand, {**rec, "employee_number": "SYN-A"})
    assert first.status_code == 200
    rec = client.get(f'/api/company-workspaces/{c["id"]}/employees/{rec["id"]}').json()
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-B", "contract")])
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    response = select_record(client, aid, cand, {**rec, "employee_number": "SYN-B"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_EMPLOYEE_NUMBER_MISMATCH"


def test_current_unbound_snapshot_requires_explicit_record_assignment(api, tmp_path):
    client, db = api
    c = company(client)
    rec = record(client, c["id"])
    aid = current(client, c["id"], 1)["analysis_id"]
    with db.session() as s:
        employee = Employee(analysis_id=aid, masked_name=rec["masked_name"], normalized_name=rec["masked_name"],
            employee_number="SYN-001", match_status="auto_matched")
        s.add(employee)
        s.commit()
        eid = employee.id
    result = extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    assert result["status"] == "matching_review"
    with db.session() as s:
        assert s.get(EmployeeSnapshotBinding, eid) is None
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    body = {"candidate_id": cand["id"], "decision": "assign", "employee_id": eid}
    assert client.post(f"/api/analyses/{aid}/matching-decisions", json=body).status_code == 409
    body.update(employee_record_id=rec["id"], expected_record_version=rec["version"])
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["employee_record_id"] == rec["id"]


@pytest.mark.parametrize("select_numberless", [False, True])
def test_omitted_number_cannot_bypass_existing_canonical_number(api, tmp_path, select_numberless):
    from qian_labor.models.core import EmployeeMatchCandidate
    client, db = api
    c = company(client)
    canonical = record(client, c["id"], "SYN-001")
    numberless = record(client, c["id"], None, 1)
    aid = current(client, c["id"], 2)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "roster")])
    cand = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    body = {"candidate_id": cand["id"], "decision": "create_unknown", "display_name": "合成"}
    if select_numberless:
        body.update(employee_record_id=numberless["id"], expected_record_version=numberless["version"])
    response = client.post(f"/api/analyses/{aid}/matching-decisions", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_EMPLOYEE_NUMBER_EXISTS"
    with db.session() as s:
        assert len(list(s.scalars(select(EmployeeRecord)))) == 2
        assert list(s.scalars(select(Employee))) == []
        assert list(s.scalars(select(EmployeeSnapshotBinding))) == []
        assert s.get(EmployeeMatchCandidate, cand["id"]).status == "pending"
    body.update(employee_record_id=canonical["id"], expected_record_version=canonical["version"])
    assert client.post(f"/api/analyses/{aid}/matching-decisions", json=body).status_code == 200


@pytest.mark.parametrize("profile", ["labor_materials_v1", "legacy_full_v1"])
def test_employee_summary_sql_is_bounded_to_requested_snapshots(api, tmp_path, profile):
    from sqlalchemy import event
    from qian_labor.services.dashboard import DashboardService
    client, db = api
    c = company(client)
    recs = [record(client, c["id"], f"SYN-{i:03}", i) for i in range(2)]
    aid = current(client, c["id"], 2)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, r["employee_number"], "roster") for r in recs])
    for cand in client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"]:
        rec = next(r for r in recs if r["employee_number"] == cand["extracted_fields"]["employee_ids"][0])
        assert select_record(client, aid, cand, rec).status_code == 200
    # An independent synthetic legacy batch exercises the shared service's legacy branch.
    if profile == "legacy_full_v1":
        aid, _ = snapshot(db)
        with db.session() as s:
            s.add(Employee(analysis_id=aid, masked_name="合成", normalized_name="合成"))
            s.commit()
    all_items = DashboardService(db).employees(aid)["items"]
    selected = all_items[0]["id"]
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT"):
            queries.append((statement, parameters))
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        bounded = DashboardService(db).employees(aid, employee_ids={selected})
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert bounded["items"] == [all_items[0]]
    facts = [(sql, args) for sql, args in queries if "FROM employment_facts " in sql]
    findings = [(sql, args) for sql, args in queries if "FROM risk_findings " in sql]
    assert len(facts) == 1, "Desktop must not run and discard the legacy evidence query"
    assert len(findings) == 1
    for sql, args in facts + findings:
        assert "employee_id IN" in sql and selected in args
    if profile == "labor_materials_v1":
        queries.clear()
        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            result = client.get(f'/api/company-workspaces/{c["id"]}/current?page_size=1').json()
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        page_snapshot = result["employees"][0]["snapshot_employee_id"]
        for sql, args in queries:
            if "FROM risk_findings " in sql or ("FROM employment_facts " in sql and " AS fact_id" in sql):
                assert "employee_id IN" in sql and page_snapshot in args
