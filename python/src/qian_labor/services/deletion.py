from contextlib import ExitStack
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from uuid import UUID

from sqlalchemy import select, text

from qian_labor.database import Database
from qian_labor.sqlite_migrations import assert_no_pending_recovery
from qian_labor.models.core import (
    AnalysisBatch,
    DeletionTombstone,
    ProcessingJob,
    UploadedFile,
    utcnow,
    CompanyWorkspace, CompanyAnalysisBinding, EmployeeRecord, EmployeeSnapshotBinding,
)
from qian_labor.services.company_workspaces import WorkspaceError
from qian_labor.security.uploads import ALLOWED


_FD_DELETION_SUPPORTED = (
    all(operation in os.supports_dir_fd for operation in (os.open, os.stat, os.unlink, os.rmdir))
    and os.stat in os.supports_follow_symlinks and os.listdir in os.supports_fd
    and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
)


def _unsafe_storage() -> WorkspaceError:
    return WorkspaceError("DESKTOP_DELETION_STORAGE_UNSAFE")


@dataclass
class _OwnedDirectory:
    parent_fd: int
    name: str
    fd: int
    info: os.stat_result
    files: list[tuple[str, os.stat_result]] = field(default_factory=list)
    children: list["_OwnedDirectory"] = field(default_factory=list)


def _check_entry(parent_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        raise _unsafe_storage() from None
    if not os.path.samestat(current, expected) or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected.st_mode):
        raise _unsafe_storage()


def _open_directory(parent_fd: int, name: str, handles: ExitStack) -> _OwnedDirectory:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe_storage()
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        # The initial no-follow stat found this directory. Disappearance now is
        # a replacement race, not an already-missing private copy on retry.
        raise _unsafe_storage() from None
    handles.callback(os.close, fd)
    if not os.path.samestat(info, os.fstat(fd)):
        raise _unsafe_storage()
    _check_entry(parent_fd, name, info)
    return _OwnedDirectory(parent_fd, name, fd, info)


def _scan_owned_tree(directory: _OwnedDirectory, handles: ExitStack) -> None:
    for name in sorted(os.listdir(directory.fd)):
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _open_directory(directory.fd, name, handles)
            if not os.path.samestat(info, child.info):
                raise _unsafe_storage()
            directory.children.append(child)
            _scan_owned_tree(child, handles)
        elif stat.S_ISREG(info.st_mode):
            # Unlink removes only this owned name; a hardlink elsewhere survives.
            directory.files.append((name, info))
        else:
            raise _unsafe_storage()


def _validate_owned_tree(directory: _OwnedDirectory) -> None:
    _check_entry(directory.parent_fd, directory.name, directory.info)
    expected_names = {name for name, _ in directory.files} | {child.name for child in directory.children}
    if set(os.listdir(directory.fd)) != expected_names:
        raise _unsafe_storage()
    for name, info in directory.files:
        _check_entry(directory.fd, name, info)
    for child in directory.children:
        _validate_owned_tree(child)


def _remove_owned_tree(directory: _OwnedDirectory, boundaries: list[_OwnedDirectory]) -> None:
    for name, info in directory.files:
        for boundary in boundaries:
            _check_entry(boundary.parent_fd, boundary.name, boundary.info)
        _check_entry(directory.parent_fd, directory.name, directory.info)
        _check_entry(directory.fd, name, info)
        # POSIX has no unlink-if-inode. A last-instruction replacement may remove
        # that name (including a symlink), but unlinkat never follows its target.
        os.unlink(name, dir_fd=directory.fd)
    for child in directory.children:
        _remove_owned_tree(child, boundaries)
    for boundary in boundaries:
        _check_entry(boundary.parent_fd, boundary.name, boundary.info)
    _check_entry(directory.parent_fd, directory.name, directory.info)
    # rmdir does not traverse a replacement directory or a symlink. New entries
    # cause ENOTEMPTY and leave the existing deleting state available for retry.
    os.rmdir(directory.name, dir_fd=directory.parent_fd)


