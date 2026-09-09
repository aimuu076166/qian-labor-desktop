"""Owned deletion uses real SQLite and synthetic private bytes only."""
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from test_company_workspaces import api
from test_historical_import import setup_history, post
from qian_labor.models.core import (
    AnalysisBatch, CompanyAnalysisBinding, DeletionTombstone, EmployeeRecord, UploadedFile,
)


@pytest.fixture
def materials(api):
    client, db, aid, current_id, files, url = setup_history(api)
    copied = post(client, url, aid, files[:1]).json()["results"][0]["file_id"]
    _, _, foreign_id, _, foreign_files, _ = setup_history(api)
    storage = client.app.state.import_service.storage
    with db.session() as session:
        source_paths = [storage.root / session.get(UploadedFile, f.id).storage_key for f in files]
        protected_ids = [copied, *[f.id for f in foreign_files]]
        protected = {fid: (storage.root / session.get(UploadedFile, fid).storage_key,
                           session.get(UploadedFile, fid).sha256,
                           (storage.root / session.get(UploadedFile, fid).storage_key).read_bytes())
                     for fid in protected_ids}
        records = {r.id: r.version for r in session.scalars(select(EmployeeRecord))}
    original_bytes = {path: path.read_bytes() for path in source_paths}
    return client, db, aid, current_id, foreign_id, files, source_paths, protected, records, original_bytes


def assert_protected(materials, *, failure=True):
    client, db, aid, current_id, foreign_id, files, paths, protected, records, originals = materials
    for path, digest, original in protected.values():
        assert path.read_bytes() == original
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with db.session() as session:
        assert session.get(AnalysisBatch, current_id) is not None
        assert session.get(AnalysisBatch, foreign_id) is not None
        assert session.get(CompanyAnalysisBinding, current_id).role == "current"
        for fid in protected:
            assert session.get(UploadedFile, fid) is not None
        for rid, version in records.items():
            record = session.get(EmployeeRecord, rid)
            assert record is not None
            if failure:
                assert record.version == version
        if failure:
            assert session.get(AnalysisBatch, aid) is not None
            assert session.get(DeletionTombstone, aid) is None
            assert all(session.get(UploadedFile, f.id) is not None for f in files)


@pytest.mark.parametrize("damage", ["foreign", "arbitrary", "absolute", "traversal", "wrong_file_uuid",
                                    "extension", "bad_file_uuid"])
def test_invalid_owned_key_rejects_every_target_before_cleanup(materials, damage):
    client, db, aid, _, _, files, paths, protected, _, originals = materials
    arbitrary = client.app.state.storage_root / "unowned.csv"
    arbitrary.write_bytes(b"synthetic unowned bytes")
    with db.session() as session:
        item = session.get(UploadedFile, files[-1].id)
        if damage == "foreign":
            # Different persisted string, same foreign target after resolve(); keep
            # the real storage_key UNIQUE constraint rather than weakening SQLite.
            item.storage_key = session.get(UploadedFile, next(iter(protected))).storage_key.replace(
                "analyses/", "analyses/./", 1)
        elif damage == "arbitrary":
            item.storage_key = "unowned.csv"
        elif damage == "absolute":
            item.storage_key = str(arbitrary)
        elif damage == "traversal":
            item.storage_key = f"analyses/{aid}/../{aid}/{item.id}.csv"
        elif damage == "wrong_file_uuid":
            item.storage_key = f"analyses/{aid}/{uuid4()}.csv"
        elif damage == "extension":
            item.extension = ".exe"
        else:
            item.id = "not-a-file-uuid"
        session.commit()
    response = client.delete(f"/api/analyses/{aid}")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": {"code": "DESKTOP_DELETION_STORAGE_UNSAFE"}}
    assert str(arbitrary) not in response.text
    assert arbitrary.read_bytes() == b"synthetic unowned bytes"
    assert all(path.read_bytes() == content for path, content in originals.items())
    if damage == "bad_file_uuid":
        with db.session() as session:
            session.get(UploadedFile, "not-a-file-uuid").id = files[-1].id
            session.commit()
    assert_protected(materials)


