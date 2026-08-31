from pathlib import Path

from fastapi.testclient import TestClient

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import (
    AnalysisBatch,
    Employee,
    EmployeeMatchCandidate,
    EmploymentFact,
    SourceLocator,
    UploadedFile,
)

TOKEN = "desktop-matching-api-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


def _seed_review(app, *, suffix: str = "one") -> dict[str, str]:
    with app.state.database.session() as session:
        analysis = AnalysisBatch(
            name=f"匹配复核-{suffix}",
            company_display_name="完全虚构企业",
            status="matching_review",
            current_stage="matching_review",
            progress=90,
        )
        uploaded = UploadedFile(
            analysis=analysis,
            original_filename=f"虚构材料-{suffix}.docx",
            storage_key=f"storage/{suffix}.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            extension=".docx",
            size_bytes=16,
            sha256=suffix.rjust(64, "0"),
            status="processed",
            detected_kind="docx",
            classified_kind="contract",
        )
        employee = Employee(
            analysis=analysis,
            masked_name="虚构员**",
            normalized_name="虚构员**",
            employee_number=f"F-{suffix.upper()}",
            department="虚构部门",
            match_status="ambiguous",
        )
        session.add_all([analysis, uploaded, employee])
        session.flush()
        fact = EmploymentFact(
            analysis=analysis,
            file=uploaded,
            employee_id=None,
            fact_type="employment.status",
            value_json="active",
            normalized_value_json="active",
            extraction_method="ai",
            confidence=0.9,
            verification_status="unverified",
            dedupe_key=f"fact-{suffix}".ljust(64, "0"),
        )
        session.add(fact)
        session.flush()
        source = SourceLocator(
            analysis=analysis,
            file=uploaded,
            fact=fact,
            locator_type="paragraph",
            location={"paragraph": 1},
            excerpt="完全虚构且已脱敏的材料摘录",
            content_hash=f"source-{suffix}".ljust(64, "0"),
        )
        candidate = EmployeeMatchCandidate(
            analysis=analysis,
            file_id=uploaded.id,
            candidate_employee_id=employee.id,
            extracted_fields={"fact_ids": [fact.id], "employee_ids": [employee.employee_number]},
            score=0.72,
            reason="multiple_identifier_values",
            status="pending",
        )
        session.add_all([source, candidate])
        session.commit()
        return {
            "analysis_id": analysis.id,
            "candidate_id": candidate.id,
            "employee_id": employee.id,
            "fact_id": fact.id,
        }


def test_matching_candidates_are_exposed_as_masked_review_data(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_review(app)

    with TestClient(app) as client:
        unauthenticated = client.get(
            f"/api/analyses/{seeded['analysis_id']}/matching-candidates"
        )
        response = client.get(
            f"/api/analyses/{seeded['analysis_id']}/matching-candidates",
            headers=HEADERS,
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "analysis_id": seeded["analysis_id"],
        "candidates": [
            {
                "id": seeded["candidate_id"],
                "file_id": response.json()["candidates"][0]["file_id"],
                "material_name": "虚构材料-one.docx",
                "employee_id": seeded["employee_id"],
                "employee_name": "虚构员**",
                "employee_number": "F-ONE",
                "extracted_fields": {
                    "fact_ids": [seeded["fact_id"]],
                    "employee_ids": ["F-ONE"],
                },
                "fact_ids": [seeded["fact_id"]],
                "score": 0.72,
                "reasons": ["multiple_identifier_values"],
                "status": "pending",
                "employee_options": [
                    {
                        "employee_id": seeded["employee_id"],
                        "employee_name": "虚构员**",
                        "employee_number": "F-ONE",
                        "department": "虚构部门",
                    }
                ],
            }
        ],
    }


def test_last_match_assignment_resumes_rules_and_completes_analysis(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_review(app)

    with TestClient(app) as client:
        response = client.post(
            f"/api/analyses/{seeded['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json={
                "candidate_id": seeded["candidate_id"],
                "decision": "assign",
                "employee_id": seeded["employee_id"],
                "fact_ids": [seeded["fact_id"]],
            },
        )
        processing = client.get(
            f"/api/analyses/{seeded['analysis_id']}/processing", headers=HEADERS
        )

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "completed"
    assert processing.json()["status"] == "completed"
    with app.state.database.session() as session:
        fact = session.get(EmploymentFact, seeded["fact_id"])
        assert fact is not None
        assert fact.employee_id == seeded["employee_id"]


def test_stale_match_decision_is_rejected_without_reapplying_facts(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    seeded = _seed_review(app)
    payload = {
        "candidate_id": seeded["candidate_id"],
        "decision": "assign",
        "employee_id": seeded["employee_id"],
        "fact_ids": [seeded["fact_id"]],
    }

    with TestClient(app) as client:
        first = client.post(
            f"/api/analyses/{seeded['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json=payload,
        )
        second = client.post(
            f"/api/analyses/{seeded['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "MATCH_DECISION_STALE"


def test_match_assignment_cannot_target_an_employee_from_another_analysis(
    tmp_path: Path,
) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    source = _seed_review(app, suffix="source")
    other = _seed_review(app, suffix="other")

    with TestClient(app) as client:
        response = client.post(
            f"/api/analyses/{source['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json={
                "candidate_id": source["candidate_id"],
                "decision": "assign",
                "employee_id": other["employee_id"],
                "fact_ids": [source["fact_id"]],
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MATCH_CROSS_ANALYSIS_FORBIDDEN"
    with app.state.database.session() as session:
        candidate = session.get(EmployeeMatchCandidate, source["candidate_id"])
        assert candidate is not None
        assert candidate.status == "pending"