class DeletionService:
    def __init__(self, database: Database, storage_root: str) -> None:
        self.database = database
        # Normalize lexical components, but do not resolve away a symlink before
        # the descriptor walk has a chance to reject it.
        self.storage_root = Path(os.path.abspath(storage_root))

    def delete(self, analysis_id: str, *, reason: str = "user_requested") -> dict[str, str]:
        if self.database.path is not None:
            assert_no_pending_recovery(self.database.path.parent)
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            owner = session.get(CompanyAnalysisBinding, analysis_id)
            if owner is not None and owner.role == "current":
                raise WorkspaceError("WORKSPACE_CURRENT_ANALYSIS_DELETE_FORBIDDEN")
            tombstone = session.get(DeletionTombstone, analysis_id)
            if tombstone:
                deleted_at = tombstone.deleted_at
                material_entries = []
            else:
                analysis = session.get(AnalysisBatch, analysis_id)
                if analysis is None:
                    return {"id": analysis_id, "status": "deleted"}
                material_entries = list(
                    session.execute(
                        select(UploadedFile.id, UploadedFile.extension, UploadedFile.original_filename,
                               UploadedFile.storage_key).where(
                            UploadedFile.analysis_id == analysis_id
                        )
                    )
                )
                analysis.status = "deleting"
                for job in session.scalars(
                    select(ProcessingJob).where(
                        ProcessingJob.analysis_id == analysis_id,
                        ProcessingJob.status.in_({"pending", "running"}),
                    )
                ):
                    job.status = "cancelled"
                    job.error_code = "ANALYSIS_DELETED"
                session.commit()

        self._remove_files(analysis_id, material_entries)

        if tombstone:
            return {
                "id": analysis_id,
                "status": "deleted",
                "deleted_at": deleted_at.isoformat(),
            }

        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            tombstone = session.get(DeletionTombstone, analysis_id)
            if tombstone:
                return {
                    "id": analysis_id,
                    "status": "deleted",
                    "deleted_at": tombstone.deleted_at.isoformat(),
                }
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                return {"id": analysis_id, "status": "deleted"}
            deleted_at = utcnow()
            owner = session.get(CompanyAnalysisBinding, analysis_id)
            if owner is not None:
                if owner.role == "current":
                    raise WorkspaceError("WORKSPACE_CURRENT_ANALYSIS_DELETE_FORBIDDEN")
                session.get(CompanyWorkspace, owner.company_id).version += 1
                record_ids = set(session.scalars(select(EmployeeSnapshotBinding.employee_record_id).where(
                    EmployeeSnapshotBinding.analysis_id == analysis_id)))
                for record_id in record_ids:
                    session.get(EmployeeRecord, record_id).version += 1
            session.add(
                DeletionTombstone(
                    analysis_id=analysis_id,
                    deleted_at=deleted_at,
                    deletion_reason=reason,
                )
            )
            session.delete(analysis)
            session.commit()
        return {"id": analysis_id, "status": "deleted", "deleted_at": deleted_at.isoformat()}

    def _remove_files(self, analysis_id: str, material_entries: list[tuple[str, str, str, str]]) -> None:
        """Prevalidate all metadata/tree entries, then remove only through pinned FDs.

        The queue's existing mutation reservation excludes app writers. FDs prevent
        a renamed parent from redirecting cleanup into a different analysis. Every
        handle closes on success or failure; no pathname-based recursive fallback.
        """
        try:
            if str(UUID(analysis_id)) != analysis_id:
                raise ValueError
            expected_files = set()
            for file_id, extension, original_filename, key in material_entries:
                if (str(UUID(file_id)) != file_id or extension not in ALLOWED
                        or Path(original_filename).suffix.lower() != extension
                        or key != f"analyses/{analysis_id}/{file_id}{extension}"):
                    raise ValueError
                expected_files.add(f"{file_id}{extension}")
        except (ValueError, TypeError, AttributeError):
            raise _unsafe_storage() from None
        if not _FD_DELETION_SUPPORTED:
            raise _unsafe_storage()
        with ExitStack() as handles:
            try:
                fd = os.open(self.storage_root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                handles.callback(os.close, fd)
                boundaries = []
                for name in (*self.storage_root.parts[1:], "analyses", analysis_id):
                    try:
                        directory = _open_directory(fd, name, handles)
                    except FileNotFoundError:
                        # Missing private copies are normal for an interrupted cleanup.
                        return
                    boundaries.append(directory)
                    fd = directory.fd
                owned = boundaries[-1]
                for name in expected_files:
                    try:
                        info = os.stat(name, dir_fd=owned.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise _unsafe_storage()
                _scan_owned_tree(owned, handles)
                for boundary in boundaries:
                    _check_entry(boundary.parent_fd, boundary.name, boundary.info)
                _validate_owned_tree(owned)
            except OSError:
                raise _unsafe_storage() from None
            # No filesystem mutation occurs until the complete plan is validated.
            # Keep I/O failures retryable; never write a tombstone after failed cleanup.
            _remove_owned_tree(owned, boundaries)