@pytest.mark.parametrize("boundary", ["file", "analysis", "analyses", "root", "derived_directory", "derived_file"])
def test_symlink_boundary_is_rejected_without_following_or_partial_cleanup(materials, tmp_path, boundary):
    client, _, aid, _, _, _, paths, protected, _, originals = materials
    owned = paths[0].parent
    protected_path = next(iter(protected.values()))[0]
    moved = None
    if boundary == "file":
        target = paths[-1]
        target.unlink()
        target.symlink_to(protected_path)
    elif boundary in {"analysis", "analyses", "root"}:
        target = {"analysis": owned, "analyses": owned.parent, "root": owned.parent.parent}[boundary]
        moved = tmp_path / f"synthetic-moved-{boundary}"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
    else:
        target = owned / "derived" / "unsafe"
        target.parent.mkdir()
        target.symlink_to(protected_path.parent if boundary == "derived_directory" else protected_path,
                          target_is_directory=boundary == "derived_directory")
    response = client.delete(f"/api/analyses/{aid}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "DESKTOP_DELETION_STORAGE_UNSAFE"
    # Restore only synthetic links so the unchanged byte and row oracles remain direct.
    target.unlink()
    if moved is not None:
        moved.rename(target)
    elif boundary == "file":
        target.write_bytes(originals[target])
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert_protected(materials)


@pytest.mark.parametrize("kind", ["analysis", "derived_directory"])
def test_directory_replaced_after_open_is_detected_before_any_unlink(materials, tmp_path, monkeypatch, kind):
    client, _, aid, _, _, _, paths, protected, _, originals = materials
    target = paths[0].parent
    if kind == "derived_directory":
        target = target / "derived"
        target.mkdir()
        (target / "synthetic.txt").write_bytes(b"owned derived bytes")
    displaced = tmp_path / "synthetic-displaced"
    protected_dir = next(iter(protected.values()))[0].parent
    original_open, original_unlink = os.open, os.unlink
    replaced = False
    deletes = []
    directory_fds = []

    def replace_after_open(path, flags, *args, **kwargs):
        nonlocal replaced
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        if str(path) == target.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            target.rename(displaced)
            target.symlink_to(protected_dir, target_is_directory=True)
        return fd

    def count_unlink(path, *args, **kwargs):
        deletes.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_after_open)
    monkeypatch.setattr(os, "unlink", count_unlink)
    response = client.delete(f"/api/analyses/{aid}")
    assert replaced, "The owned directory must be pinned with a relative no-follow open."
    assert response.status_code == 409, response.text
    assert deletes == []
    for fd in directory_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    original_unlink(target)
    displaced.rename(target)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert_protected(materials)


@pytest.mark.parametrize("boundary", ["root", "analyses", "analysis"])
def test_directory_disappearing_between_stat_and_open_retains_retryable_state(
    materials, tmp_path, monkeypatch, boundary,
):
    client, db, aid, _, _, _, paths, _, _, originals = materials
    owned = paths[0].parent
    target = {"root": owned.parent.parent, "analyses": owned.parent, "analysis": owned}[boundary]
    displaced = tmp_path / "synthetic-stat-open-displaced"
    original_open, original_stat, original_unlink, original_rmdir = os.open, os.stat, os.unlink, os.rmdir
    observed_stat = False
    replaced = False
    destructive_calls = []
    directory_fds = []

    def observe_stat(path, *args, **kwargs):
        nonlocal observed_stat
        info = original_stat(path, *args, **kwargs)
        if str(path) == target.name and kwargs.get("dir_fd") is not None and kwargs.get("follow_symlinks") is False:
            observed_stat = True
        return info

    def move_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if str(path) == target.name and kwargs.get("dir_fd") is not None and not replaced:
            assert observed_stat
            replaced = True
            target.rename(displaced)
        # The real filesystem now raises ENOENT; no synthetic exception replaces it.
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        return fd

    def observe_unlink(path, *args, **kwargs):
        destructive_calls.append(("unlink", path))
        return original_unlink(path, *args, **kwargs)

    def observe_rmdir(path, *args, **kwargs):
        destructive_calls.append(("rmdir", path))
        return original_rmdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", observe_stat)
        patch.setattr(os, "open", move_before_open)
        patch.setattr(os, "unlink", observe_unlink)
        patch.setattr(os, "rmdir", observe_rmdir)
        try:
            response = client.delete(f"/api/analyses/{aid}")
            assert observed_stat and replaced
            assert response.status_code == 409, response.text
            assert response.json() == {"detail": {"code": "DESKTOP_DELETION_STORAGE_UNSAFE"}}
            assert destructive_calls == []
            for fd in directory_fds:
                with pytest.raises(OSError):
                    os.fstat(fd)
        finally:
            if replaced:
                displaced.rename(target)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert_protected(materials)
    with db.session() as session:
        assert session.get(AnalysisBatch, aid).status == "deleting"
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert_protected(materials, failure=False)


