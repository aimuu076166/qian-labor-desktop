"""Synthetic stored-fact corrections: real SQLite/API, no provider requests."""
from uuid import uuid4
from copy import deepcopy

import pytest
from sqlalchemy import select

from test_company_workspaces import api, company, record
from test_current_company import current, material, extract_materials, select_record
from qian_labor.models.core import EmploymentFact, SourceLocator, UploadedFile


def add_fact(db, aid, name, value, *, file_id=None, unlocated=False):
    import hashlib
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    from qian_labor.services.source_provenance import deterministic_citation_id
    with db.session() as s:
        original = s.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))
        fact = EmploymentFact(analysis_id=aid, employee_id=original.employee_id, file_id=file_id or original.file_id,
            fact_type=name, value_json=value, normalized_value_json=value, extraction_method="synthetic",
            confidence=1, verification_status="needs_human_confirmation" if unlocated else "verified", dedupe_key=str(uuid4()))
        s.add(fact)
        s.flush()
        excerpt = "" if unlocated else "合成原材料"
        location = {"_grounding": {"version": EXTRACTION_VERSION, "status": "unlocated_needs_review" if unlocated else "locally_located"}}
        file = s.get(UploadedFile, fact.file_id)
        location["_citation_id"] = deterministic_citation_id(file.sha256, location, excerpt)
        source = SourceLocator(analysis_id=aid, fact_id=fact.id, file_id=fact.file_id, locator_type="document",
            location=location, excerpt=excerpt, content_hash=hashlib.sha256(excerpt.encode()).hexdigest())
        s.add(source)
        s.commit()
        return fact.id


