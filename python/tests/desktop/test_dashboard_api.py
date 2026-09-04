from pathlib import Path

from fastapi.testclient import TestClient

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import (
    AnalysisBatch,
    Employee,
    EmploymentFact,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)

TOKEN = "desktop-dashboard-api-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


def _seed_completed_analysis(app) -> dict[str, str]:
    with app.state.database.session() as session:
        analysis = AnalysisBatch(
            name="虚构企业体检",
            company_display_name="完全虚构企业",
            status="completed",
            current_stage="completed",
            progress=100,
            employee_count=1,
            high_count=1,
        )
        uploaded = UploadedFile(
            analysis=analysis,
            original_filename="虚构合同.docx",
            storage_key="storage/dashboard-contract.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            extension=".docx",
            size_bytes=32,
            sha256="d" * 64,
            status="processed",
            detected_kind="docx",
            classified_kind="contract",
        )
        employee = Employee(
            analysis=analysis,
            masked_name="虚构员**",
            normalized_name="虚构员**",
            employee_number="F-100",
            department="虚构制造部",
            job_title="虚构操作员",
            employment_status="active",
            match_status="confirmed",
        )
        session.add_all([analysis, uploaded, employee])
        session.flush()
        fact = EmploymentFact(
            analysis=analysis,
            employee=employee,
            file=uploaded,
            fact_type="employment.status",
            value_json="active",
            normalized_value_json="active",
            extraction_method="ai",
            confidence=0.93,
            verification_status="verified",
            dedupe_key="e" * 64,
        )
        session.add(fact)
        session.flush()
        source = SourceLocator(
            analysis=analysis,
            file=uploaded,
            fact=fact,
            locator_type="paragraph",
            location={"paragraph": 2},
            excerpt="完全虚构且已脱敏的材料摘录",
            content_hash="f" * 64,
        )
        session.add(source)
        session.flush()
        finding = RiskFinding(
            analysis=analysis,
            employee=employee,
            rule_id="R01",
            rule_version="1.0.0",
            category="contract",
            severity="high",
            assessment_status="suspected_risk",
            title="劳动合同签订事项待核查",
            summary="完全虚构的风险摘要",
            trigger_fact_ids=[fact.id],
            source_locator_ids=[source.id],
            legal_basis={"basis_type": "legal"},
            recommended_actions=["人工核查虚构材料"],
            requires_human_review=True,
        )
        session.add(finding)
        session.commit()
        return {
            "analysis_id": analysis.id,
            "employee_id": employee.id,
            "finding_id": finding.id,
        }


def test_full_dashboard_contract_exposes_coverage_and_priority_findings(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_completed_analysis(app)

    with TestClient(app) as client:
        response = client.get(
            f"/api/analyses/{seeded['analysis_id']}/dashboard", headers=HEADERS
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["company_name"] == "完全虚构企业"
    assert payload["overview"]["summary"]["affected_employee_count"] == 1
    assert payload["overview"]["summary"]["requires_human_review_count"] == 1
    assert payload["overview"]["material_coverage"]["overall"] >= 0
    assert payload["overview"]["priority_findings"][0]["id"] == seeded["finding_id"]


def test_employee_ledger_and_detail_are_available_only_within_the_analysis(
    tmp_path: Path,
) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_completed_analysis(app)

    with TestClient(app) as client:
        ledger = client.get(
            f"/api/analyses/{seeded['analysis_id']}/employees", headers=HEADERS
        )
        detail = client.get(
            f"/api/analyses/{seeded['analysis_id']}/employees/{seeded['employee_id']}",
            headers=HEADERS,
        )
        missing = client.get(
            f"/api/analyses/not-the-analysis/employees/{seeded['employee_id']}",
            headers=HEADERS,
        )

    assert ledger.status_code == 200
    assert ledger.json()["total"] == 1
    assert ledger.json()["items"][0]["masked_name"] == "虚构员**"
    assert ledger.json()["items"][0]["risk_counts"] == {"high": 1, "medium": 0}
    assert detail.status_code == 200
    assert detail.json()["employee"]["id"] == seeded["employee_id"]
    assert detail.json()["findings"][0]["id"] == seeded["finding_id"]
    assert missing.status_code == 404


def test_report_reuses_dashboard_ledger_and_traceable_sources(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_completed_analysis(app)

    with TestClient(app) as client:
        response = client.get(
            f"/api/analyses/{seeded['analysis_id']}/report", headers=HEADERS
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis_id"] == seeded["analysis_id"]
    assert payload["summary"]["employee_count"] == 1
    assert payload["employees"][0]["id"] == seeded["employee_id"]
    assert payload["findings"][0]["id"] == seeded["finding_id"]
    assert payload["findings"][0]["sources"][0] == {
        "file_name": "虚构合同.docx",
        "locator_type": "paragraph",
        "location": {"paragraph": 2},
    }
    assert payload["is_demo"] is False
