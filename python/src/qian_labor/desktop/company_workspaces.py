"""Authenticated by the desktop /api middleware; validation errors never echo inputs."""
from fastapi import APIRouter, Query
from contextlib import nullcontext
from uuid import UUID
from typing import Literal
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from qian_labor.desktop.company_schemas import (
    AdoptionRequest, BindingView, CompanyAnalysisPage, CompanyCreate, CompanyView, EmployeeCreate,
    EmployeeDetail, EmployeePage, EmployeeView, EmployeeDisplayNameUpdate, PreferenceUpdate, PreferenceView,
    CurrentAnalysisCreate, HistoricalImportRequest, HistoricalImportView,
)
from qian_labor.services.company_workspaces import CompanyWorkspaceService, WorkspaceError
from qian_labor.sqlite_migrations import MigrationError
from qian_labor.services.contract_advisory import ContractAdvisoryService, AdvisoryHandlingRequest
from qian_labor.services.effective_facts import EffectiveFactService, FactRevisionRequest
from qian_labor.services.assessment_state import AssessmentStateService, AssessmentDecisionRequest, ReevaluationRequest


class WorkspaceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse({"detail": {"code": "WORKSPACE_REQUEST_INVALID"}}, status_code=422)
            except WorkspaceError as error:
                return JSONResponse({"detail": {"code": error.code}}, status_code=error.status)
            except MigrationError:
                return JSONResponse({"detail": {"code": "DESKTOP_DB_RECOVERY_REQUIRED"}}, status_code=409)
            except RuntimeError as error:
                if str(error) != "DESKTOP_ANALYSIS_BUSY":
                    raise
                return JSONResponse({"detail": {"code": "DESKTOP_ANALYSIS_BUSY"}}, status_code=409)
        return safe_handler


