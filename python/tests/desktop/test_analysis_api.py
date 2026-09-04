from pathlib import Path

from fastapi.testclient import TestClient

from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import AnalysisBatch, UploadedFile

TOKEN = "desktop-analysis-api-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


def test_sidecar_exposes_local_analysis_import_and_processing_endpoints(tmp_path: Path) -> None:
    source = tmp_path / "selected.csv"
    source.write_text("员工编号,姓名\nF-101,虚构员工甲\n", encoding="utf-8")
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        created = client.post(
            "/api/analyses",
            headers=HEADERS,
            json={"name": "桌面体检", "company_display_name": "虚构企业"},
        )
        assert created.status_code == 201
        analysis_id = created.json()["id"]

        imported = client.post(
            f"/api/analyses/{analysis_id}/import-paths",
            headers=HEADERS,
            json={"paths": [str(source)]},
        )
        assert imported.status_code == 200
        assert [item["original_filename"] for item in imported.json()["files"]] == [
            "selected.csv"
        ]
        assert source.read_text(encoding="utf-8") == "员工编号,姓名\nF-101,虚构员工甲\n"

        submitted = client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS)
        assert submitted.status_code == 202
        assert submitted.json()["queue_mode"] == "desktop"

        status = client.get(f"/api/analyses/{analysis_id}/processing", headers=HEADERS)
        assert status.status_code == 200
        payload = status.json()
        assert payload["analysis_id"] == analysis_id
        assert payload["status"] in {
            "uploading",
            "queued",
            "parsing",
            "extracting",
            "evaluating",
            "matching_review",
            "completed",
            "partial",
            "failed",
        }
        assert len(payload["files"]) == 1


def test_analysis_is_committed_as_queued_before_the_worker_can_start(tmp_path: Path) -> None:
    source = tmp_path / "selected.csv"
    source.write_text("员工编号,姓名\nF-102,虚构员工乙\n", encoding="utf-8")
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    observed_statuses: list[str] = []

    with TestClient(app) as client:
        analysis_id = client.post(
            "/api/analyses",
            headers=HEADERS,
            json={"name": "队列提交顺序", "company_display_name": "虚构企业"},
        ).json()["id"]
        imported = client.post(
            f"/api/analyses/{analysis_id}/import-paths",
            headers=HEADERS,
            json={"paths": [str(source)]},
        )
        assert imported.status_code == 200

        def observe_submit(submitted_id: str) -> dict[str, object]:
            with app.state.database.session() as session:
                analysis = session.get(AnalysisBatch, submitted_id)
                assert analysis is not None
                observed_statuses.append(analysis.status)
            return {
                "analysis_id": submitted_id,
                "status": "queued",
                "queue_mode": "desktop",
            }

        app.state.processing_queue.submit = observe_submit
        submitted = client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS)

        assert submitted.status_code == 202
        assert observed_statuses == ["queued"]


def test_busy_queue_restores_the_analysis_state(tmp_path: Path) -> None:
    source = tmp_path / "selected.csv"
    source.write_text("员工编号,姓名\nF-103,虚构员工丙\n", encoding="utf-8")
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        analysis_id = client.post(
            "/api/analyses",
            headers=HEADERS,
            json={"name": "忙队列回滚", "company_display_name": "虚构企业"},
        ).json()["id"]
        imported = client.post(
            f"/api/analyses/{analysis_id}/import-paths",
            headers=HEADERS,
            json={"paths": [str(source)]},
        )
        assert imported.status_code == 200

        def reject_submit(_submitted_id: str) -> dict[str, object]:
            raise RuntimeError("DESKTOP_ANALYSIS_BUSY")

        app.state.processing_queue.submit = reject_submit
        submitted = client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS)

        assert submitted.status_code == 409
        with app.state.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            assert analysis is not None
            assert analysis.status == "uploading"


def test_analysis_endpoints_remain_launch_token_protected(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    with TestClient(app) as client:
        assert client.post("/api/analyses", json={"name": "x", "company_display_name": "y"}).status_code == 401


def test_latest_analysis_returns_empty_home_and_normalizes_legacy_all_file_failure(
    tmp_path: Path,
) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        empty = client.get("/api/analyses/latest", headers=HEADERS)
        assert empty.status_code == 200
        assert empty.json() == {"analysis": None}

        with app.state.database.session() as session:
            analysis = AnalysisBatch(
                name="历史失败分析",
                company_display_name="虚构企业",
                status="partial",
                current_stage="partial",
                progress=100,
                file_count=1,
            )
            session.add(analysis)
            session.flush()
            session.add(
                UploadedFile(
                    analysis=analysis,
                    original_filename="虚构材料.docx",
                    storage_key=f"analyses/{analysis.id}/failed.docx",
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    extension=".docx",
                    size_bytes=10,
                    sha256="a" * 64,
                    status="failed",
                    progress=55,
                    error_code="AI_TIMEOUT",
                )
            )
            session.commit()
            analysis_id = analysis.id

        latest = client.get("/api/analyses/latest", headers=HEADERS)
        assert latest.status_code == 200
        assert latest.json()["analysis"] == {
            "id": analysis_id,
            "status": "failed",
            "progress": 100,
            "current_stage": "failed",
            "error_code": "AI_TIMEOUT",
        }
