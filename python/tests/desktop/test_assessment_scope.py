"""Synthetic evidence only: scoped evaluation must match every material view."""
import hashlib

import pytest
from sqlalchemy import select

from qian_labor.database import create_database
from qian_labor.models.core import AnalysisBatch, Employee, EmploymentFact, SourceLocator, UploadedFile
from qian_labor.services.analyses import AnalysisService
from qian_labor.services.dashboard import DashboardService
from qian_labor.services.report import ReportService
from qian_labor.services.risk_evaluation import RiskEvaluationService


def seed(profile="labor_materials_v1", status="active", extra=None):
    db = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    analysis = AnalysisService(db).create("合成范围测试", "合成公司", assessment_profile=profile)
    with db.session() as session:
        person = Employee(analysis_id=analysis.id, masked_name="合成员工", normalized_name="合成员工",
                          employment_status=status, match_status="confirmed")
        session.add(person)
        session.flush()
        groups = {
            "contract": {"employment.status": status, "employment.contract.exists": True},
            "social_insurance": {"employment.social_insurance.present": True},
        }
        for kind, values in (extra or {}).items():
            groups.setdefault(kind, {}).update(values)
        for kind, values in groups.items():
            file = UploadedFile(analysis_id=analysis.id, original_filename=f"synthetic-{kind}.csv",
                storage_key=f"{analysis.id}/{kind}", mime_type="text/csv", extension=".csv",
                size_bytes=1, sha256=hashlib.sha256(kind.encode()).hexdigest(), classified_kind=kind)
            session.add(file)
            session.flush()
            for name, value in values.items():
                fact = EmploymentFact(analysis_id=analysis.id, employee_id=person.id, file_id=file.id,
                    fact_type=name, value_json=value, normalized_value_json=value, extraction_method="fixture",
                    confidence=1, verification_status="confirmed", dedupe_key=hashlib.sha256((kind+name).encode()).hexdigest())
                session.add(fact)
                session.flush()
                session.add(SourceLocator(analysis_id=analysis.id, file_id=file.id, fact_id=fact.id,
                    locator_type="cell", location={"row": 1}, excerpt="合成来源", content_hash="a"*64))
        session.commit()
        return db, analysis.id, person.id


def test_desktop_scope_coverage_matches_evaluation_dashboard_employee_and_report():
    db, aid, eid = seed(extra={"contract": {"employment.material_coverage": 0, "analysis.minimum_core_coverage": 0}})
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    assert "MATERIAL_COVERAGE_LOW" not in {f.rule_id for f in findings}
    dashboard = DashboardService(db).get(aid)
    assert dashboard["material_coverage"]["overall"] == 1
    assert {i["code"] for i in dashboard["material_coverage"]["items"]} == {"contract", "social_insurance", "termination"}
    assert DashboardService(db).employees(aid)["items"][0]["material_coverage"] == 1
    assert ReportService(db).get(aid)["material_coverage"] == dashboard["material_coverage"]
    assert AnalysisService(db).get(aid).coverage_rate == 1
    scope = dashboard["assessment_scope"]
    assert scope["identifier"] == "labor_materials_v1"
    assert scope["excluded_rule_codes"] == ["R09", "R13", "R14", "R15"]
    assert scope["payroll_evaluated"] is scope["attendance_evaluated"] is False
    assert "R16" in scope["not_evaluated_reasons"]
    assert DashboardService(db).employee_detail(aid, eid)["assessment_scope"] == scope
    assert ReportService(db).get(aid)["assessment_scope"] == scope


def test_excluded_payroll_attendance_and_mixed_r16_cannot_trigger_findings():
    db, aid, _ = seed(status="terminated", extra={"payroll": {
        "employment.post_termination_record": True, "employment.pay.contract_wage": 5000,
        "employment.pay.actual_wage": 1, "employment.pay.comparable": True,
        "employment.attendance.overtime_hours": 80, "employment.pay.overtime_evidence": False,
        "employment.attendance_payroll.mismatch": True, "employment.attendance.present": False}})
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    forbidden = {"PAY_CONTRACT_ACTUAL_MISMATCH", "OVERTIME_WITHOUT_PAY_EVIDENCE", "ATTENDANCE_PAYROLL_MISMATCH",
                 "ACTIVE_WITHOUT_ATTENDANCE_RECORD", "TERMINATED_STILL_PAID_OR_INSURED"}
    assert not forbidden.intersection(f.rule_id for f in findings)


def test_r18_accepts_settlement_document_without_payroll_table():
    db, aid, _ = seed(status="terminated", extra={"termination": {
        "employment.termination.settlement_materials": ["final_pay", "separation_certificate", "handover"]}})
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    assert "TERMINATION_MISSING_SETTLEMENT_RECORD" not in {f.rule_id for f in findings}


