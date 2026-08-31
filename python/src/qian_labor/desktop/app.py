from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from qian_labor.ai.provider_factory import provider_from_settings
from qian_labor.database import create_desktop_database
from qian_labor.desktop.auth import request_has_valid_token
from qian_labor.desktop.import_service import DesktopImportService
from qian_labor.desktop.queue import DesktopProcessingQueue
from qian_labor.desktop.schemas import (
    CreateAnalysisRequest,
    DashboardSummary,
    DesktopStatusResponse,
    FindingSource,
    FindingSummary,
    HealthResponse,
    ImportPathsRequest,
)
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import (
    AnalysisBatch,
    ProcessingJob,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)
from qian_labor.security.local_redaction import PrivacyBoundary
from qian_labor.services.analyses import AnalysisService
from qian_labor.services.assessment_gate import ensure_finding_access
from qian_labor.services.dashboard import DashboardService
from qian_labor.services.deletion import DeletionService
from qian_labor.settings import Settings, get_settings
from qian_labor.storage.local import LocalStorage


TAURI_PRODUCTION_ORIGINS = ("tauri://localhost", "http://tauri.localhost")


def _processing_payload(database, analysis_id: str) -> dict[str, object]:
    with database.session() as session:
        analysis = session.get(AnalysisBatch, analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        files = list(
            session.scalars(
                select(UploadedFile)
                .where(UploadedFile.analysis_id == analysis_id)
                .order_by(UploadedFile.created_at)
            )
        )
        jobs = list(
            session.scalars(
                select(ProcessingJob)
                .where(ProcessingJob.analysis_id == analysis_id)
                .order_by(ProcessingJob.started_at)
            )
        )
        return {
            "analysis_id": analysis.id,
            "status": analysis.status,
            "progress": analysis.progress,
            "current_stage": analysis.current_stage,
            "failure_reason": analysis.failure_reason,
            "files": [
                {
                    "id": item.id,
                    "filename": item.original_filename,
                    "detected_kind": item.detected_kind,
                    "status": item.status,
                    "progress": item.progress,
                    "error_code": item.error_code,
                }
                for item in files
            ],
            "jobs": [
                {
                    "id": job.id,
                    "file_id": job.file_id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "attempts": job.attempts,
                    "error_code": job.error_code,
                }
                for job in jobs
            ],
        }


def _finding_detail(database, finding_id: str) -> dict[str, object]:
    with database.session() as session:
        finding = session.get(RiskFinding, finding_id)
        if finding is None:
            raise KeyError(finding_id)
        ensure_finding_access(session, finding)
        source_ids = list(dict.fromkeys(finding.source_locator_ids or []))
        sources_by_id = {
            item.id: item
            for item in session.scalars(
                select(SourceLocator).where(SourceLocator.id.in_(source_ids))
            )
        }
        sources: list[FindingSource] = []
        for source_id in source_ids:
            item = sources_by_id.get(source_id)
            if item is None:
                continue
            uploaded_file = session.get(UploadedFile, item.file_id)
            if uploaded_file is None:
                continue
            sources.append(
                FindingSource(
                    id=item.id,
                    file_id=item.file_id,
                    file_name=uploaded_file.original_filename,
                    locator_type=item.locator_type,
                    location=item.location,
                    excerpt=item.excerpt,
                )
            )
        return {
            "id": finding.id,
            "analysis_id": finding.analysis_id,
            "rule_id": finding.rule_id,
            "title": finding.title,
            "severity": finding.severity,
            "assessment_status": finding.assessment_status,
            "requires_human_review": finding.requires_human_review,
            "summary": finding.summary,
            "sources": [item.model_dump() for item in sources],
        }


def create_desktop_app(
    *,
    data_dir: Path,
    launch_token: str,
    settings: Settings | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    if not launch_token:
        raise ValueError("DESKTOP_TOKEN_REQUIRED")

    resolved_settings = settings or get_settings()
    provider = provider_from_settings(resolved_settings)
    provider_is_external = bool(
        getattr(provider, "is_external", getattr(provider, "name", "fake") != "fake")
    )
    privacy_pepper = (
        resolved_settings.pii_hash_pepper
        if provider_is_external
        else "desktop-synthetic-local-pepper"
    )

    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database = create_desktop_database(data_dir)
    storage_root = data_dir / "storage"
    storage = LocalStorage(str(storage_root))
    import_service = DesktopImportService(database, data_dir)
    processing_queue = DesktopProcessingQueue(
        lambda: ProcessingPipeline(
            database,
            storage,
            provider,
            privacy_boundary=PrivacyBoundary(privacy_pepper),
            max_provider_calls=resolved_settings.ai_max_calls_per_analysis,
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            processing_queue.shutdown()
            database.dispose()

    app = FastAPI(
        title="企安用工 Desktop Sidecar",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.database_path = database.path
    app.state.data_dir = data_dir
    app.state.storage_root = storage_root
    app.state.import_service = import_service
    app.state.processing_queue = processing_queue
    app.state.ai_provider_name = provider.name

    @app.middleware("http")
    async def require_launch_token(request: Request, call_next):
        if request.url.path.startswith("/api/") and not request_has_valid_token(
            request, launch_token
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": {"code": "DESKTOP_TOKEN_REQUIRED"}},
            )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(TAURI_PRODUCTION_ORIGINS),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Qian-Desktop-Token"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="qian-labor-desktop-sidecar")

    @app.get("/api/status", response_model=DesktopStatusResponse)
    def status() -> DesktopStatusResponse:
        return DesktopStatusResponse(status="ready", database_path=str(database.path))

    @app.post("/api/internal/shutdown", status_code=http_status.HTTP_202_ACCEPTED)
    def request_shutdown() -> dict[str, str]:
        if shutdown_callback is None:
            raise HTTPException(
                http_status.HTTP_503_SERVICE_UNAVAILABLE,
                {"code": "DESKTOP_SHUTDOWN_UNAVAILABLE"},
            )
        shutdown_callback()
        return {"status": "shutdown_requested"}

    @app.post("/api/analyses", status_code=http_status.HTTP_201_CREATED)
    def create_analysis(body: CreateAnalysisRequest) -> dict[str, object]:
        item = AnalysisService(database).create(body.name, body.company_display_name)
        return AnalysisService.payload(item)

    @app.post("/api/analyses/{analysis_id}/import-paths")
    def import_paths(analysis_id: str, body: ImportPathsRequest) -> dict[str, object]:
        try:
            files = import_service.import_paths(
                analysis_id,
                [Path(value) for value in body.paths],
            )
        except KeyError:
            raise HTTPException(404, {"code": "ANALYSIS_NOT_FOUND"}) from None
        except ValueError as error:
            raise HTTPException(400, {"code": str(error)}) from error
        return {
            "analysis_id": analysis_id,
            "files": [
                {
                    "id": item.id,
                    "original_filename": item.original_filename,
                    "size_bytes": item.size_bytes,
                    "status": item.status,
                    "detected_kind": item.detected_kind,
                }
                for item in files
            ],
        }

    @app.post(
        "/api/analyses/{analysis_id}/process",
        status_code=http_status.HTTP_202_ACCEPTED,
    )
    def process_analysis(analysis_id: str) -> dict[str, object]:
        previous_state: tuple[str, str, int] | None = None
        with database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise HTTPException(404, {"code": "ANALYSIS_NOT_FOUND"})
            has_file = session.scalar(
                select(UploadedFile.id).where(UploadedFile.analysis_id == analysis_id).limit(1)
            )
            if has_file is None:
                raise HTTPException(409, {"code": "ANALYSIS_HAS_NO_FILES"})
            if analysis.status in {"created", "uploading", "uploaded"}:
                previous_state = (analysis.status, analysis.current_stage, analysis.progress)
                analysis.status = "queued"
                analysis.current_stage = "queued"
                analysis.progress = max(1, analysis.progress)
                session.commit()

        def restore_unsubmitted_state() -> None:
            if previous_state is None:
                return
            with database.session() as session:
                analysis = session.get(AnalysisBatch, analysis_id)
                if analysis is not None and analysis.status == "queued":
                    analysis.status, analysis.current_stage, analysis.progress = previous_state
                    session.commit()

        try:
            result = processing_queue.submit(analysis_id)
        except RuntimeError as error:
            restore_unsubmitted_state()
            if str(error) == "DESKTOP_ANALYSIS_BUSY":
                raise HTTPException(409, {"code": "DESKTOP_ANALYSIS_BUSY"}) from None
            raise
        except Exception:
            restore_unsubmitted_state()
            raise
        return result

    @app.get("/api/analyses/{analysis_id}/processing")
    def processing_status(analysis_id: str) -> dict[str, object]:
        try:
            return _processing_payload(database, analysis_id)
        except KeyError:
            raise HTTPException(404, {"code": "ANALYSIS_NOT_FOUND"}) from None

    @app.get("/api/analyses/{analysis_id}/dashboard")
    def dashboard(analysis_id: str) -> dict[str, object]:
        service = DashboardService(database)
        try:
            dashboard_payload = service.get(analysis_id)
            findings = service.findings(analysis_id)
        except KeyError:
            raise HTTPException(404, {"code": "ANALYSIS_NOT_FOUND"}) from None
        summary_payload = dashboard_payload["summary"]
        summary = DashboardSummary(
            analysis_id=analysis_id,
            status=str(dashboard_payload["status"]),
            employee_count=int(summary_payload["employee_count"]),
            finding_count=len(findings),
            high_count=int(summary_payload["high_count"]),
            medium_count=int(summary_payload["medium_count"]),
            insufficient_data_count=int(summary_payload["insufficient_data_count"]),
        )
        finding_items = [
            FindingSummary(
                id=str(item["id"]),
                rule_id=str(item["rule_id"]),
                title=str(item["title"]),
                severity=str(item["severity"]),
                assessment_status=str(item["assessment_status"]),
                requires_human_review=bool(item["requires_human_review"]),
            )
            for item in findings
        ]
        return {
            "summary": summary.model_dump(),
            "findings": [item.model_dump() for item in finding_items],
        }

    @app.get("/api/findings/{finding_id}")
    def finding_detail(finding_id: str) -> dict[str, object]:
        try:
            return _finding_detail(database, finding_id)
        except KeyError:
            raise HTTPException(404, {"code": "FINDING_NOT_FOUND"}) from None

    @app.delete("/api/analyses/{analysis_id}")
    def delete_analysis(analysis_id: str) -> dict[str, str]:
        if processing_queue.active_analysis_id == analysis_id:
            raise HTTPException(409, {"code": "DESKTOP_ANALYSIS_BUSY"})
        return DeletionService(database, str(storage_root)).delete(analysis_id)

    return app
