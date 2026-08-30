import shutil
from pathlib import Path

from sqlalchemy import select

from qian_labor.database import Database
from qian_labor.models.core import (
    AnalysisBatch,
    DeletionTombstone,
    ProcessingJob,
    UploadedFile,
    utcnow,
)


class DeletionService:
    def __init__(self, database: Database, storage_root: str) -> None:
        self.database = database
        self.storage_root = Path(storage_root).resolve()

    def delete(self, analysis_id: str, *, reason: str = "user_requested") -> dict[str, str]:
        with self.database.session() as session:
            tombstone = session.get(DeletionTombstone, analysis_id)
            if tombstone:
                deleted_at = tombstone.deleted_at
                storage_keys: list[str] = []
            else:
                analysis = session.get(AnalysisBatch, analysis_id)
                if analysis is None:
                    return {"id": analysis_id, "status": "deleted"}
                storage_keys = list(
                    session.scalars(
                        select(UploadedFile.storage_key).where(
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

        self._remove_files(analysis_id, storage_keys)

        if tombstone:
            return {
                "id": analysis_id,
                "status": "deleted",
                "deleted_at": deleted_at.isoformat(),
            }

        with self.database.session() as session:
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

    def _remove_files(self, analysis_id: str, storage_keys: list[str]) -> None:
        for key in storage_keys:
            path = (self.storage_root / key).resolve()
            if not path.is_relative_to(self.storage_root):
                raise RuntimeError("UNSAFE_STORAGE_PATH")
            path.unlink(missing_ok=True)
        analysis_directory = (self.storage_root / "analyses" / analysis_id).resolve()
        if not analysis_directory.is_relative_to(self.storage_root):
            raise RuntimeError("UNSAFE_STORAGE_PATH")
        if analysis_directory.exists():
            shutil.rmtree(analysis_directory)
