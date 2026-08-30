from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from qian_labor.database import Database
from qian_labor.models.core import AnalysisBatch, UploadedFile
from qian_labor.security.uploads import UploadPolicy, validate_upload
from qian_labor.storage.local import LocalStorage


@dataclass(frozen=True)
class UploadResult:
    id: str
    original_filename: str
    size_bytes: int
    mime_type: str
    sha256: str
    status: str
    duplicate_of: str | None
    detected_kind: str
    progress: int
    error_code: str | None = None


class UploadService:
    def __init__(
        self, database: Database, storage: LocalStorage, policy: UploadPolicy | None = None
    ) -> None:
        self.database = database
        self.storage = storage
        self.policy = policy or UploadPolicy()

    def add(self, analysis_id: str, filename: str, mime: str, content: bytes) -> UploadResult:
        digest = validate_upload(filename, mime, content, self.policy.max_bytes)
        extension = Path(filename).suffix.lower()
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            current_bytes = session.scalar(
                select(func.coalesce(func.sum(UploadedFile.size_bytes), 0)).where(
                    UploadedFile.analysis_id == analysis_id
                )
            )
            current_files = session.scalar(
                select(func.count())
                .select_from(UploadedFile)
                .where(UploadedFile.analysis_id == analysis_id)
            )
            if int(current_bytes or 0) + len(content) > self.policy.max_batch_bytes:
                raise ValueError("BATCH_SIZE_LIMIT")
            if int(current_files or 0) >= self.policy.max_files:
                raise ValueError("BATCH_FILE_LIMIT")
            existing = session.scalar(
                select(UploadedFile).where(
                    UploadedFile.analysis_id == analysis_id,
                    UploadedFile.sha256 == digest,
                )
            )
            if existing:
                return UploadResult(
                    id=existing.id,
                    original_filename=filename,
                    size_bytes=len(content),
                    mime_type=mime,
                    sha256=digest,
                    status="duplicate",
                    duplicate_of=existing.id,
                    detected_kind=existing.detected_kind,
                    progress=existing.progress,
                )
            file_id = str(uuid4())
            storage_key = f"analyses/{analysis_id}/{file_id}{extension}"
            self.storage.save_bytes(content, storage_key)
            item = UploadedFile(
                id=file_id,
                analysis_id=analysis_id,
                original_filename=filename,
                storage_key=storage_key,
                mime_type=mime,
                extension=extension,
                size_bytes=len(content),
                sha256=digest,
                purge_at=analysis.purge_at,
            )
            session.add(item)
            analysis.file_count += 1
            analysis.status = "uploading"
            analysis.current_stage = "uploading"
            session.commit()
            return UploadResult(
                id=item.id,
                original_filename=item.original_filename,
                size_bytes=item.size_bytes,
                mime_type=item.mime_type,
                sha256=item.sha256,
                status=item.status,
                duplicate_of=None,
                detected_kind=item.detected_kind,
                progress=item.progress,
            )
