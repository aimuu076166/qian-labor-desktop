"""Synthetic occurrence and public provenance boundary regressions."""
import json

import pytest
from sqlalchemy import select

from test_finding_review_api import HEADERS, post_review, review_case, stored_state
from qian_labor.ai.schemas import ExtractionResult
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.desktop.schemas import MatchDecisionRequest
from qian_labor.matching.service import EmployeeMatcher
from qian_labor.security.local_redaction import PreparedProviderInput
from qian_labor.models.core import (
    AnalysisBatch, CompanyAnalysisBinding, CompanyWorkspace, Employee, EmployeeMatchCandidate, EmployeeRecord, EmploymentFact, RiskFinding, SourceLocator, UploadedFile,
)
from qian_labor.services.risk_evaluation import RiskEvaluationService
from qian_labor.storage.local import LocalStorage


def extracted(*, rows=(1, 2), number="SYN-R01", values=None):
    return ExtractionResult.model_validate({"employee_number": number, "facts": [
        {"employee_id": number, "fact_type": "employment.contract.exists",
         "value": values[i] if values else True, "confidence": 1,
         "source": {"file_name": "虚构.csv", "row": row, "column": "A", "excerpt": "合成条款"}}
        for i, row in enumerate(rows)
    ]})


def test_repeated_fact_keeps_each_occurrence_without_rewriting_original(review_case, tmp_path):
    _, database, analysis_id, _, _ = review_case
    pipeline = ProcessingPipeline(database, LocalStorage(tmp_path))
    with database.session() as session:
        uploaded = session.scalar(select(UploadedFile))
        result = extracted()
        pipeline._persist_result(session, analysis_id, uploaded, result)
        session.flush()
        fact = session.scalar(select(EmploymentFact).where(EmploymentFact.extraction_method == pipeline.provider.name))
        fact_id = fact.id
        sources = list(session.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact_id)))
        assert {s.location["row"] for s in sources} == {1, 2}
        # Reordered persisted JSON and repeated extraction are the same location.
        sources[0].location = dict(reversed(list(sources[0].location.items())))
        fact.normalized_value_json = False
        fact.verification_status = "confirmed"
        pipeline._persist_result(session, analysis_id, uploaded, result)
        pipeline._persist_result(session, analysis_id, uploaded, extracted(rows=(3,), values=[False]))
        session.flush()
        assert len(list(session.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact_id)))) == 2
        assert fact.value_json is True and fact.normalized_value_json is False
        assert fact.verification_status == "confirmed"
        assert len(list(session.scalars(select(EmploymentFact).where(
            EmploymentFact.extraction_method == pipeline.provider.name)))) == 2


@pytest.mark.parametrize("damage", ["source_analysis", "file_analysis", "file_mismatch", "fact_analysis",
                                  "employee", "foreign_employee", "foreign_source", "unrelated_fact",
                                  "unrelated_trigger_type", "missing"])
