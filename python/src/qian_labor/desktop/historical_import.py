"""Explicit reuse of owned historical private bytes; never runs the provider."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import stat
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from qian_labor.desktop.company_schemas import HistoricalImportOutcome, HistoricalImportView
from qian_labor.models.core import AnalysisBatch, CompanyAnalysisBinding, UploadedFile
from qian_labor.security.uploads import ALLOWED, UploadRejected
from qian_labor.services.company_workspaces import WorkspaceError
from qian_labor.sqlite_migrations import assert_no_pending_recovery


class HistoricalImportService:
    def __init__(self, database, uploads, queue):
        self.database, self.uploads, self.queue = database, uploads, queue

    def _target(self, company_id, source_id):
        with self.database.session() as session:
            source = session.get(CompanyAnalysisBinding, source_id)
            target = session.scalar(select(CompanyAnalysisBinding).where(
                CompanyAnalysisBinding.company_id == company_id,
                CompanyAnalysisBinding.role == "current"))
            if (source is None or source.company_id != company_id or source.role != "historical"
                    or target is None or target.analysis_id == source_id):
                raise WorkspaceError("WORKSPACE_HISTORICAL_SOURCE_INVALID")
            if any(session.get(AnalysisBatch, aid).status == "deleting"
                   for aid in (source_id, target.analysis_id)):
                raise WorkspaceError("DESKTOP_ANALYSIS_BUSY")
            return target.analysis_id

    def copy(self, company_id, source_id, file_ids):
        if not file_ids or len(file_ids) > 100 or len(set(file_ids)) != len(file_ids):
            raise WorkspaceError("WORKSPACE_REQUEST_INVALID", 422)
        if self.database.path is not None:
            assert_no_pending_recovery(self.database.path.parent)
        target_id = self._target(company_id, source_id)
        with ExitStack() as reservations:
            for aid in sorted((source_id, target_id)):
                reservations.enter_context(self.queue.mutation(aid))
            # Recheck ownership after reserving; a deletion can precede reservation.
            if self._target(company_id, source_id) != target_id:
                raise WorkspaceError("WORKSPACE_HISTORICAL_SOURCE_INVALID")
            with self.database.session() as session:
                files = []
                for file_id in file_ids:
                    item = session.get(UploadedFile, file_id)
                    if item is None or item.analysis_id != source_id:
                        raise WorkspaceError("WORKSPACE_HISTORICAL_FILE_INVALID")
                    session.expunge(item)
                    files.append(item)
            outcomes = []
            for item in files:
                try:
                    content = self._read_private(item)
                    result = self.uploads.add(target_id, item.original_filename, item.mime_type, content)
                    outcomes.append(HistoricalImportOutcome(source_file_id=item.id, file_id=result.id,
                        status="duplicate" if result.duplicate_of else "imported"))
                except WorkspaceError as error:
                    outcomes.append(self._error(item, error.code))
                except UploadRejected:
                    outcomes.append(self._error(item, "HISTORICAL_UPLOAD_INVALID"))
                except ValueError as error:
                    code = str(error) if str(error) in {"BATCH_SIZE_LIMIT", "BATCH_FILE_LIMIT"} else "UPLOAD_INVALID"
                    outcomes.append(self._error(item, "HISTORICAL_" + code))
                except (OSError, SQLAlchemyError):
                    # UploadService commits each file. Earlier successes remain visible to GET.
                    outcomes.append(self._error(item, "HISTORICAL_COPY_FAILED"))
            return HistoricalImportView(analysis_id=target_id, source_analysis_id=source_id, results=outcomes)

    @staticmethod
    def _error(item, code):
        return HistoricalImportOutcome(source_file_id=item.id, file_id=None, status="error", error_code=code)

    def _read_private(self, item):
        """Read only an exact owned storage entry through pinned, non-symlink FDs.

        The existing LocalStorage.open resolves links before opening and cannot provide
        this stronger read guarantee. Fail closed on platforms lacking dir_fd/no-follow.
        All size/type/hash checks precede UploadService's ordinary content validation.
        """
        try:
            if str(UUID(item.id)) != item.id or str(UUID(item.analysis_id)) != item.analysis_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise WorkspaceError("HISTORICAL_STORAGE_UNSAFE") from None
        extension = Path(item.original_filename).suffix.lower()
        expected = f"analyses/{item.analysis_id}/{item.id}{extension}"
        if extension not in ALLOWED or item.extension != extension or item.storage_key != expected:
            raise WorkspaceError("HISTORICAL_STORAGE_UNSAFE")
        if not 0 < item.size_bytes <= self.uploads.policy.max_bytes:
            raise WorkspaceError("HISTORICAL_SIZE_INVALID")
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise WorkspaceError("HISTORICAL_STORAGE_UNAVAILABLE")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            with ExitStack() as handles:
                root = self.uploads.storage.root
                fd = os.open(root.anchor, directory_flags)
                handles.callback(os.close, fd)
                # Walk trusted root AND owned analysis components without following links.
                for part in (*root.parts[1:], "analyses", item.analysis_id):
                    fd = os.open(part, directory_flags, dir_fd=fd)
                    handles.callback(os.close, fd)
                fd = os.open(f"{item.id}{extension}", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=fd)
                handles.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceError("HISTORICAL_STORAGE_UNSAFE")
                if info.st_size != item.size_bytes:
                    raise WorkspaceError("HISTORICAL_SIZE_INVALID")
                with os.fdopen(os.dup(fd), "rb") as stream:
                    content = stream.read(item.size_bytes + 1)
                if len(content) != item.size_bytes:
                    raise WorkspaceError("HISTORICAL_SIZE_INVALID")
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise WorkspaceError("HISTORICAL_CHECKSUM_MISMATCH")
                return content
        except FileNotFoundError:
            raise WorkspaceError("HISTORICAL_FILE_MISSING") from None
        except OSError:
            raise WorkspaceError("HISTORICAL_STORAGE_UNSAFE") from None
