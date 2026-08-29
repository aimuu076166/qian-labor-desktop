from __future__ import annotations

from pathlib import Path

from qian_labor.database import Database
from qian_labor.models.core import UploadedFile
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


class DesktopImportService:
    def __init__(self, database: Database, data_dir: Path) -> None:
        self.database = database
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = LocalStorage(str(self.data_dir / "storage"))
        self.uploads = UploadService(database, self.storage)

    def import_paths(self, analysis_id: str, paths: list[Path]) -> list[UploadedFile]:
        imported_ids: list[str] = []
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            if not path.is_file():
                raise ValueError("DESKTOP_IMPORT_FILE_NOT_FOUND")
            extension = path.suffix.lower()
            mime_type = _MIME_BY_EXTENSION.get(extension)
            if mime_type is None:
                raise ValueError("DESKTOP_IMPORT_FORMAT_UNSUPPORTED")
            content = path.read_bytes()
            result = self.uploads.add(analysis_id, path.name, mime_type, content)
            imported_ids.append(result.duplicate_of or result.id)

        imported: list[UploadedFile] = []
        with self.database.session() as session:
            for file_id in imported_ids:
                item = session.get(UploadedFile, file_id)
                if item is None:
                    raise RuntimeError("DESKTOP_IMPORTED_FILE_MISSING")
                session.expunge(item)
                imported.append(item)
        return imported
