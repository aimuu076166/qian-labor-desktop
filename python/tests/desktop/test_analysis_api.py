from pathlib import Path

from fastapi.testclient import TestClient

from qian_labor.desktop.app import create_desktop_app

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


def test_analysis_endpoints_remain_launch_token_protected(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)
    with TestClient(app) as client:
        assert client.post("/api/analyses", json={"name": "x", "company_display_name": "y"}).status_code == 401