def test_corrupt_declared_source_is_hidden_in_detail_report_and_cannot_be_reviewed(review_case, damage):
    client, database, analysis_id, employee_id, finding_id = review_case
    with database.session() as session:
        companies = [CompanyWorkspace(display_name="同名合成企业") for _ in range(2)]
        session.add_all(companies)
        other = AnalysisBatch(name="同名合成企业", status="completed")
        session.add(other)
        session.flush()
        session.add_all([CompanyAnalysisBinding(analysis_id=aid, company_id=company.id)
                         for aid, company in zip((analysis_id, other.id), companies)])
        local = session.get(AnalysisBatch, analysis_id)
        local.name = other.name
        foreign_employee = Employee(analysis_id=other.id, masked_name="合成员工", normalized_name="合成员工")
        peer = Employee(analysis_id=analysis_id, masked_name="合成员工", normalized_name="合成员工")
        session.add_all([foreign_employee, peer])
        session.flush()
        finding = session.get(RiskFinding, finding_id)
        source = session.get(SourceLocator, finding.source_locator_ids[0])
        fact = session.get(EmploymentFact, source.fact_id)
        uploaded = session.get(UploadedFile, source.file_id)
        bad_id = source.id
        if damage == "source_analysis":
            source.analysis_id = other.id
        elif damage == "file_analysis":
            uploaded.analysis_id = other.id
        elif damage == "file_mismatch":
            other_file = UploadedFile(analysis_id=analysis_id, original_filename="禁止泄漏.csv",
                storage_key="synthetic/foreign.csv", mime_type="text/csv", extension=".csv",
                size_bytes=1, sha256="c" * 64)
            session.add(other_file)
            session.flush()
            source.file_id = other_file.id
        elif damage == "fact_analysis":
            fact.analysis_id = other.id
        elif damage in {"employee", "foreign_employee"}:
            fact.employee_id = peer.id if damage == "employee" else foreign_employee.id
        elif damage == "foreign_source":
            source.analysis_id = fact.analysis_id = uploaded.analysis_id = other.id
            fact.employee_id = foreign_employee.id
        elif damage == "unrelated_fact":
            unrelated = EmploymentFact(analysis_id=analysis_id, employee_id=employee_id,
                file_id=uploaded.id, fact_type="employment.unrelated", value_json=True,
                normalized_value_json=True, extraction_method="fixture", confidence=1,
                verification_status="confirmed", dedupe_key="unrelated")
            session.add(unrelated)
            session.flush()
            source.fact_id = unrelated.id
        elif damage == "unrelated_trigger_type":
            fact.fact_type = "employment.unrelated"
        else:
            session.delete(source)
        if damage != "missing":
            source.excerpt = "禁止泄漏原文"
            source.location = {"row": 9999}
        session.commit()
    before = stored_state(database, finding_id)
    detail = client.get(f"/api/findings/{finding_id}", headers=HEADERS).json()
    assert bad_id not in {s["id"] for s in detail["sources"]}
    assert "禁止泄漏" not in json.dumps(detail, ensure_ascii=False)
    report = client.get(f"/api/analyses/{analysis_id}/report", headers=HEADERS).json()
    item = next(f for f in report["findings"] if f["id"] == finding_id)
    assert {"row": 9999} not in [s["location"] for s in item["sources"]]
    assert "禁止泄漏" not in json.dumps(item, ensure_ascii=False)
    assert stored_state(database, finding_id) == before
    assert post_review(client, finding_id).status_code == 409
    assert stored_state(database, finding_id) == before


def test_source_free_insufficient_data_remains_actionable(review_case):
    client, database, _, _, _ = review_case
    with database.session() as session:
        finding = session.scalar(select(RiskFinding).where(RiskFinding.assessment_status == "insufficient_data"))
        assert finding is not None
        finding.source_locator_ids = []
        finding.trigger_fact_ids = []
        finding_id = finding.id
        session.commit()
    assert post_review(client, finding_id, status="needs_material").status_code == 200


def test_ambiguous_scopes_stay_isolated_and_matching_retry_preserves_fact_ids(review_case, tmp_path):
    _, database, analysis_id, employee_id, _ = review_case
    pipeline = ProcessingPipeline(database, LocalStorage(tmp_path))
    prepared = [PreparedProviderInput(f"synthetic-scope-{i}", b"synthetic", {}) for i in (1, 2)]
    result = extracted(number=None)
    with database.session() as session:
        uploaded = session.scalar(select(UploadedFile))
        for scope in prepared:
            pipeline._persist_result(session, analysis_id, uploaded, result, scope)
        session.flush()
        candidates = list(session.scalars(select(EmployeeMatchCandidate)))
        assert len(candidates) == 2
        scopes = [set(c.extracted_fields["fact_ids"]) for c in candidates]
        assert all(len(scope) == 1 for scope in scopes) and not scopes[0].intersection(scopes[1])
        candidate = next(c for c in candidates if c.extracted_fields["source_scope_key"] == prepared[0].filename)
        candidate_id, fact_ids = candidate.id, candidate.extracted_fields["fact_ids"]
        session.get(AnalysisBatch, analysis_id).status = "matching_review"
        session.commit()
    EmployeeMatcher(database).decide(analysis_id, MatchDecisionRequest(
        decision="assign", candidate_id=candidate_id, employee_id=employee_id, fact_ids=fact_ids))
    with database.session() as session:
        before = [(f.id, f.employee_id) for f in session.scalars(select(EmploymentFact).order_by(EmploymentFact.id))]
        uploaded = session.scalar(select(UploadedFile))
        pipeline._persist_result(session, analysis_id, uploaded, result, prepared[0])
        session.flush()
        after = [(f.id, f.employee_id) for f in session.scalars(select(EmploymentFact).order_by(EmploymentFact.id))]
        assert after == before
        assert session.get(EmploymentFact, fact_ids[0]).employee_id == employee_id
        assert session.get(EmployeeMatchCandidate, candidate_id).status == "confirmed"
        for scope in scopes:
            assert len(list(session.scalars(select(SourceLocator).where(SourceLocator.fact_id.in_(scope))))) == 2


