from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from qian_labor.desktop.app import create_desktop_app
from qian_labor.desktop.queue import DesktopProcessingQueue
from qian_labor.models.core import AnalysisBatch, UploadedFile
from qian_labor.services.deletion import DeletionService

HEADERS = {"X-Qian-Desktop-Token": "synthetic-queue-token"}


@pytest.fixture
def imported_case(tmp_path):
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=HEADERS["X-Qian-Desktop-Token"])
    source = tmp_path / "synthetic.csv"
    source.write_text("员工编号,姓名\nSYN-001,虚构员工\n")
    extra = tmp_path / "supplement.csv"
    extra.write_text("员工编号,工资\nSYN-001,5000\n")
    with TestClient(app) as client:
        analysis_id = client.post("/api/analyses", headers=HEADERS,
                                  json={"name": "虚构并发体检"}).json()["id"]
        assert client.post(f"/api/analyses/{analysis_id}/import-paths", headers=HEADERS,
                           json={"paths": [str(source)]}).status_code == 200
        yield app, client, analysis_id, extra


def assert_busy(response):
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_ANALYSIS_BUSY"


def test_running_analysis_rejects_import_delete_and_duplicate_process_without_mutation(imported_case):
    app, client, analysis_id, extra = imported_case
    started, release = Event(), Event()
    class Pipeline:
        def process(self, target):
            with app.state.database.session() as session:
                analysis = session.get(AnalysisBatch, target)
                analysis.status, analysis.current_stage, analysis.progress = "extracting", "extracting", 50
                session.commit()
            started.set()
            assert release.wait(5)
            return {"status": "completed"}
    app.state.processing_queue.pipeline_factory = Pipeline
    original_files = {str(path): path.read_bytes() for path in app.state.storage_root.rglob("*") if path.is_file()}
    try:
        assert client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS).status_code == 202
        assert started.wait(2)
        assert_busy(client.post(f"/api/analyses/{analysis_id}/import-paths", headers=HEADERS,
                                json={"paths": [str(extra)]}))
        assert_busy(client.delete(f"/api/analyses/{analysis_id}", headers=HEADERS))
        assert_busy(client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS))
        with app.state.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            assert (analysis.status, analysis.current_stage, analysis.progress) == ("extracting", "extracting", 50)
            assert session.scalar(select(func.count()).select_from(UploadedFile)) == 1
        assert {str(path): path.read_bytes() for path in app.state.storage_root.rglob("*") if path.is_file()} == original_files
    finally:
        release.set()


@pytest.mark.parametrize("operation", ["import", "delete"])
def test_mutation_reservation_rejects_racing_process_and_mutation_but_allows_reads(imported_case, monkeypatch, operation):
    app, client, analysis_id, extra = imported_case
    entered, release = Event(), Event()
    if operation == "import":
        original = app.state.import_service.import_paths
        def slow_import(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(app.state.import_service, "import_paths", slow_import)
        def mutate():
            return client.post(f"/api/analyses/{analysis_id}/import-paths", headers=HEADERS,
                               json={"paths": [str(extra)]})
    else:
        original = DeletionService.delete
        def slow_delete(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(DeletionService, "delete", slow_delete)
        def mutate():
            return client.delete(f"/api/analyses/{analysis_id}", headers=HEADERS)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate)
        try:
            assert entered.wait(2)
            assert client.get(f"/api/analyses/{analysis_id}/processing", headers=HEADERS).status_code == 200
            assert_busy(client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS))
            assert_busy(client.post(f"/api/analyses/{analysis_id}/import-paths", headers=HEADERS,
                                    json={"paths": [str(extra)]}))
            assert_busy(client.delete(f"/api/analyses/{analysis_id}", headers=HEADERS))
        finally:
            release.set()
        assert future.result(2).status_code == 200


def test_submission_reservation_covers_prepare_and_failed_submit_rollback(monkeypatch):
    preparing, prepared, rolling_back, release_rollback = Event(), Event(), Event(), Event()
    class RejectingExecutor:
        def submit(self, *_args):
            raise RuntimeError("EXECUTOR_SUBMIT_FAILED")
        def shutdown(self, **_kwargs):
            pass
    queue = DesktopProcessingQueue(lambda: None)
    queue._executor.shutdown()
    queue._executor = RejectingExecutor()
    def prepare():
        preparing.set()
        assert prepared.wait(5)
        def rollback():
            rolling_back.set()
            assert release_rollback.wait(5)
        return rollback
    def submit():
        with pytest.raises(RuntimeError, match="EXECUTOR_SUBMIT_FAILED"):
            queue.submit("synthetic", prepare=prepare)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(submit)
        try:
            assert preparing.wait(2)
            assert queue.is_busy
            with pytest.raises(RuntimeError, match="DESKTOP_ANALYSIS_BUSY"):
                with queue.mutation("synthetic"):
                    pytest.fail("mutation entered during submit preparation")
            with pytest.raises(RuntimeError, match="DESKTOP_ANALYSIS_BUSY"):
                queue.submit("another")
            prepared.set()
            assert rolling_back.wait(2)
            with pytest.raises(RuntimeError, match="DESKTOP_ANALYSIS_BUSY"):
                with queue.mutation("synthetic"):
                    pytest.fail("mutation entered before failed submission rolled back")
        finally:
            prepared.set()
            release_rollback.set()
        future.result(2)
    assert not queue.is_busy
    with queue.mutation("synthetic"):
        pass
    queue.shutdown()


def test_executor_rejection_restores_only_its_reserved_analysis_and_releases_reservation(imported_case, monkeypatch):
    app, client, analysis_id, extra = imported_case
    def reject(*_args, **_kwargs):
        raise RuntimeError("EXECUTOR_SUBMIT_FAILED")
    monkeypatch.setattr(app.state.processing_queue._executor, "submit", reject)
    with pytest.raises(RuntimeError, match="EXECUTOR_SUBMIT_FAILED"):
        client.post(f"/api/analyses/{analysis_id}/process", headers=HEADERS)
    with app.state.database.session() as session:
        assert session.get(AnalysisBatch, analysis_id).status == "uploading"
    assert not app.state.processing_queue.is_busy
    assert client.post(f"/api/analyses/{analysis_id}/import-paths", headers=HEADERS,
                       json={"paths": [str(extra)]}).status_code == 200