def test_generic_default_and_explicit_legacy_keep_old_scope():
    db = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    assert AnalysisService(db).create("合成旧分析", "合成公司").assessment_profile == "legacy_full_v1"
    db, aid, _ = seed(profile="legacy_full_v1")
    assert DashboardService(db).get(aid)["material_coverage"]["overall"] == .5
    assert DashboardService(db).get(aid)["assessment_scope"]["identifier"] == "legacy_full_v1"


def test_unknown_profile_rejected_before_analysis_creation():
    db = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    with pytest.raises(ValueError, match="ASSESSMENT_PROFILE_UNSUPPORTED"):
        AnalysisService(db).create("合成", "合成", assessment_profile="future")
    with db.session() as session:
        assert session.scalar(select(AnalysisBatch)) is None


@pytest.mark.parametrize("status", ["unknown", ""])
def test_unknown_employee_scope_stays_pending(status):
    db, aid, _ = seed(status=status)
    RiskEvaluationService(db).evaluate_analysis(aid)
    coverage = DashboardService(db).get(aid)["material_coverage"]
    assert coverage["scope_pending"] is True
    assert all(not item["not_applicable"] for item in coverage["items"])


def test_desktop_creation_selects_new_profile_and_does_not_relabel_legacy(tmp_path):
    from fastapi.testclient import TestClient
    from qian_labor.desktop.app import create_desktop_app
    app = create_desktop_app(data_dir=tmp_path, launch_token="synthetic-scope-token")
    headers = {"X-Qian-Desktop-Token": "synthetic-scope-token"}
    legacy = AnalysisService(app.state.database).create("合成旧分析", "合成")
    with TestClient(app) as client:
        response = client.post("/api/analyses", json={"name": "合成新分析"}, headers=headers)
        assert response.status_code == 201
        assert response.json()["assessment_scope"]["identifier"] == "labor_materials_v1"
        for aid, profile in ((response.json()["id"], "labor_materials_v1"), (legacy.id, "legacy_full_v1")):
            dashboard = client.get(f"/api/analyses/{aid}/dashboard", headers=headers).json()
            assert dashboard["overview"]["assessment_scope"]["identifier"] == profile
            assert dashboard["overview"]["material_coverage"]["scope_pending"] is True
            report = client.get(f"/api/analyses/{aid}/report", headers=headers).json()
            assert report["assessment_scope"]["identifier"] == profile


def test_employee_without_any_facts_gets_insufficient_data_not_empty_success():
    db = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    analysis = AnalysisService(db).create("合成", "合成", assessment_profile="labor_materials_v1")
    with db.session() as session:
        session.add(Employee(analysis_id=analysis.id, masked_name="合成", normalized_name="合成", employment_status="unknown"))
        session.commit()
    findings = RiskEvaluationService(db).evaluate_analysis(analysis.id)
    assert any(f.rule_id == "MATERIAL_COVERAGE_LOW" and f.assessment_status == "insufficient_data" for f in findings)


def test_unknown_stored_profile_fails_closed_for_reads_and_evaluation():
    db = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    with db.session() as session:
        analysis = AnalysisBatch(name="合成未知版本", assessment_profile="future_profile")
        session.add(analysis)
        session.commit()
        aid = analysis.id
    for call in (AnalysisService(db).get, DashboardService(db).get,
                 DashboardService(db).findings, RiskEvaluationService(db).evaluate_analysis):
        with pytest.raises(ValueError, match="ASSESSMENT_PROFILE_UNSUPPORTED"):
            call(aid)


def test_r20_derived_missing_coverage_retains_actual_source_trace():
    db, aid, _ = seed(extra={"social_insurance": {"employment.social_insurance.present": False},
                           "contract": {"employment.material_coverage": 1, "analysis.minimum_core_coverage": 1}})
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    coverage_finding = next(f for f in findings if f.rule_id == "MATERIAL_COVERAGE_LOW")
    assert coverage_finding.assessment_status == "insufficient_data"
    assert coverage_finding.source_locator_ids and coverage_finding.trigger_fact_ids
    assert DashboardService(db).get(aid)["material_coverage"]["overall"] == .5
    assert AnalysisService(db).get(aid).coverage_rate == .5


def test_model_percentage_sources_cannot_substitute_material_evidence():
    db, aid, _ = seed(extra={"contract": {"employment.material_coverage": 1, "analysis.minimum_core_coverage": 1}})
    with db.session() as session:
        for fact in session.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == aid)):
            if fact.fact_type not in {"employment.material_coverage", "analysis.minimum_core_coverage"}:
                for source in session.scalars(select(SourceLocator).where(SourceLocator.fact_id == fact.id)):
                    session.delete(source)
                session.delete(fact)
        session.commit()
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    finding = next(f for f in findings if f.rule_id == "MATERIAL_COVERAGE_LOW")
    assert finding.assessment_status == "insufficient_data"
    assert not finding.source_locator_ids and not finding.trigger_fact_ids
    assert DashboardService(db).get(aid)["material_coverage"]["overall"] == 0


