"""Read-only workspace navigation, independent of processing and provider state."""

from collections import Counter

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from qian_labor.database import Database
from qian_labor.security.filenames import display_filename
from qian_labor.models.core import AnalysisBatch, EmploymentFact, UploadedFile, ProcessingJob, ParsedDocument, ContractAdvisoryRun, AuditEvent
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.ai.grounding import EXTRACTION_VERSION
from qian_labor.services.analyses import AnalysisService
from qian_labor.services.effective_facts import effective_projection


def workspace_router(database: Database) -> APIRouter:
    router = APIRouter(prefix="/api/analyses")

    @router.get("")
    def list_analyses(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        with database.session() as session:
            visible = AnalysisBatch.deleted_at.is_(None)
            total = session.scalar(select(func.count()).select_from(AnalysisBatch).where(visible)) or 0
            analyses = session.scalars(select(AnalysisBatch).where(visible).order_by(
                AnalysisBatch.created_at.desc(), AnalysisBatch.id.desc(),
            ).offset((page - 1) * page_size).limit(page_size))
            return {"items": [AnalysisService.payload(item) for item in analyses],
                    "total": total, "page": page, "page_size": page_size,
                    "pages": (total + page_size - 1) // page_size}

    @router.get("/{analysis_id}/workspace")
    def get_workspace(analysis_id: str):
        with database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None or analysis.deleted_at is not None:
                raise HTTPException(404, {"code": "DESKTOP_ANALYSIS_NOT_FOUND"})
            files = session.scalars(select(UploadedFile).where(
                UploadedFile.analysis_id == analysis_id,
            ).order_by(UploadedFile.created_at, UploadedFile.id))
            fact_counts = dict(session.execute(select(EmploymentFact.file_id, func.count()).where(
                EmploymentFact.analysis_id == analysis_id,
            ).group_by(EmploymentFact.file_id)).all())
            display_fact_counts = Counter(
                row.file_id for row in effective_projection(session, analysis_id) if row.file_id
            )
            extraction_jobs = list(session.scalars(select(ProcessingJob).where(
                ProcessingJob.analysis_id == analysis_id, ProcessingJob.job_type == "extract")))
            succeeded = {job.unique_key for job in extraction_jobs if job.status == "succeeded"}
            previously_extracted = {job.file_id for job in extraction_jobs}
            advisory_runs = {run.file_id: run for run in session.scalars(select(ContractAdvisoryRun).where(
                ContractAdvisoryRun.analysis_id == analysis_id).order_by(ContractAdvisoryRun.created_at, ContractAdvisoryRun.id))}
            diagnostics: dict[str, tuple[str | None, dict]] = {}
            for event in session.scalars(select(AuditEvent).where(
                AuditEvent.analysis_id == analysis_id,
                AuditEvent.event_type == "processing_failed",
            ).order_by(AuditEvent.created_at, AuditEvent.id)):
                metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
                file_id = metadata.get("file_id")
                diagnostic = metadata.get("diagnostic")
                if isinstance(file_id, str) and isinstance(diagnostic, dict):
                    diagnostics[file_id] = (metadata.get("error_code"), diagnostic)
            def diagnostic_for(item, error_code):
                saved = diagnostics.get(item.id)
                return saved[1] if saved and saved[0] == error_code else None
            warnings = dict(session.execute(select(ParsedDocument.file_id, ParsedDocument.warnings).join(
                UploadedFile, UploadedFile.id == ParsedDocument.file_id).where(UploadedFile.analysis_id == analysis_id)).all())
            return {"analysis": AnalysisService.payload(analysis), "files": [{
                "id": item.id, "filename": display_filename(item.original_filename),
                "status": item.status, "progress": item.progress,
                "detected_kind": item.detected_kind, "classified_kind": item.classified_kind,
                "error_code": item.error_code or (
                    "AI_NO_SUPPORTED_FACTS" if item.status == "processed" and not fact_counts.get(item.id) and item.id not in advisory_runs else None),
                "error_diagnostic": diagnostic_for(item, item.error_code or (
                    "AI_NO_SUPPORTED_FACTS" if item.status == "processed" and not fact_counts.get(item.id) and item.id not in advisory_runs else None)),
                "advisory_status": advisory_runs[item.id].execution_status if item.id in advisory_runs else "not_executed",
                "fact_count": display_fact_counts.get(item.id, 0), "size_bytes": item.size_bytes,
                "warnings": [w for w in warnings.get(item.id, []) if w in {"embedded_images_need_vision", "empty_csv"}],
                "extraction_version": EXTRACTION_VERSION if ProcessingPipeline._job_key(analysis_id, item.id, "extract", item.sha256) in succeeded else None,
                "needs_reextraction": (item.id in previously_extracted or item.status in {"processed", "partial"}) and
                    (ProcessingPipeline._job_key(analysis_id, item.id, "extract", item.sha256) not in succeeded
                     or item.extension.lower() in {".xlsx", ".xls"} and
                        ProcessingPipeline.has_unlocated_sources(session, analysis_id, item.id)),
            } for item in files]}

    return router