def local_evaluate(client, base):
    state = client.get(base + "/assessment-results").json()["assessment_revision"]
    response = client.post(base + "/reevaluate", json={"id": str(uuid4()), "expected_input_revision": state["input_revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def prepared(api, tmp_path):
    client, db = api
    c = company(client)
    rec = record(client, c["id"])
    aid = current(client, c["id"], 1)["analysis_id"]
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-001", "contract")])
    candidate = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    assert select_record(client, aid, candidate, rec).status_code == 200
    return client, db, c, rec, aid, f'/api/company-workspaces/{c["id"]}/analyses/{aid}'


def revision_body(row, value=False, kind="correct"):
    return {"id": str(uuid4()), "expected_version": row["version"], "kind": kind,
            "value": value, "reason": "已核对合成原材料", "expected_owner_signature": row["owner_signature"],
            "expected_source_signature": row["source_signature"]}


def test_processed_file_with_unlocated_current_source_is_not_complete(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    add_fact(db, aid, 'employment.probation.start_date', '2026-01-01', unlocated=True)
    state = client.get(base + '/assessment-results').json()['assessment_revision']
    assert state['availability'] == 'available'
    assert state['completeness'] == 'partial'


def test_recovered_fact_risk_context_uses_only_current_evidence(api, tmp_path):
    import hashlib
    from qian_labor.ai.grounding import EXTRACTION_VERSION, deterministic_citation_id
    from qian_labor.models.core import AuditEvent
    from qian_labor.services.risk_evaluation import RiskEvaluationService
    _, db, _, _, aid, _ = prepared(api, tmp_path)
    fid = add_fact(db, aid, 'employment.probation.assessment_exists', True, unlocated=True)
    with db.session() as s:
        fact = s.get(EmploymentFact, fid)
        file = s.get(UploadedFile, fact.file_id)
        quote = '已留存考核表'
        location = {'sheet': 'synthetic', 'row': 2, 'column': '4', '_grounding': {
            'version': EXTRACTION_VERSION, 'status': 'locally_located', 'requires_review': False}}
        location['_citation_id'] = deterministic_citation_id(file.sha256, location, quote)
        source = SourceLocator(analysis_id=aid, fact_id=fid, file_id=file.id, locator_type='cell',
            location=location, excerpt=quote, content_hash=hashlib.sha256(quote.encode()).hexdigest())
        s.add(source)
        s.flush()
        s.add(AuditEvent(analysis_id=aid, event_type='extraction_grounding_completed',
            metadata_json={'file_id': file.id, 'source_ids': [source.id]}))
        s.commit()
        context = RiskEvaluationService._context_facts(s, [fact])['employment.probation.assessment_exists']
        assert context.value is True and context.conflicted is False
        assert context.source_locator_ids == (source.id,)


def test_human_correction_immutable_sources_cas_and_reconciliation(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    response = client.get(base + "/effective-facts", params={"record_id": rec["id"]})
    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    with db.session() as s:
        original = deepcopy(s.get(EmploymentFact, row["id"]).value_json)
        sources = [(x.id, deepcopy(x.location), x.excerpt, x.content_hash) for x in s.scalars(select(SourceLocator))]
    body = revision_body(row)
    saved = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["effective_value"] is False
    assert saved.json()["human_confirmed"] is True
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=body).status_code == 409
    result = client.get(base + "/effective-facts", params={"record_id": rec["id"], "request_id": body["id"]}).json()
    assert result["request_revision"]["id"] == body["id"]
    assert result["assessment_revision"]["fresh"] is False
    with db.session() as s:
        assert s.get(EmploymentFact, row["id"]).value_json == original
        assert [(x.id, x.location, x.excerpt, x.content_hash) for x in s.scalars(select(SourceLocator))] == sources


def test_workspace_counts_current_projection_not_all_extraction_versions(api, tmp_path):
    from qian_labor.services.effective_facts import effective_projection

    client, db, company_row, record_row, aid, base = prepared(api, tmp_path)
    with db.session() as session:
        original = session.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))
        file_id = original.file_id
    legacy_id = add_fact(db, aid, "employment.probation.start_date", "2026-01-01", file_id=file_id)
    with db.session() as session:
        for source in session.scalars(select(SourceLocator).where(SourceLocator.fact_id == legacy_id)):
            source.location = {**source.location, "_grounding": {
                "version": "parser-grounding-v2", "status": "unlocated_needs_review"}}
        session.commit()
        expected = sum(row.file_id == file_id for row in effective_projection(session, aid))
    response = client.get(f"/api/analyses/{aid}/workspace")
    assert response.status_code == 200
    material_row = next(row for row in response.json()["files"] if row["id"] == file_id)
    assert material_row["fact_count"] == expected
    with db.session() as session:
        assert session.get(EmploymentFact, legacy_id) is not None


def test_v2_fact_revision_and_frozen_report_survive_v3_reextraction(api, tmp_path):
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    from qian_labor.models.core import AnalysisBatch, EffectiveFactRevision, EmploymentFact, SourceLocator, ProcessingJob
    from qian_labor.services.effective_facts import effective_projection
    from qian_labor.storage.local import LocalStorage
    from qian_labor.jobs.processing import ProcessingPipeline
    from test_current_company import SyntheticMaterialProvider

    client, db, c, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    revision = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, False))
    assert revision.status_code == 200, revision.text

    context = client.get(base + "/report-versions").json()["current_context"]
    report_request = {
        "request_id": str(uuid4()),
        "expected_input_revision": context["input_revision"],
        "expected_result_revision": context["result_revision"],
        "expected_review_revision": context["review_revision"],
        "expected_context_signature": context["context_signature"],
    }
    report_response = client.post(base + "/report-versions", json=report_request)
    assert report_response.status_code == 201, report_response.text
    frozen = report_response.json()["snapshot"]

    with db.session() as session:
        old_fact = session.get(EmploymentFact, row["id"])
        assert old_fact is not None
        old_fact.dedupe_key = "legacy-v2-" + str(uuid4())
        for source in session.scalars(select(SourceLocator).where(SourceLocator.fact_id == old_fact.id)):
            proof = dict(source.location.get("_grounding", {}))
            proof["version"] = "parser-grounding-v2"
            source.location = {**source.location, "_grounding": proof}
        old_job = session.scalar(select(ProcessingJob).where(
            ProcessingJob.analysis_id == aid, ProcessingJob.file_id == old_fact.file_id,
            ProcessingJob.job_type == "extract"))
        assert old_job is not None
        old_job.unique_key = f"{aid}:{old_fact.file_id}:extract:{session.get(UploadedFile, old_fact.file_id).sha256}:parser-grounding-v2:contract-advisory-v1"
        session.get(AnalysisBatch, aid).status = "created"
        session.commit()
        old_id = old_fact.id

    pipeline = ProcessingPipeline(db, LocalStorage(str(client.app.state.storage_root)),
                                  provider=SyntheticMaterialProvider())
    pipeline.process(aid)

    with db.session() as session:
        facts = list(session.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)))
        assert old_id in {fact.id for fact in facts}
        new_facts = [fact for fact in facts if fact.id != old_id and fact.file_id == row["file_id"]]
        assert new_facts
        assert any(
            source.location.get("_grounding", {}).get("version") == EXTRACTION_VERSION
            for fact in new_facts
            for source in session.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact.id))
        )
        old_revision = session.scalar(select(EffectiveFactRevision).where(EffectiveFactRevision.fact_id == old_id))
        assert old_revision is not None and old_revision.value is False
        current_rows = effective_projection(session, aid)
        assert old_id not in {fact.id for fact in current_rows}
        assert {fact.id for fact in new_facts} & {fact.id for fact in current_rows}

    reopened = client.get(base + "/report-versions/" + frozen["id"])
    assert reopened.status_code == 200
    assert reopened.json()["snapshot"] == frozen


def test_strict_manual_shapes():
    from qian_labor.services import effective_facts
    assert hasattr(effective_facts, "validate_manual_value")
    validate = effective_facts.validate_manual_value
    assert validate("employment.contract.exists", False) is False
    assert validate("employment.contract.end_date", "2028-02-29") == "2028-02-29"
    assert validate("employment.probation.periods", [["2026-01-01", "2026-02-01"]])
    for kind, value in [("employment.contract.exists", 1), ("employment.contract.exists", "false"),
            ("employment.contract.end_date", "2026-02-29"), ("employment.contract.end_date", "20260101"),
            ("employment.probation.periods", [["2026-02-01", "2026-01-01"]]),
            ("employment.status", "invented"), ("employment.entities", ["x"] * 51),
            ("employment.termination.settlement_materials", ["unknown"]),
            ("employment.material_coverage", True), ("employment.pay.actual_wage", float("inf")),
            ("employment.identity.match_status", "confirmed"), ("invented", None)]:
        with pytest.raises(ValueError):
            validate(kind, value)


