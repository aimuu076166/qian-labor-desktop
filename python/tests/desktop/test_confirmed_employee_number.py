from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import Employee, EmployeeMatchCandidate
from test_matching_api import HEADERS, TOKEN, _seed_review


def test_manually_confirmed_employee_number_is_retained(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    seeded = _seed_review(app)
    with TestClient(app) as client:
        response = client.post(
            f"/api/analyses/{seeded['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json={"candidate_id": seeded["candidate_id"], "decision": "create_unknown",
                  "display_name": "完全虚构新员工", "employee_number": " SYN-010 ",
                  "fact_ids": [seeded["fact_id"]]},
        )
        assert response.status_code == 200
        with app.state.database.session() as session:
            employee = session.get(Employee, response.json()["target_employee_id"])
            assert employee.employee_number == "SYN-010"
            assert employee.match_status == "confirmed"


def test_duplicate_confirmed_number_does_not_create_or_merge_employee(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    seeded = _seed_review(app)
    with TestClient(app) as client:
        response = client.post(
            f"/api/analyses/{seeded['analysis_id']}/matching-decisions",
            headers=HEADERS,
            json={"candidate_id": seeded["candidate_id"], "decision": "create_unknown",
                  "display_name": "完全虚构新员工", "employee_number": "F-ONE",
                  "fact_ids": [seeded["fact_id"]]},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "MATCH_EMPLOYEE_NUMBER_EXISTS"
        with app.state.database.session() as session:
            assert session.scalar(select(func.count()).select_from(Employee)) == 1
            assert session.get(EmployeeMatchCandidate, seeded["candidate_id"]).status == "pending"