def test_retry_after_current_company_identity_confirmation_reuses_original_fact(review_case, tmp_path):
    _, database, analysis_id, employee_id, _ = review_case
    pipeline = ProcessingPipeline(database, LocalStorage(tmp_path))
    with database.session() as session:
        company = CompanyWorkspace(display_name="合成当前企业")
        session.add(company)
        session.flush()
        session.add(CompanyAnalysisBinding(analysis_id=analysis_id, company_id=company.id, role="current"))
        record = EmployeeRecord(company_id=company.id, masked_name="合成员工", employee_number="SYN-R01")
        session.add(record)
        session.flush()
        record_id = record.id
        uploaded = session.scalar(select(UploadedFile))
        pipeline._persist_result(session, analysis_id, uploaded, extracted())
        session.flush()
        candidate = session.scalar(select(EmployeeMatchCandidate).where(EmployeeMatchCandidate.status == "pending"))
        fact_ids = candidate.extracted_fields["fact_ids"]
        candidate_id = candidate.id
        session.get(AnalysisBatch, analysis_id).status = "matching_review"
        session.commit()
    EmployeeMatcher(database).decide(analysis_id, MatchDecisionRequest(
        decision="assign", candidate_id=candidate_id, employee_id=employee_id, fact_ids=fact_ids,
        employee_record_id=record_id, expected_record_version=0))
    with database.session() as session:
        before = [(f.id, f.employee_id) for f in session.scalars(select(EmploymentFact).order_by(EmploymentFact.id))]
        uploaded = session.scalar(select(UploadedFile))
        pipeline._persist_result(session, analysis_id, uploaded, extracted())
        session.flush()
        assert [(f.id, f.employee_id) for f in session.scalars(select(EmploymentFact).order_by(EmploymentFact.id))] == before
        assert session.get(EmployeeMatchCandidate, candidate_id).extracted_fields["fact_ids"] == fact_ids


def test_derived_coverage_keeps_cross_employee_contributors_review_safe(review_case):
    client, database, analysis_id, _, _ = review_case
    with database.session() as session:
        analysis = AnalysisBatch(name="合成覆盖率", status="evaluating", assessment_profile="labor_materials_v1")
        session.add(analysis)
        session.flush()
        analysis_id = analysis.id
        peer = Employee(analysis_id=analysis_id, masked_name="合成乙", normalized_name="合成乙",
                        employee_number="SYN-PEER", match_status="confirmed")
        session.add(peer)
        session.flush()
        uploaded = UploadedFile(analysis_id=analysis_id, original_filename="合成覆盖.csv",
            storage_key="synthetic/coverage.csv", mime_type="text/csv", extension=".csv",
            size_bytes=1, sha256="e" * 64)
        session.add(uploaded)
        session.flush()
        fact = EmploymentFact(analysis_id=analysis_id, employee_id=peer.id, file_id=uploaded.id,
            fact_type="employment.status", value_json="active", normalized_value_json="active",
            extraction_method="fixture", confidence=1, verification_status="confirmed", dedupe_key="peer")
        session.add(fact)
        session.flush()
        source = SourceLocator(analysis_id=analysis_id, file_id=uploaded.id, fact_id=fact.id,
            locator_type="cell", location={"row": 8}, excerpt="合成乙在职", content_hash="d" * 64)
        session.add(source)
        owner = Employee(analysis_id=analysis_id, masked_name="合成甲", normalized_name="合成甲",
                         employee_number="SYN-OWNER", match_status="confirmed")
        session.add(owner)
        session.flush()
        own_fact = EmploymentFact(analysis_id=analysis_id, employee_id=owner.id, file_id=uploaded.id,
            fact_type="employment.status", value_json="active", normalized_value_json="active",
            extraction_method="fixture", confidence=1, verification_status="confirmed", dedupe_key="owner")
        session.add(own_fact)
        session.flush()
        session.add(SourceLocator(analysis_id=analysis_id, file_id=uploaded.id, fact_id=own_fact.id,
            locator_type="cell", location={"row": 9}, excerpt="合成甲在职", content_hash="f" * 64))
        session.commit()
        source_id = source.id
        owner_id = owner.id
    findings = RiskEvaluationService(database).evaluate_analysis(analysis_id)
    coverage = next(f for f in findings if f.rule_id == "MATERIAL_COVERAGE_LOW" and f.employee_id == owner_id)
    detail = client.get(f"/api/findings/{coverage.id}", headers=HEADERS).json()
    assert source_id in {s["id"] for s in detail["sources"]}
    assert post_review(client, coverage.id, version=coverage.version).status_code == 200