def test_explicit_local_reevaluation_uuid_and_check_date(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    response = client.get(base + "/assessment-decisions")
    assert response.status_code == 200, response.text
    state = response.json()
    decision = client.post(base + "/assessment-decisions", json={"id": str(uuid4()),
        "kind": "check_date", "expected_version": state["check_date_version"],
        "value": "2027-01-01", "reason": "核查合成材料日期"})
    assert decision.status_code == 200, decision.text
    state = client.get(base + "/assessment-results").json()["assessment_revision"]
    body = {"id": str(uuid4()), "expected_input_revision": state["input_revision"]}
    result = client.post(base + "/reevaluate", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["fresh"] is True
    assert result.json()["check_date"] == "2027-01-01"
    assert client.post(base + "/reevaluate", json=body).status_code == 409
    reconciled = client.get(base + "/assessment-results", params={"request_id": body["id"]}).json()
    assert reconciled["request_result"]["id"] == body["id"]


def test_shared_effective_date_boolean_confirmation_and_honest_sources(api, tmp_path, monkeypatch):
    from qian_labor.services.risk_evaluation import RiskEvaluationService
    from qian_labor.models.core import RiskFinding, AIUsageRecord
    from qian_labor.ai.providers import FakeAIProvider
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    status = add_fact(db, aid, "employment.status", "probation", unlocated=True)
    end = add_fact(db, aid, "employment.contract.end_date", "2025-01-01", unlocated=True)
    contract = client.get(base + "/effective-facts").json()["items"]
    row = next(r for r in contract if r["fact_type"] == "employment.contract.exists")
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, True, "confirm")).status_code == 200
    monkeypatch.setattr(FakeAIProvider, "extract", lambda *a, **k: pytest.fail("local reevaluation called provider"))
    with db.session() as s:
        usage = len(list(s.scalars(select(AIUsageRecord))))
    for fid, value in [(status, "probation"), (end, "2027-01-15")]:
        row = next(row for row in client.get(base + "/effective-facts").json()["items"] if row["id"] == fid)
        body = revision_body(row, value, "confirm" if fid == status else "correct")
        response = client.post(base + f'/effective-facts/{fid}/revisions', json=body)
        assert response.status_code == 200, response.text
        assert response.json()["sources"][0]["provenance"] == "unlocated_needs_review"
    day = client.get(base + "/assessment-decisions").json()
    assert client.post(base + "/assessment-decisions", json={"id": str(uuid4()), "kind": "check_date",
        "expected_version": day["check_date_version"], "value": "2027-01-01", "reason": "合成核查"}).status_code == 200
    local_evaluate(client, base)
    with db.session() as s:
        facts = list(s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)))
        context = RiskEvaluationService._context_facts(s, facts)
        assert context["employment.contract.end_date"].value == "2027-01-15"
        assert context["employment.status"].value == "probation"
        assert len(list(s.scalars(select(AIUsageRecord)))) == usage
        finding = s.scalar(select(RiskFinding).where(RiskFinding.analysis_id == aid, RiskFinding.rule_id == "CONTRACT_EXPIRING_30D", RiskFinding.is_current.is_(True)))
        assert finding is not None
    dashboard = client.get(f"/api/analyses/{aid}/dashboard").json()["overview"]
    assert dashboard["assessment_revision"]["fresh"] is True
    assert dashboard["material_coverage"]["overall"] == 0.5
    current_state = client.get(f'/api/company-workspaces/{c["id"]}/current').json()
    assert current_state["employees"][0]["employment_status"] == "probation"
    assert current_state["employees"][0]["assessment_state"] == "evaluated"
    detail = client.get(f"/api/findings/{finding.id}").json()
    assert detail["requires_human_review"] is False
    assert any(source["human_confirmed"] for source in detail["sources"])
    report = client.get(f"/api/analyses/{aid}/report").json()
    assert report["assessment_revision"] == dashboard["assessment_revision"]


def test_confirmation_reopens_dependency_review_without_changing_original(api, tmp_path):
    from qian_labor.services.finding_review import finding_signature
    from qian_labor.models.core import RiskFinding
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    add_fact(db, aid, "employment.status", "active")
    local_evaluate(client, base)
    with db.session() as s:
        finding = s.scalar(select(RiskFinding).where(RiskFinding.analysis_id == aid, RiskFinding.is_current.is_(True),
            RiskFinding.rule_id == "CONTRACT_MISSING_ACTIVE"))
        fid = finding.id
        before = finding_signature(s, finding)
    row = next(r for r in client.get(base + "/effective-facts").json()["items"] if r["fact_type"] == "employment.contract.exists")
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, True, "confirm")).status_code == 200
    with db.session() as s:
        # Same-value human provenance is a real evidence dependency change.
        from qian_labor.services.effective_facts import effective_projection
        assert next(r for r in effective_projection(s, aid) if r.id == row["id"]).human_confirmed
        target = s.get(RiskFinding, fid)
        assert row["id"] in target.trigger_fact_ids
        assert finding_signature(s, target) != before


def clone_contract_file(db, aid, suffix="new"):
    from qian_labor.models.core import UploadedFile
    with db.session() as s:
        original = s.scalar(select(UploadedFile).where(UploadedFile.analysis_id == aid))
        row = UploadedFile(analysis_id=aid, original_filename=suffix+".docx", storage_key=str(uuid4()),
            mime_type=original.mime_type, extension=".docx", size_bytes=1, sha256=str(uuid4()),
            status="processed", classified_kind="contract")
        s.add(row)
        s.commit()
        return row.id


