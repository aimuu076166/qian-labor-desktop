from pathlib import Path

from fastapi.testclient import TestClient

from qian_labor.desktop.app import create_desktop_app

TOKEN = "synthetic-workspace-api-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


def test_workspace_lists_and_reopens_batches_with_material_status(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    source = tmp_path / "synthetic.csv"
    source.write_text("工号,姓名\nSYN-001,完全虚构员工\n", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get("/api/analyses").status_code == 401
        ids = []
        for name in ("第一次体检", "第二次体检"):
            response = client.post("/api/analyses", headers=HEADERS,
                                   json={"name": name, "company_display_name": "虚构企业"})
            ids.append(response.json()["id"])
        imported = client.post(f"/api/analyses/{ids[0]}/import-paths", headers=HEADERS,
                               json={"paths": [str(source)]})
        assert imported.status_code == 200
        history = client.get("/api/analyses", headers=HEADERS)
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == ids[::-1]
        workspace = client.get(f"/api/analyses/{ids[0]}/workspace", headers=HEADERS)
        assert workspace.status_code == 200
        assert workspace.json()["analysis"]["name"] == "第一次体检"
        assert workspace.json()["files"][0]["filename"] == "synthetic.csv"
        assert workspace.json()["files"][0]["status"] == "uploaded"
        assert "storage_key" not in workspace.text
        assert str(tmp_path) not in workspace.text
        assert client.get("/api/analyses/nonexistent/workspace", headers=HEADERS).status_code == 404


def test_workspace_history_pagination_and_deleted_batch_exclusion(tmp_path: Path) -> None:
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        ids = [client.post("/api/analyses", headers=HEADERS, json={
            "name": f"虚构体检{index}", "company_display_name": "虚构企业",
        }).json()["id"] for index in range(3)]
        page = client.get("/api/analyses?page=2&page_size=2", headers=HEADERS)
        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert [item["id"] for item in page.json()["items"]] == ids[:1]
        assert client.delete(f"/api/analyses/{ids[0]}", headers=HEADERS).status_code == 200
        assert client.get("/api/analyses", headers=HEADERS).json()["total"] == 2
        assert client.get("/api/analyses?page=0", headers=HEADERS).status_code == 422