@pytest.mark.parametrize("boundary", ["root", "analyses", "analysis"])
def test_directory_absent_before_initial_stat_preserves_missing_copy_idempotence(api, boundary):
    client, db = api
    aid = client.post("/api/analyses", json={"name": "合成已缺失副本"}).json()["id"]
    storage_root = client.app.state.storage_root
    if boundary != "root":
        storage_root.mkdir(exist_ok=True)
    if boundary == "analysis":
        (storage_root / "analyses").mkdir(exist_ok=True)
    missing = {"root": storage_root, "analyses": storage_root / "analyses",
               "analysis": storage_root / "analyses" / aid}[boundary]
    # LocalStorage constructs an empty root; remove only that known-empty synthetic
    # directory to model an initial missing root, never a populated tree.
    if boundary == "root":
        missing.rmdir()
    assert not missing.exists()
    first = client.delete(f"/api/analyses/{aid}")
    second = client.delete(f"/api/analyses/{aid}")
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "deleted"
    with db.session() as session:
        assert session.get(AnalysisBatch, aid) is None
        assert session.get(DeletionTombstone, aid) is not None


def test_final_unlink_uses_pinned_parent_after_analysis_path_swap(materials, tmp_path, monkeypatch):
    client, _, aid, _, _, _, paths, protected, _, _ = materials
    owned = paths[0].parent
    moved = tmp_path / "synthetic-pinned-owned"
    foreign_dir = next(iter(protected.values()))[0].parent
    original_unlink = os.unlink
    replaced = False

    def replace_at_unlink(path, *args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            assert kwargs.get("dir_fd") is not None
            owned.rename(moved)
            owned.symlink_to(foreign_dir, target_is_directory=True)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", replace_at_unlink)
    response = client.delete(f"/api/analyses/{aid}")
    assert replaced
    assert response.status_code == 409, response.text
    original_unlink(owned)
    moved.rename(owned)
    assert_protected(materials)
    # Owned partial cleanup remains explicitly retryable, with no successful tombstone.
    monkeypatch.setattr(os, "unlink", original_unlink)
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert_protected(materials, failure=False)


def test_pending_recovery_stops_delete_before_private_cleanup(materials):
    client, db, aid, _, _, _, _, _, _, originals = materials
    (db.path.parent / ".qian-migration-recovery").mkdir()
    response = client.delete(f"/api/analyses/{aid}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DESKTOP_DB_RECOVERY_REQUIRED"
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert_protected(materials)


def test_unsafe_tombstone_retry_keeps_foreign_material_and_existing_tombstone(materials):
    client, db, aid, _, _, _, paths, protected, _, _ = materials
    response = client.delete(f"/api/analyses/{aid}")
    assert response.status_code == 200
    with db.session() as session:
        deleted_at = session.get(DeletionTombstone, aid).deleted_at
    paths[0].parent.symlink_to(next(iter(protected.values()))[0].parent, target_is_directory=True)
    retry = client.delete(f"/api/analyses/{aid}")
    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"]["code"] == "DESKTOP_DELETION_STORAGE_UNSAFE"
    with db.session() as session:
        assert session.get(DeletionTombstone, aid).deleted_at == deleted_at
    assert_protected(materials, failure=False)


def test_final_file_symlink_replacement_never_unlinks_target(materials, monkeypatch):
    client, _, aid, _, _, _, paths, protected, _, _ = materials
    protected_path = next(iter(protected.values()))[0]
    original_unlink = os.unlink
    replaced = False

    def replace_at_unlink(path, *args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            parent_fd = kwargs.get("dir_fd")
            assert parent_fd is not None
            original_unlink(path, dir_fd=parent_fd)
            os.symlink(protected_path, path, dir_fd=parent_fd)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", replace_at_unlink)
    response = client.delete(f"/api/analyses/{aid}")
    assert replaced
    assert response.status_code == 200, response.text
    assert not paths[0].parent.exists()
    assert_protected(materials, failure=False)


def test_owned_hardlink_missing_file_derivatives_and_tombstone_retry(materials, tmp_path):
    client, db, aid, _, _, _, paths, _, _, originals = materials
    outside = tmp_path / "synthetic-original-hardlink.csv"
    os.link(paths[0], outside)
    paths[1].unlink()
    derived = paths[0].parent / "rendered" / "nested"
    derived.mkdir(parents=True)
    (derived / "page.txt").write_bytes(b"synthetic rendered private bytes")
    first = client.delete(f"/api/analyses/{aid}")
    second = client.delete(f"/api/analyses/{aid}")
    assert first.status_code == second.status_code == 200
    # SQLite returns the existing UTC timestamp without its timezone suffix on retry.
    assert datetime.fromisoformat(first.json()["deleted_at"]).replace(tzinfo=timezone.utc) == (
        datetime.fromisoformat(second.json()["deleted_at"]).replace(tzinfo=timezone.utc))
    assert outside.read_bytes() == originals[paths[0]]
    assert not paths[0].parent.exists()
    with db.session() as session:
        assert session.get(AnalysisBatch, aid) is None
        assert session.get(DeletionTombstone, aid) is not None
    assert_protected(materials, failure=False)