def choose_contract(client, base, rec, file_id):
    state = client.get(base + "/assessment-decisions", params={"record_id": rec["id"]}).json()
    response = client.post(base + "/assessment-decisions", json={"id": str(uuid4()), "kind": "current_contract",
        "record_id": rec["id"], "expected_version": state["current_contract_version"], "value": file_id,
        "expected_dependency_signature": state["contract_dependency_signature"], "reason": "确认当前合成签署合同"})
    assert response.status_code == 200, response.text
    return response.json()


def test_selected_renewal_preserves_all_probation_periods_and_separate_assessment(api, tmp_path):
    from qian_labor.services.risk_evaluation import RiskEvaluationService
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    old = add_fact(db, aid, "employment.probation.start_date", "2020-01-01")
    add_fact(db, aid, "employment.probation.end_date", "2020-03-01")
    add_fact(db, aid, "employment.probation.periods", [["2020-01-01", "2020-03-01"]])
    assessment = add_fact(db, aid, "employment.probation.assessment_exists", True, unlocated=True)
    newer = clone_contract_file(db, aid)
    for name, value in [("employment.contract.start_date", "2026-01-01"),
            ("employment.contract.end_date", "2028-01-01"), ("employment.contract.type", "fixed"),
            ("employment.probation.periods", [["2026-01-01", "2026-03-01"]])]:
        add_fact(db, aid, name, value, file_id=newer)
    choose_contract(client, base, rec, newer)
    with db.session() as s:
        facts = list(s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)))
        values = RiskEvaluationService._context_facts(s, facts)
        assert values["employment.contract.start_date"].value == "2026-01-01"
        assert "employment.probation.start_date" not in values or values["employment.probation.start_date"].conflicted
        assert len(values["employment.probation.periods"].value) == 2
    for name, value in [("employment.probation.start_date", "2026-01-01"), ("employment.probation.end_date", "2026-03-01")]:
        row = next(r for r in client.get(base + "/effective-facts").json()["items"] if r["fact_type"] == name)
        body = {**revision_body(row, value), "expected_context_signature": row["context_signature"]}
        response = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=body)
        assert response.status_code == 200, response.text
    row = next(r for r in client.get(base + "/effective-facts").json()["items"] if r["id"] == assessment)
    assert row["context_signature"]
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json={
        **revision_body(row, True, "confirm"), "expected_context_signature": row["context_signature"]}).status_code == 200
    with db.session() as s:
        values = RiskEvaluationService._context_facts(s, list(s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))))
        assert values["employment.probation.assessment_exists"].value is True
    choose_contract(client, base, rec, newer)
    row = next(r for r in client.get(base + "/effective-facts").json()["items"] if r["id"] == assessment)
    assert row["human_confirmed"] is False


def test_foreign_corrupt_sources_and_invalid_requests_are_rejected(api, tmp_path):
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    other = company(client)
    foreign = base.replace(c["id"], other["id"])
    assert client.get(foreign + "/effective-facts").status_code == 404
    assert client.post(foreign + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row)).status_code == 404
    for changes in [{"value": "false"}, {"expected_version": True}, {"source_location": {}}, {"reason": " "}]:
        assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json={**revision_body(row), **changes}).status_code == 422
    for suffix in ["effective-facts", "assessment-decisions", "assessment-results"]:
        assert client.get(base + "/" + suffix, params={"page_size": 51}).status_code == 422
    with db.session() as s:
        source = s.get(SourceLocator, row["sources"][0]["id"])
        source.excerpt = "corrupt synthetic quote"
        s.commit()
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row)).status_code == 409
    state = client.get(base + "/assessment-results").json()["assessment_revision"]
    response = client.post(base + "/reevaluate", json={"id": str(uuid4()), "expected_input_revision": state["input_revision"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FACT_SOURCE_INVALID"


def test_revision_cas_restart_and_no_unknown_write_replay(api, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from qian_labor.desktop.app import create_desktop_app
    from test_company_workspaces import HEADERS
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    requests = [revision_body(row, value) for value in [False, True]]
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda body: client.post(base + f'/effective-facts/{row["id"]}/revisions', json=body), requests))
    assert sorted(r.status_code for r in responses) == [200, 409]
    saved = next(body for body, response in zip(requests, responses) if response.status_code == 200)
    # Close the owned lifespan; concurrent same-directory apps are not a restart.
    client.__exit__(None, None, None)
    with TestClient(create_desktop_app(data_dir=tmp_path, launch_token=HEADERS["X-Qian-Desktop-Token"])) as reopened:
        reopened.headers.update(HEADERS)
        result = reopened.get(base + "/effective-facts", params={"request_id": saved["id"]}).json()
        assert result["request_revision"]["id"] == saved["id"]
        assert result["items"][0]["effective_value"] == saved["value"]
        assert reopened.get(base + "/effective-facts", params={"request_id": str(uuid4())}).json()["request_revision"] is None
        history = reopened.get(base + f'/effective-facts/{row["id"]}/revisions').json()
        assert history["total"] == 1 and history["items"][0]["actor"] == "local-user"


def test_assignment_cycle_invalidates_confirmation_only_for_affected_fact(api, tmp_path):
    from qian_labor.models.core import Employee, EmployeeMatchCandidate, EmployeeMatchDecision
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, False)).status_code == 200
    with db.session() as s:
        other = Employee(analysis_id=aid, employee_number="SYN-OTHER", masked_name="合成***", normalized_name="合成***", match_status="confirmed")
        s.add(other)
        s.flush()
        candidate = EmployeeMatchCandidate(analysis_id=aid, file_id=row["file_id"], status="confirmed", score=1, reason="synthetic",
            extracted_fields={"fact_ids": []})
        s.add(candidate)
        s.flush()
        s.add(EmployeeMatchDecision(analysis_id=aid, candidate_id=candidate.id, decision="assign", target_employee_id=other.id, corrected_fields={}))
        s.commit()
    assert client.get(base + "/effective-facts").json()["items"][0]["human_confirmed"] is True
    # Exact existing assignment workflow stores immutable candidate scopes. Simulate its committed A→B→A graph.
    with db.session() as s:
        for target in [other.id, row["employee_id"]]:
            candidate = EmployeeMatchCandidate(analysis_id=aid, file_id=row["file_id"], status="confirmed", score=1, reason="synthetic",
                extracted_fields={"fact_ids": [row["id"]]})
            s.add(candidate)
            s.flush()
            s.add(EmployeeMatchDecision(analysis_id=aid, candidate_id=candidate.id, decision="assign", target_employee_id=target, corrected_fields={}))
            s.get(EmploymentFact, row["id"]).employee_id = target
            s.flush()
        s.commit()
    updated = client.get(base + "/effective-facts").json()["items"][0]
    assert updated["human_confirmed"] is False
    assert updated["original_value"] is True
    assert updated["latest_revision"]["value"] is False
    assert updated["owner_signature"] != row["owner_signature"]


