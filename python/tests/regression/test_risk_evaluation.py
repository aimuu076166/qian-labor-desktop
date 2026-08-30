import hashlib
import json

from sqlalchemy import func, select

from qian_labor.database import create_database
from qian_labor.models.core import (
    AnalysisBatch,
    Employee,
    EmploymentFact,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)
from qian_labor.services.risk_evaluation import RiskEvaluationService


def test_evaluation_persists_traceable_findings_once_and_updates_aggregates() -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    values = {
        "employment.status": "active",
        "employment.start_date": "2026-01-01",
        "employment.contract.exists": False,
        "employment.contract.end_date": "2026-09-23",
        "employment.evidence_after_contract_end": False,
        "employment.entities": ["虚构甲公司"],
        "employment.contract.term_readable": True,
        "employment.contract.start_date": "2026-01-01",
        "employment.contract.type": "fixed",
        "employment.probation.start_date": "2026-01-01",
        "employment.probation.end_date": "2026-01-31",
        "employment.probation.periods": [["2026-01-01", "2026-01-31"]],
        "employment.probation.assessment_exists": True,
        "employment.pay.contract_wage": 5000,
        "employment.pay.actual_wage": 5000,
        "employment.pay.comparable": True,
        "employment.social_insurance.present": True,
        "employment.social_insurance.period_matches": True,
        "employment.contract.employer": "虚构甲公司",
        "employment.social_insurance.entity": "虚构甲公司",
        "employment.entity_mismatch_explained": False,
        "employment.social_insurance.waiver_language": False,
        "employment.attendance.overtime_hours": 0,
        "employment.attendance.overtime_type": "rest_day",
        "employment.pay.overtime_evidence": False,
        "employment.attendance.comp_time_evidence": False,
        "employment.attendance_payroll.mismatch": False,
        "employment.attendance.present": True,
        "employment.post_termination_record": False,
        "employment.termination.occurred": False,
        "employment.termination.notice_exists": True,
        "employment.termination.delivery_exists": True,
        "employment.termination.settlement_materials": [
            "final_pay",
            "handover",
            "separation_certificate",
        ],
        "employment.identity.match_status": "confirmed",
        "employment.material_coverage": 0.8,
        "analysis.minimum_core_coverage": 0.8,
    }
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构规则测试", status="evaluating")
        session.add(analysis)
        session.flush()
        employee = Employee(
            analysis_id=analysis.id,
            masked_name="虚构员工甲",
            normalized_name="虚构员工甲",
            employee_number="F-001",
            match_status="confirmed",
        )
        uploaded = UploadedFile(
            analysis_id=analysis.id,
            original_filename="虚构材料.csv",
            storage_key=f"analyses/{analysis.id}/fictional.csv",
            mime_type="text/csv",
            extension=".csv",
            size_bytes=100,
            sha256="a" * 64,
        )
        session.add_all([employee, uploaded])
        session.flush()
        for index, (fact_type, value) in enumerate(values.items()):
            fact = EmploymentFact(
                analysis_id=analysis.id,
                employee_id=employee.id,
                file_id=uploaded.id,
                fact_type=fact_type,
                value_json=value,
                normalized_value_json=value,
                extraction_method="fixture",
                confidence=1,
                verification_status="confirmed",
                dedupe_key=hashlib.sha256(f"{index}:{fact_type}".encode()).hexdigest(),
            )
            session.add(fact)
            session.flush()
            session.add(
                SourceLocator(
                    analysis_id=analysis.id,
                    file_id=uploaded.id,
                    fact_id=fact.id,
                    locator_type="cell",
                    location={"row": index + 1},
                    excerpt="虚构规则测试值",
                    content_hash=hashlib.sha256(json.dumps(value).encode()).hexdigest(),
                )
            )
        session.commit()
        analysis_id = analysis.id

    first = RiskEvaluationService(database).evaluate_analysis(analysis_id)
    second = RiskEvaluationService(database).evaluate_analysis(analysis_id)

    assert first
    assert len(first) == len(second)
    assert all(item.source_locator_ids and item.trigger_fact_ids for item in first)
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RiskFinding)) == len(first)
        analysis = session.get(AnalysisBatch, analysis_id)
        assert analysis is not None
        assert analysis.status == "completed"
        assert analysis.coverage_rate == 0.8
        reminder = session.scalar(
            select(RiskFinding).where(RiskFinding.rule_id == "CONTRACT_EXPIRING_30D")
        )
        assert reminder is not None
        assert reminder.assessment_status == "management_reminder"
        assert reminder.legal_basis["basis_type"] == "system_management_parameter"
        assert reminder.legal_basis["last_verified_at"] == "2026-08-24"
        assert reminder.legal_basis["effective_date"] == "2026-08-24"


def test_r18_internal_handover_gap_is_management_reminder_not_legal_conclusion() -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构离职管理提醒", status="evaluating")
        session.add(analysis)
        session.flush()
        employee = Employee(
            analysis_id=analysis.id,
            masked_name="虚构离职员工",
            normalized_name="虚构离职员工",
            employee_number="F-R18",
            match_status="confirmed",
        )
        uploaded = UploadedFile(
            analysis_id=analysis.id,
            original_filename="虚构离职材料.csv",
            storage_key=f"analyses/{analysis.id}/fictional-r18.csv",
            mime_type="text/csv",
            extension=".csv",
            size_bytes=100,
            sha256="b" * 64,
        )
        session.add_all([employee, uploaded])
        session.flush()
        values = {
            "employment.status": "terminated",
            "employment.termination.settlement_materials": [
                "final_pay",
                "separation_certificate",
            ],
        }
        for index, (fact_type, value) in enumerate(values.items()):
            fact = EmploymentFact(
                analysis_id=analysis.id,
                employee_id=employee.id,
                file_id=uploaded.id,
                fact_type=fact_type,
                value_json=value,
                normalized_value_json=value,
                extraction_method="fixture",
                confidence=1,
                verification_status="confirmed",
                dedupe_key=hashlib.sha256(f"r18:{index}".encode()).hexdigest(),
            )
            session.add(fact)
            session.flush()
            session.add(
                SourceLocator(
                    analysis_id=analysis.id,
                    file_id=uploaded.id,
                    fact_id=fact.id,
                    locator_type="field",
                    location={"field": fact_type},
                    excerpt="虚构离职材料字段",
                    content_hash=hashlib.sha256(fact_type.encode()).hexdigest(),
                )
            )
        session.commit()
        analysis_id = analysis.id

    RiskEvaluationService(database).evaluate_analysis(analysis_id)

    with database.session() as session:
        finding = session.scalar(
            select(RiskFinding).where(
                RiskFinding.rule_id == "TERMINATION_MISSING_SETTLEMENT_RECORD"
            )
        )
        assert finding is not None
        assert finding.assessment_status == "management_reminder"
        assert finding.legal_basis["basis_type"] == "system_management_parameter"
        assert finding.legal_basis["legal_sources"] == []
        assert finding.legal_basis["management_parameters"][0]["name"] == (
            "termination_handover_checklist"
        )
