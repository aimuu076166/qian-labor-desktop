"""Explicit private-copy reuse, using synthetic local files only."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from threading import Event

import pytest
from sqlalchemy import select

from test_company_workspaces import api, company, snapshot
from test_current_company import current
from qian_labor.models.core import AnalysisBatch, UploadedFile, ProcessingJob
from qian_labor.security.uploads import UploadPolicy


def setup_history(api):
    client, db = api
    c = company(client)
    aid, sid = snapshot(db)
    imports = client.app.state.import_service
    files = [imports.uploads.add(aid, f"合成材料-{i}.csv", "text/csv", f"material\nsynthetic-{i}\n".encode())
             for i in range(3)]
    with db.session() as s:
        s.get(AnalysisBatch, aid).status = "completed"
        s.commit()
    response = client.put(f'/api/company-workspaces/{c["id"]}/analyses/{aid}/binding', json={
        "expected_company_version": 0, "expected_analysis_version": 0, "decisions": [
            {"action": "create", "snapshot_id": sid, "record": {"id": str(uuid4()), "display_name": "合成员工"}}]})
    assert response.status_code == 200, response.text
    cur = current(client, c["id"], 1)
    url = f'/api/company-workspaces/{c["id"]}/current-analysis/import-historical'
    return client, db, aid, cur["analysis_id"], files, url


def post(client, url, aid, files):
    return client.post(url, json={"source_analysis_id": aid, "file_ids": [f.id for f in files]})


def test_explicit_copy_labels_retry_source_unchanged_and_deletion_survival(api):
    client, db, aid, target, files, url = setup_history(api)
    with db.session() as s:
        before = {col.name: getattr(s.get(AnalysisBatch, aid), col.name) for col in AnalysisBatch.__table__.columns}
    result = post(client, url, aid, files[:1])
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["analysis_id"] == target
    assert body["requires_explicit_start"] is True
    assert "额度" in body["provider_quota_notice"]
    assert [r["status"] for r in body["results"]] == ["imported"]
    retry = post(client, url, aid, files[:1]).json()
    assert retry["results"][0]["status"] == "duplicate"
    assert retry["results"][0]["file_id"] == body["results"][0]["file_id"]
    with db.session() as s:
        originals = list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == aid)))
        copies = list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == target)))
        assert len(originals) == 3 and len(copies) == 1
        assert copies[0].original_filename == files[0].original_filename
        assert copies[0].mime_type == "text/csv" and copies[0].sha256 == files[0].sha256
        assert copies[0].status == "uploaded"
        assert before == {col.name: getattr(s.get(AnalysisBatch, aid), col.name) for col in AnalysisBatch.__table__.columns}
        assert list(s.scalars(select(ProcessingJob))) == []
        key = copies[0].storage_key
    assert client.get(url.removesuffix('/import-historical')).json()["stale"] is True
    assert client.delete(f'/api/analyses/{aid}').status_code == 200
    assert client.app.state.import_service.storage.read_bytes(key) == b"material\nsynthetic-0\n"


@pytest.mark.parametrize("selection", [[], [str(uuid4())] * 2, [str(uuid4()) for _ in range(101)]])
def test_selection_required_distinct_bounded(api, selection):
    client, db, aid, target, files, url = setup_history(api)
    response = client.post(url, json={"source_analysis_id": aid, "file_ids": selection})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "WORKSPACE_REQUEST_INVALID"}}


def test_ownership_rejects_entire_request_before_copy(api):
    client, db, aid, target, files, url = setup_history(api)
    other, _ = snapshot(db)
    alien = client.app.state.import_service.uploads.add(other, "other.csv", "text/csv", b"synthetic,other\n")
    for source, selected in [(aid, [files[0], alien]), (other, [alien]), (target, files[:1])]:
        response = post(client, url, source, selected)
        assert response.status_code == 409, response.text
    second = company(client)
    response = post(client, url.replace(url.split('/')[3], second["id"]), aid, files[:1])
    assert response.status_code == 409, response.text
    with db.session() as s:
        assert list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == target))) == []


@pytest.mark.parametrize("damage", ["missing", "checksum", "size", "key", "outside", "symlink", "parent_symlink",
                                      "root_symlink", "hardlink", "directory", "oversize", "invalid_content"])
def test_unsafe_missing_corrupt_private_source_partial_and_retry(api, tmp_path, damage):
    client, db, aid, target, files, url = setup_history(api)
    storage = client.app.state.import_service.storage
    with db.session() as s:
        item = s.get(UploadedFile, files[1].id)
        key = item.storage_key
        path = storage.root / key
        original = path.read_bytes()
        if damage == "missing":
            path.unlink()
        elif damage == "checksum":
            path.write_bytes(b"x" * len(original))
        elif damage == "size":
            item.size_bytes += 1
        elif damage == "key":
            item.storage_key = key.replace(item.id, str(uuid4()))
        elif damage == "outside":
            item.storage_key = str(tmp_path / "unselected.csv")
        elif damage == "symlink":
            other = tmp_path / "unselected.csv"
            other.write_bytes(original)
            path.unlink()
            path.symlink_to(other)
        elif damage == "parent_symlink":
            moved = path.parent.with_name(aid + "-moved")
            path.parent.rename(moved)
            path.parent.symlink_to(moved, target_is_directory=True)
        elif damage == "root_symlink":
            moved = storage.root.with_name("moved-storage")
            storage.root.rename(moved)
            storage.root.symlink_to(moved, target_is_directory=True)
        elif damage == "hardlink":
            os.link(path, tmp_path / "linked.csv")
        elif damage == "directory":
            path.unlink()
            path.mkdir()
        elif damage == "oversize":
            item.size_bytes = 15_000_001
        elif damage == "invalid_content":
            content = b"#!/synthetic-executable\n"
            path.write_bytes(content)
            item.size_bytes = len(content)
            item.sha256 = hashlib.sha256(content).hexdigest()
        s.commit()
    result = post(client, url, aid, files)
    assert result.status_code == 200, result.text
    outcomes = result.json()["results"]
    expected = ["error"] * 3 if damage in {"parent_symlink", "root_symlink"} else ["imported", "error", "imported"]
    assert [r["status"] for r in outcomes] == expected
    assert outcomes[1]["file_id"] is None and outcomes[1]["error_code"].startswith("HISTORICAL_")
    assert str(tmp_path) not in result.text
    retry = post(client, url, aid, files).json()["results"]
    assert [r["status"] for r in retry] == ["duplicate" if v == "imported" else v for v in expected]
    with db.session() as s:
        assert s.get(UploadedFile, files[1].id) is not None
        assert len(list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == target)))) == expected.count("imported")


@pytest.mark.parametrize("reserved", ["source", "target"])
def test_both_mutation_reservations_exclude_import(api, reserved):
    client, db, aid, target, files, url = setup_history(api)
    queue = client.app.state.processing_queue
    with queue.mutation(aid if reserved == "source" else target):
        result = post(client, url, aid, files)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "DESKTOP_ANALYSIS_BUSY"
    assert post(client, url, aid, files).status_code == 200


def test_retry_duplicate_at_capacity_and_partial_limit(api):
    client, db, aid, target, files, url = setup_history(api)
    client.app.state.import_service.uploads.policy = UploadPolicy(max_files=1)
    result = post(client, url, aid, files).json()
    assert [r["status"] for r in result["results"]] == ["imported", "error", "error"]
    retry = post(client, url, aid, files[:1])
    assert retry.status_code == 200, retry.text
    assert retry.json()["results"][0]["status"] == "duplicate"


def test_reservations_hold_during_read_without_queue_mutex_and_release_in_order(api, monkeypatch):
    from qian_labor.desktop.historical_import import HistoricalImportService
    client, db, aid, target, files, url = setup_history(api)
    queue = client.app.state.processing_queue
    entered, release = Event(), Event()
    read = HistoricalImportService._read_private
    mutation = queue.mutation
    acquired = []

    @contextmanager
    def trace_mutation(analysis_id):
        with mutation(analysis_id):
            acquired.append(analysis_id)
            yield

    def waiting_read(self, item):
        entered.set()
        assert release.wait(5)
        return read(self, item)

    monkeypatch.setattr(queue, "mutation", trace_mutation)
    monkeypatch.setattr(HistoricalImportService, "_read_private", waiting_read)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(post, client, url, aid, files[:1])
        try:
            assert entered.wait(5)
            assert acquired == sorted((aid, target))
            # Real queue lock remains available during disk work.
            assert queue.is_busy is False
            assert client.delete(f"/api/analyses/{aid}").status_code == 409
            assert client.post(f"/api/analyses/{target}/process").status_code == 409
        finally:
            release.set()
        assert future.result().status_code == 200
    with mutation(aid), mutation(target):
        pass


def test_source_and_target_active_processing_exclude_copy(api):
    client, db, aid, target, files, url = setup_history(api)
    queue = client.app.state.processing_queue
    entered, release = Event(), Event()

    class WaitingPipeline:
        def process(self, analysis_id):
            entered.set()
            assert release.wait(5)
            return {"status": "completed"}

    queue.pipeline_factory = WaitingPipeline
    for active in (aid, target):
        entered.clear()
        release.clear()
        queue.submit(active)
        try:
            assert entered.wait(5)
            response = post(client, url, aid, files[:1])
            assert response.status_code == 409, response.text
        finally:
            release.set()
            # Wait for actual synthetic queue task completion before reserving again.
            queue._executor.submit(lambda: None).result(timeout=5)
    assert post(client, url, aid, files[:1]).status_code == 200


def test_copy_write_failure_reconciles_successes_and_retry(api, monkeypatch):
    client, db, aid, target, files, url = setup_history(api)
    storage = client.app.state.import_service.storage
    save = storage.save_bytes

    def fail_one(content, key):
        if b"synthetic-1" in content:
            raise OSError("synthetic private path must not escape response")
        return save(content, key)

    monkeypatch.setattr(storage, "save_bytes", fail_one)
    response = post(client, url, aid, files)
    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["results"]] == ["imported", "error", "imported"]
    assert response.json()["results"][1]["error_code"] == "HISTORICAL_COPY_FAILED"
    assert "private path" not in response.text
    with db.session() as s:
        assert s.get(AnalysisBatch, target).file_count == 2
    monkeypatch.setattr(storage, "save_bytes", save)
    assert [r["status"] for r in post(client, url, aid, files).json()["results"]] == ["duplicate", "imported", "duplicate"]


def test_auth_and_path_input_rejected_without_reading(api):
    client, db, aid, target, files, url = setup_history(api)
    body = {"source_analysis_id": aid, "file_ids": [files[0].id]}
    assert client.post(url, json=body, headers={"X-Qian-Desktop-Token": "wrong"}).status_code == 401
    response = client.post(url, json={**body, "paths": ["/synthetic/unselected.csv"]})
    assert response.status_code == 422
    assert "/synthetic" not in response.text