def test_material_freshness_is_separate_from_partial_completeness(api, tmp_path):
    from qian_labor.models.core import UploadedFile
    client, db, c, rec, aid, base = prepared(api, tmp_path)
    state = local_evaluate(client, base)
    assert state["fresh"] is True
    extra = clone_contract_file(db, aid, "unreadable")
    with db.session() as s:
        file = s.get(UploadedFile, extra)
        file.status, file.error_code = "failed", "PROCESSING_FILE_FAILED"
        s.commit()
    state = client.get(base + "/assessment-results").json()["assessment_revision"]
    assert state["fresh"] is False and state["completeness"] == "partial"
    result = local_evaluate(client, base)
    assert result["fresh"] is True and result["completeness"] == "partial" and result["availability"] == "available"
    current_state = client.get(f'/api/company-workspaces/{c["id"]}/current-analysis').json()
    assert current_state["status"] == "partial" and current_state["stale"] is False


def test_ten_employee_real_mixed_inputs_local_corrections_consistent_without_provider(api, tmp_path):
    import re
    from synthetic_mixed_materials import build_materials, EMPLOYEES, OCR_LINE
    from qian_labor.ai.schemas import ExtractionResult
    from qian_labor.jobs.processing import ProcessingPipeline
    from qian_labor.storage.local import LocalStorage
    from qian_labor.security.local_redaction import PrivacyBoundary, LocalImageRedactor, OCRToken
    class SyntheticOCR:
        def extract_tokens(self, content):
            return [OCRToken(OCR_LINE, 10, 20, 350, 12, "synthetic-line")]
    class MixedProvider:
        name, is_external, calls = "offline-effective-fixture", True, 0
        def extract(self, filename, content):
            self.calls += 1
            text = OCR_LINE if filename.endswith((".png", ".jpg")) else content.decode()
            kind, name, value = (
                ("roster", "employment.status", "active") if filename == "roster.csv" else
                ("social_insurance", "employment.social_insurance.present", True) if filename == "social.csv" else
                ("contract", "employment.probation.end_date", "2026-03-01") if filename == "probation.xlsx" else
                ("contract", "employment.contract.end_date", "2027-01-01") if filename.startswith("renewal") else
                ("termination", "employment.termination.notice_exists", True) if filename.startswith("termination") else
                ("contract", "employment.contract.exists", True))
            facts = []
            for line in text.splitlines():
                if line.startswith("[source"): continue
                match = re.search(r"SYN-\d{3}", line)
                if match:
                    facts.append({"employee_id": match.group(), "fact_type": name, "value": value,
                        "confidence": 1, "source": {"file_name": filename, "excerpt": line}})
            return ExtractionResult.model_validate({"document_type": kind, "facts": facts})
    client, db = api
    co = company(client)
    records = {number: record(client, co["id"], number, index) for index, number in enumerate(EMPLOYEES)}
    aid = current(client, co["id"], 10)["analysis_id"]
    base = f'/api/company-workspaces/{co["id"]}/analyses/{aid}'
    paths = []
    for name, content in build_materials().items():
        path = tmp_path / name
        path.write_bytes(content)
        paths.append(str(path))
    assert client.post(f"/api/analyses/{aid}/import-paths", json={"paths": paths}).status_code == 200
    provider = MixedProvider()
    pipeline = ProcessingPipeline(db, LocalStorage(str(client.app.state.storage_root)), provider,
        privacy_boundary=PrivacyBoundary("synthetic-effective-pepper-at-least-32", LocalImageRedactor(SyntheticOCR())))
    pipeline.process(aid)
    while True:
        candidates = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"]
        if not candidates: break
        candidate = candidates[0]
        number = candidate["extracted_fields"]["employee_ids"][0]
        rec = client.get(f'/api/company-workspaces/{co["id"]}/employees/{records[number]["id"]}').json()
        response = select_record(client, aid, candidate, rec)
        assert response.status_code == 200, response.text
    before_calls = provider.calls
    originals = None
    with db.session() as s:
        originals = [(f.id, deepcopy(f.value_json)) for f in s.scalars(select(EmploymentFact).order_by(EmploymentFact.id))]
    for number in EMPLOYEES:
        rows = client.get(base + "/effective-facts", params={"record_id": records[number]["id"], "page_size": 50}).json()
        assert rows["total"] > 0
        for row in rows["items"]:
            if row["fact_type"] in {"employment.status", "employment.contract.exists", "employment.contract.end_date"}:
                value = "2027-02-01" if row["fact_type"].endswith("end_date") else row["effective_value"]
                response = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, value,
                    "correct" if row["fact_type"].endswith("end_date") else "confirm"))
                assert response.status_code == 200, response.text
    result = local_evaluate(client, base)
    # The fixture also returns row-context metadata as an excerpt and leaves
    # unlocated observations. Successful processing is not complete evidence.
    from qian_labor.services.effective_facts import effective_projection
    from qian_labor.services.source_provenance import grounding_requires_review
    with db.session() as session:
        assert any(grounding_requires_review(src.location)
                   for row in effective_projection(session, aid) for src in row.state.sources)
    assert result["fresh"] and result["completeness"] == "partial"
    assert provider.calls == before_calls
    workbench = client.get(f'/api/company-workspaces/{co["id"]}/current').json()
    assert len(workbench["employees"]) == 10
    assert all(employee["assessment_state"] == "evaluated" for employee in workbench["employees"])
    overview = client.get(f"/api/analyses/{aid}/dashboard").json()["overview"]
    report = client.get(f"/api/analyses/{aid}/report").json()
    assert overview["assessment_revision"] == report["assessment_revision"] == workbench["current_analysis"]["assessment_revision"]
    with db.session() as s:
        assert [(f.id, f.value_json) for f in s.scalars(select(EmploymentFact).order_by(EmploymentFact.id))] == originals