def test_termination_percentage_is_not_material_coverage():
    db, aid, _ = seed(status="terminated", extra={"termination": {"employment.material_coverage": 1}})
    assert DashboardService(db).get(aid)["material_coverage"]["overall"] == 0


def test_unrelated_pending_contract_fact_does_not_gate_coverage():
    db, aid, _ = seed(extra={"contract": {"employment.contract.note": "合成无关备注"}})
    with db.session() as session:
        fact = session.scalar(select(EmploymentFact).where(EmploymentFact.fact_type == "employment.contract.note"))
        fact.verification_status = "pending_review"
        session.commit()
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    assert "MATERIAL_COVERAGE_LOW" not in {f.rule_id for f in findings}


def test_changed_classification_reopens_derived_coverage_review():
    from qian_labor.services.finding_review import FindingReviewService
    db, aid, _ = seed(extra={"social_insurance": {"employment.social_insurance.present": False}})
    finding = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid) if f.rule_id == "MATERIAL_COVERAGE_LOW")
    FindingReviewService(db).review(finding.id, expected_version=finding.version, status="reviewed", note="合成复核")
    with db.session() as session:
        file = session.scalar(select(UploadedFile).where(UploadedFile.classified_kind == "contract"))
        file.classified_kind = "termination"
        session.commit()
    changed = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid) if f.rule_id == "MATERIAL_COVERAGE_LOW")
    assert changed.review_status == "open"
    assert changed.version == 2


def test_unrelated_note_is_not_cited_and_does_not_reopen_coverage_review():
    from qian_labor.services.finding_review import FindingReviewService
    db, aid, _ = seed(extra={"social_insurance": {"employment.social_insurance.present": False},
                           "contract": {"employment.contract.note": "合成备注"}})
    first = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid) if f.rule_id == "MATERIAL_COVERAGE_LOW")
    FindingReviewService(db).review(first.id, expected_version=first.version, status="reviewed", note="合成复核")
    with db.session() as session:
        note = session.scalar(select(EmploymentFact).where(EmploymentFact.fact_type == "employment.contract.note"))
        note.verification_status = "pending_review"
        assert note.id not in first.trigger_fact_ids
        session.commit()
    second = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid) if f.rule_id == "MATERIAL_COVERAGE_LOW")
    assert second.review_status == "reviewed" and second.version == 1


def test_changed_aggregate_denominator_reopens_even_when_source_ids_stay_same():
    from qian_labor.services.finding_review import FindingReviewService
    db, aid, eid = seed(extra={"social_insurance": {"employment.social_insurance.present": False}})
    first = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid) if f.rule_id == "MATERIAL_COVERAGE_LOW")
    FindingReviewService(db).review(first.id, expected_version=first.version, status="reviewed", note="合成复核")
    with db.session() as session:
        session.add(Employee(analysis_id=aid, masked_name="合成新增", normalized_name="合成新增", employment_status="active"))
        session.commit()
    second = next(f for f in RiskEvaluationService(db).evaluate_analysis(aid)
                  if f.rule_id == "MATERIAL_COVERAGE_LOW" and f.employee_id == eid)
    assert set(first.source_locator_ids) == set(second.source_locator_ids)
    assert second.review_status == "open" and second.version == 2


def test_foreign_owned_file_cannot_contribute_scope_status_or_r20_source():
    db, aid, eid = seed(status="terminated")
    foreign = AnalysisService(db).create("合成另批次", "合成")
    with db.session() as session:
        employee = session.get(Employee, eid)
        employee.employment_status = "unknown"
        file = session.scalar(select(UploadedFile).where(UploadedFile.analysis_id == aid, UploadedFile.classified_kind == "contract"))
        invalid_sources = set(session.scalars(select(SourceLocator.id).where(SourceLocator.file_id == file.id)))
        file.analysis_id = foreign.id
        session.commit()
    findings = RiskEvaluationService(db).evaluate_analysis(aid)
    r20 = next(f for f in findings if f.rule_id == "MATERIAL_COVERAGE_LOW")
    assert invalid_sources.isdisjoint(r20.source_locator_ids)
    assert r20.assessment_status == "insufficient_data"
    overview = DashboardService(db).get(aid)
    assert overview["material_coverage"]["scope_pending"] is True
    assert DashboardService(db).employees(aid)["items"][0]["employment_status"] == "unknown"
    assert DashboardService(db).employee_detail(aid, eid)["employee"]["employment_status"] == "unknown"
