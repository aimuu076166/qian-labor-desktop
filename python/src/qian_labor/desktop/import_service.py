from __future__ import annotations

from pathlib import Path
import os
import stat

from qian_labor.database import Database
from qian_labor.models.core import AnalysisBatch, UploadedFile
from qian_labor.security.filenames import display_filename, validate_filename
from qian_labor.security.uploads import UploadRejected
from qian_labor.services.company_workspaces import require_material_mutation
from qian_labor.services.uploads import UploadService
from qian_labor.storage.local import LocalStorage

_MIME_BY_EXTENSION = {
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class ImportBatch(list[UploadedFile]):
    """Compatible success sequence with additive ordered selection outcomes."""
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, object]] = []


class DesktopImportService:
    def __init__(self, database: Database, data_dir: Path) -> None:
        self.database = database
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = LocalStorage(str(self.data_dir / "storage"))
        self.uploads = UploadService(database, self.storage)

    def import_paths(self, analysis_id: str, paths: list[Path]) -> ImportBatch:
        # Refuse historical/missing analyses before inspecting any selected object.
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None or analysis.deleted_at is not None:
                raise KeyError(analysis_id)
            require_material_mutation(session, analysis_id)
        imported = ImportBatch()
        for index, raw_path in enumerate(paths):
            outcome = {'index': index, 'filename': display_filename(raw_path.name),
                       'file_id': None, 'status': 'error', 'error_code': None}
            try:
                validate_filename(raw_path.name)
                mime = _MIME_BY_EXTENSION.get(raw_path.suffix.lower())
                if mime is None:
                    raise ValueError('DESKTOP_IMPORT_FORMAT_UNSUPPORTED')
                # Nonblocking open makes FIFOs safe; fstat validates the opened object,
                # including replacements after path resolution. User symlinks are allowed.
                path = raw_path.expanduser().resolve()
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    metadata = os.fstat(fd)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError('DESKTOP_IMPORT_NOT_REGULAR')
                    if metadata.st_size > self.uploads.policy.max_bytes:
                        raise ValueError('DESKTOP_IMPORT_TOO_LARGE')
                    with os.fdopen(fd, 'rb', closefd=False) as stream:
                        content = stream.read(self.uploads.policy.max_bytes + 1)
                finally:
                    os.close(fd)
                if not content:
                    raise ValueError('DESKTOP_IMPORT_EMPTY')
                if len(content) > self.uploads.policy.max_bytes:
                    raise ValueError('DESKTOP_IMPORT_TOO_LARGE')
                result = self.uploads.add(analysis_id, raw_path.name, mime, content)
                with self.database.session() as session:
                    item = session.get(UploadedFile, result.id)
                    if item is None:
                        raise RuntimeError('DESKTOP_IMPORTED_FILE_MISSING')
                    session.expunge(item)
                    imported.append(item)
                outcome.update(filename=display_filename(item.original_filename), file_id=item.id,
                               status='duplicate' if result.duplicate_of else 'imported')
            except FileNotFoundError:
                outcome['error_code'] = 'DESKTOP_IMPORT_FILE_NOT_FOUND'
            except PermissionError:
                outcome['error_code'] = 'DESKTOP_IMPORT_PERMISSION_DENIED'
            except UploadRejected:
                outcome['error_code'] = 'DESKTOP_IMPORT_CONTENT_INVALID'
            except ValueError as error:
                known = {'DESKTOP_IMPORT_FORMAT_UNSUPPORTED', 'DESKTOP_IMPORT_NOT_REGULAR',
                         'DESKTOP_IMPORT_TOO_LARGE', 'DESKTOP_IMPORT_EMPTY', 'BATCH_SIZE_LIMIT', 'BATCH_FILE_LIMIT'}
                outcome['error_code'] = str(error) if str(error) in known else 'DESKTOP_IMPORT_FAILED'
            except Exception:
                outcome['error_code'] = 'DESKTOP_IMPORT_FAILED'
            imported.results.append(outcome)
        return imported