def test_actual_matching_reassignment_requires_new_fact_confirmation(api, tmp_path):
    from qian_labor.models.core import AnalysisBatch, EmployeeMatchCandidate
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, False)).status_code == 200
    company_version = client.get(f'/api/company-workspaces/{co["id"]}').json()["version"]
    other = record(client, co["id"], "SYN-002", company_version)
    extract_materials(db, tmp_path, aid, [material(tmp_path, "SYN-002", "roster")])
    candidate = client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"][0]
    assert select_record(client, aid, candidate, other).status_code == 200
    for target in [other, rec]:
        target = client.get(f'/api/company-workspaces/{co["id"]}/employees/{target["id"]}').json()
        with db.session() as s:
            candidate = EmployeeMatchCandidate(analysis_id=aid, file_id=row["file_id"], status="pending", score=0,
                reason="synthetic_manual_reassignment", extracted_fields={"employee_ids": [], "fact_ids": [row["id"]]})
            s.add(candidate)
            s.get(AnalysisBatch, aid).status = "matching_review"
            s.commit()
            cid = candidate.id
        response = client.post(f"/api/analyses/{aid}/matching-decisions", json={"candidate_id": cid,
            "decision": "assign", "employee_id": target["current_binding"]["snapshot_id"],
            "employee_record_id": target["id"], "expected_record_version": target["version"]})
        assert response.status_code == 200, response.text
        changed = next(r for r in client.get(base + "/effective-facts").json()["items"] if r["id"] == row["id"])
        assert changed["human_confirmed"] is False and changed["latest_revision"]["value"] is False
    assert changed["employee_id"] == row["employee_id"]
    assert changed["owner_signature"] != row["owner_signature"]


def test_conflicting_contract_versions_need_selection_and_selected_source_support(api, tmp_path):
    from qian_labor.models.core import RiskFinding
    from qian_labor.services.risk_evaluation import RiskEvaluationService
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    add_fact(db, aid, "employment.status", "active")
    old = add_fact(db, aid, "employment.contract.end_date", "2020-01-01")
    newer = clone_contract_file(db, aid)
    chosen = add_fact(db, aid, "employment.contract.end_date", "2027-01-15", file_id=newer)
    with db.session() as s:
        facts = list(s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)))
        assert RiskEvaluationService._context_facts(s, facts)["employment.contract.end_date"].conflicted
    choose_contract(client, base, rec, newer)
    day = client.get(base + "/assessment-decisions").json()
    assert client.post(base + "/assessment-decisions", json={"id": str(uuid4()), "kind": "check_date",
        "expected_version": day["check_date_version"], "value": "2027-01-01", "reason": "合成核查"}).status_code == 200
    local_evaluate(client, base)
    with db.session() as s:
        finding = s.scalar(select(RiskFinding).where(RiskFinding.analysis_id == aid, RiskFinding.rule_id == "CONTRACT_EXPIRING_30D", RiskFinding.is_current.is_(True)))
        assert old not in finding.trigger_fact_ids and chosen in finding.trigger_fact_ids
        assert all(source.fact_id != old for source in s.scalars(select(SourceLocator).where(SourceLocator.id.in_(finding.source_locator_ids))))
    add_fact(db, aid, "employment.contract.end_date", "2029-01-01", file_id=newer)
    state = client.get(base + "/assessment-decisions", params={"record_id": rec["id"]}).json()
    assert state["current_contract_valid"] is False and state["assessment_revision"]["fresh"] is False
    choose_contract(client, base, rec, newer)
    with db.session() as s:
        facts = list(s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)))
        assert RiskEvaluationService._context_facts(s, facts)["employment.contract.end_date"].conflicted