def company_workspace_router(database, processing_queue, import_service):
    router = APIRouter(prefix="/api", route_class=WorkspaceRoute)
    service = CompanyWorkspaceService(database)
    advisory = ContractAdvisoryService(database)
    effective = EffectiveFactService(database)
    assessment = AssessmentStateService(database)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/effective-facts")
    def effective_facts(company_id: str, analysis_id: str, record_id: str | None = None, request_id: UUID | None = None,
                        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
        return effective.listing(company_id, analysis_id, record_id, request_id, page, page_size)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/effective-facts/{fact_id}/revisions")
    def fact_history(company_id: str, analysis_id: str, fact_id: str,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
        return effective.history(company_id, analysis_id, fact_id, page, page_size)

    @router.post("/company-workspaces/{company_id}/analyses/{analysis_id}/effective-facts/{fact_id}/revisions")
    def revise_fact(company_id: str, analysis_id: str, fact_id: str, request: FactRevisionRequest):
        with processing_queue.mutation(analysis_id):
            return effective.revise(company_id, analysis_id, fact_id, request)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/assessment-decisions")
    def assessment_decisions(company_id: str, analysis_id: str, record_id: str | None = None, request_id: UUID | None = None,
                             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
        return assessment.decisions(company_id, analysis_id, record_id, request_id, page, page_size)

    @router.post("/company-workspaces/{company_id}/analyses/{analysis_id}/assessment-decisions")
    def decide_assessment(company_id: str, analysis_id: str, request: AssessmentDecisionRequest):
        with processing_queue.mutation(analysis_id):
            return assessment.decide(company_id, analysis_id, request)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/assessment-results")
    def assessment_results(company_id: str, analysis_id: str, request_id: UUID | None = None,
                           page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
        return assessment.results(company_id, analysis_id, request_id, page, page_size)

    @router.post("/company-workspaces/{company_id}/analyses/{analysis_id}/reevaluate")
    def reevaluate(company_id: str, analysis_id: str, request: ReevaluationRequest):
        with processing_queue.mutation(analysis_id):
            return assessment.reevaluate(company_id, analysis_id, request)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/contract-advisories")
    def contract_advisories(company_id: str, analysis_id: str, file_id: str | None = None,
                            record_id: str | None = None, request_id: UUID | None = None,
                            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50), history: bool = False):
        return advisory.listing(company_id, analysis_id, file_id=file_id, record_id=record_id,
                                request_id=request_id, page=page, page_size=page_size, history=history)

    @router.post("/company-workspaces/{company_id}/analyses/{analysis_id}/contract-advisories/{observation_id}/handling")
    def handle_advisory(company_id: str, analysis_id: str, observation_id: str, request: AdvisoryHandlingRequest):
        with processing_queue.mutation(analysis_id):
            return advisory.handle(company_id, analysis_id, observation_id, request)

    @router.get("/company-workspaces", response_model=list[CompanyView])
    def companies():
        return service.companies()

    @router.post("/company-workspaces", response_model=CompanyView, status_code=201)
    def create_company(request: CompanyCreate):
        return service.create_company(request)

    @router.get("/company-workspaces/{company_id}", response_model=CompanyView)
    def company(company_id: str):
        return service.company(company_id)

    @router.get("/company-workspaces/{company_id}/current-analysis")
    def current_analysis(company_id: str):
        return service.current_analysis(company_id)

    @router.get("/company-workspaces/{company_id}/analyses", response_model=CompanyAnalysisPage)
    def analyses(company_id: str,
                 relation: Literal["historical", "unbound"] = "historical",
                 page: int = Query(1, ge=1),
                 page_size: int = Query(25, ge=1, le=100)):
        # Snapshot queue state first; its mutex is released before any DB work.
        active_analysis_id = processing_queue.active_analysis_id
        return service.analyses(company_id, relation, page, page_size, active_analysis_id)

    @router.post("/company-workspaces/{company_id}/current-analysis", status_code=201)
    def create_current_analysis(company_id: str, request: CurrentAnalysisCreate):
        with processing_queue.mutation(str(request.id)):
            return service.create_current_analysis(company_id, request)

    @router.post("/company-workspaces/{company_id}/current-analysis/import-historical",
                 response_model=HistoricalImportView)
    def import_historical(company_id: str, request: HistoricalImportRequest):
        from qian_labor.desktop.historical_import import HistoricalImportService
        return HistoricalImportService(database, import_service.uploads, processing_queue).copy(
            company_id, str(request.source_analysis_id), [str(fid) for fid in request.file_ids])

    @router.get("/company-workspaces/{company_id}/current")
    def current(company_id: str, search: str = Query("", max_length=100),
                page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                severity: Literal["high", "medium"] | None = None,
                assessment_state: Literal["pending_evidence", "pending_analysis", "evaluated"] | None = None):
        return service.current(company_id, search, page, page_size, severity, assessment_state)

    @router.get("/company-workspaces/{company_id}/employees", response_model=EmployeePage)
    def employees(company_id: str, search: str = Query("", max_length=100),
                  page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        return service.employees(company_id, search, page, page_size)

    @router.post("/company-workspaces/{company_id}/employees", response_model=EmployeeView, status_code=201)
    def create_employee(company_id: str, request: EmployeeCreate):
        return service.create_employee(company_id, request)

    @router.get("/company-workspaces/{company_id}/employees/{record_id}", response_model=EmployeeDetail)
    def employee(company_id: str, record_id: str):
        return service.employee(company_id, record_id)

    @router.put("/company-workspaces/{company_id}/employees/{record_id}/display-name", response_model=EmployeeView)
    def correct_display_name(company_id: str, record_id: str, request: EmployeeDisplayNameUpdate):
        current = service.current_analysis(company_id)
        with processing_queue.mutation(current["analysis_id"]) if current else nullcontext():
            return service.correct_display_name(company_id, record_id, request)

    @router.get("/company-workspaces/{company_id}/analyses/{analysis_id}/binding", response_model=BindingView)
    def binding(company_id: str, analysis_id: str):
        return service.binding(company_id, analysis_id)

    @router.put("/company-workspaces/{company_id}/analyses/{analysis_id}/binding", response_model=BindingView)
    def adopt(company_id: str, analysis_id: str, request: AdoptionRequest):
        with processing_queue.mutation(analysis_id):
            return service.adopt(company_id, analysis_id, request)

    @router.get("/workspace-preference", response_model=PreferenceView)
    def preference():
        return service.preference()

    @router.put("/workspace-preference", response_model=PreferenceView)
    def set_preference(request: PreferenceUpdate):
        return service.set_preference(request)

    return router