def test_old_unselected_check_date_and_historical_reads_never_use_today(api, tmp_path):
    from datetime import datetime
    from qian_labor.models.core import AnalysisBatch, AssessmentDecision
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    with db.session() as s:
        for row in s.scalars(select(AssessmentDecision).where(AssessmentDecision.analysis_id == aid)):
            s.delete(row)  # Frozen-v4 equivalent fixture: no explicit date existed.
        s.get(AnalysisBatch, aid).created_at = datetime(2020, 1, 2)
        s.commit()
    for _ in range(2):
        state = client.get(base + "/assessment-results").json()["assessment_revision"]
        assert state["check_date"] == "2020-01-02" and state["check_date_explicit"] is False
    with db.session() as s:
        assert not list(s.scalars(select(AssessmentDecision).where(AssessmentDecision.analysis_id == aid)))


def test_no_evidence_local_result_remains_pending_and_review_only_does_not_stale_inputs(api):
    client, db = api
    co = company(client)
    rec = record(client, co["id"])
    aid = current(client, co["id"], 1)["analysis_id"]
    base = f'/api/company-workspaces/{co["id"]}/analyses/{aid}'
    result = local_evaluate(client, base)
    assert result["availability"] == "none" and result["completeness"] == "pending"
    workbench = client.get(f'/api/company-workspaces/{co["id"]}/current').json()
    assert workbench["employees"][0]["assessment"] is None
    assert workbench["employees"][0]["assessment_state"] == "pending_evidence"


def test_confirmation_does_not_accept_numeric_boolean_and_reviews_only_change_report_revision(api, tmp_path):
    from qian_labor.models.core import RiskFinding
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    response = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, 1, "confirm"))
    assert response.status_code == 422
    add_fact(db, aid, "employment.status", "active")
    assert client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(row, False)).status_code == 200
    before = local_evaluate(client, base)
    with db.session() as s:
        finding = s.scalar(select(RiskFinding).where(RiskFinding.analysis_id == aid,
            RiskFinding.rule_id == "CONTRACT_MISSING_ACTIVE", RiskFinding.is_current.is_(True)))
    response = client.post(f"/api/findings/{finding.id}/reviews", json={"expected_version": finding.version,
        "status": "reviewed", "note": "合成处理意见"})
    assert response.status_code == 200, response.text
    after = client.get(base + "/assessment-results").json()["assessment_revision"]
    assert after["fresh"] and before["input_revision"] == after["input_revision"]
    assert before["report_review_revision"] != after["report_review_revision"]


def test_fixed_contract_without_end_has_no_trusted_current_probation_basis(api, tmp_path):
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    newer = clone_contract_file(db, aid)
    for name, value in [("employment.contract.start_date", "2026-01-01"), ("employment.contract.type", "fixed"),
            ("employment.probation.start_date", "2026-01-01"), ("employment.probation.end_date", "2026-03-01")]:
        add_fact(db, aid, name, value, file_id=newer)
    choose_contract(client, base, rec, newer)
    rows = client.get(base + "/effective-facts").json()["items"]
    assert all(r["basis_pending"] for r in rows if r["fact_type"].startswith("employment.probation."))


@pytest.mark.parametrize("end_support", ["absent", "conflicting", "uncertain"])
def test_indefinite_probation_requires_absent_or_unambiguous_end_support(api, tmp_path, end_support):
    from qian_labor.services.risk_evaluation import RiskEvaluationService
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    newer = clone_contract_file(db, aid)
    for name, value in [("employment.contract.start_date", "2026-01-01"),
            ("employment.contract.type", "indefinite"),
            ("employment.probation.start_date", "2026-01-01"),
            ("employment.probation.end_date", "2026-03-01")]:
        add_fact(db, aid, name, value, file_id=newer)
    if end_support != "absent":
        add_fact(db, aid, "employment.contract.end_date", "2028-01-01", file_id=newer)
        add_fact(db, aid, "employment.contract.end_date",
                 "2029-01-01" if end_support == "conflicting" else "2028-01-01",
                 file_id=newer, unlocated=end_support == "uncertain")
    assessment = add_fact(db, aid, "employment.probation.assessment_exists", True, unlocated=True)
    choose_contract(client, base, rec, newer)
    rows = client.get(base + "/effective-facts").json()["items"]
    probation = [r for r in rows if r["fact_type"].startswith("employment.probation.")]
    assert len(probation) == 3
    assert all(r["basis_pending"] is (end_support != "absent") for r in probation)
    row = next(r for r in probation if r["id"] == assessment)
    assert bool(row["context_signature"]) is (end_support == "absent")
    if end_support == "absent":
        response = client.post(base + f'/effective-facts/{assessment}/revisions', json={
            **revision_body(row, True, "confirm"), "expected_context_signature": row["context_signature"]})
        assert response.status_code == 200, response.text
    with db.session() as s:
        values = RiskEvaluationService._context_facts(s, list(s.scalars(
            select(EmploymentFact).where(EmploymentFact.analysis_id == aid))))
        if end_support == "absent":
            assert values["employment.probation.assessment_exists"].value is True
        else:
            assert values["employment.probation.start_date"].conflicted
            assert "employment.probation.assessment_exists" not in values or values["employment.probation.assessment_exists"].conflicted


def test_parsed_content_hash_only_change_invalidates_freshness_across_consumers(api, tmp_path):
    from qian_labor.models.core import ParsedDocument, UploadedFile
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    before = local_evaluate(client, base)
    assert before["fresh"] is True
    with db.session() as s:
        document = s.scalar(select(ParsedDocument).where(ParsedDocument.file_id == row["file_id"]))
        assert document.content_hash == s.get(UploadedFile, row["file_id"]).sha256
        document.content_hash = "f" * 64
        s.commit()
    response = client.get(base + "/effective-facts").json()
    assert response["items"][0]["source_valid"] is False
    after = response["assessment_revision"]
    assert after["input_revision"] != before["input_revision"]
    assert after["fresh"] is False
    assert after["result_revision"] == before["result_revision"]
    assert after["evaluated_input_revision"] == before["input_revision"]
    assert client.get(base + "/assessment-results").json()["assessment_revision"] == after
    assert client.get(f"/api/analyses/{aid}/dashboard").json()["overview"]["assessment_revision"] == after
    assert client.get(f"/api/analyses/{aid}/report").json()["assessment_revision"] == after
    current_state = client.get(f'/api/company-workspaces/{co["id"]}/current').json()["current_analysis"]
    assert current_state["assessment_revision"] == after
    assert current_state["stale"] is True


@pytest.mark.parametrize("damage", ["location", "file_hash"])
def test_corrupt_source_metadata_cannot_be_fixed_by_confirmation(api, tmp_path, damage):
    from qian_labor.models.core import UploadedFile
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    row = client.get(base + "/effective-facts").json()["items"][0]
    with db.session() as s:
        if damage == "location":
            s.get(SourceLocator, row["sources"][0]["id"]).location = {"_grounding": {"version": "invented", "status": "locally_located"}}
        else:
            s.get(UploadedFile, row["file_id"]).sha256 = "f" * 64
        s.commit()
    changed = client.get(base + "/effective-facts").json()["items"][0]
    assert changed["source_valid"] is False and changed["read_only"] is True
    response = client.post(base + f'/effective-facts/{row["id"]}/revisions', json=revision_body(changed, True, "confirm"))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "FACT_SOURCE_INVALID"


def test_historical_apis_read_only_and_decision_unknown_lookup_scoped(api, tmp_path):
    from test_company_workspaces import snapshot
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    hist_aid, hist_eid = snapshot(db)
    company_version = client.get(f'/api/company-workspaces/{co["id"]}').json()["version"]
    hist_rec = str(uuid4())
    assert client.put(f'/api/company-workspaces/{co["id"]}/analyses/{hist_aid}/binding', json={
        "expected_company_version": company_version, "expected_analysis_version": 0,
        "decisions": [{"snapshot_id": hist_eid, "action": "create", "record": {"id": hist_rec, "display_name": "合成历史人员"}}]}).status_code == 200
    hist_base = f'/api/company-workspaces/{co["id"]}/analyses/{hist_aid}'
    state = client.get(hist_base + "/assessment-results").json()["assessment_revision"]
    assert state["read_only"] is True
    assert client.get(hist_base + "/effective-facts").json()["read_only"] is True
    assert client.post(hist_base + "/reevaluate", json={"id": str(uuid4()), "expected_input_revision": state["input_revision"]}).status_code == 409
    assert client.post(hist_base + "/assessment-decisions", json={"id": str(uuid4()), "kind": "check_date", "value": "2027-01-01",
        "expected_version": 0, "reason": "合成"}).status_code == 409
    current_day = client.get(base + "/assessment-decisions").json()
    request_id = str(uuid4())
    body = {"id": request_id, "kind": "check_date", "value": "2027-01-01", "expected_version": current_day["check_date_version"], "reason": "合成"}
    assert client.post(base + "/assessment-decisions", json=body).status_code == 200
    assert client.post(base + "/assessment-decisions", json=body).status_code == 409
    assert client.get(base + "/assessment-decisions", params={"request_id": request_id}).json()["request_decision"]["id"] == request_id
    assert client.get(hist_base + "/assessment-decisions", params={"request_id": request_id}).status_code == 404


def test_contract_classification_change_invalidates_explicit_selection(api, tmp_path):
    from qian_labor.models.core import UploadedFile
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    file_id = client.get(base + "/effective-facts").json()["items"][0]["file_id"]
    choose_contract(client, base, rec, file_id)
    with db.session() as s:
        s.get(UploadedFile, file_id).classified_kind = "social_insurance"
        s.commit()
    state = client.get(base + "/assessment-decisions", params={"record_id": rec["id"]}).json()
    assert state["current_contract_valid"] is False


def test_foreign_employee_attribution_is_not_available_current_evidence(api, tmp_path):
    from test_company_workspaces import snapshot
    client, db, co, rec, aid, base = prepared(api, tmp_path)
    _, foreign_employee = snapshot(db)
    with db.session() as s:
        fact = s.scalar(select(EmploymentFact).where(EmploymentFact.analysis_id == aid))
        fact.employee_id = foreign_employee
        s.commit()
    state = client.get(base + "/assessment-results").json()["assessment_revision"]
    assert state["availability"] == "none"
    response = client.post(base + "/reevaluate", json={"id": str(uuid4()), "expected_input_revision": state["input_revision"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "FACT_OWNERSHIP_INVALID"
