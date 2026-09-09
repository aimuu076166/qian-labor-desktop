"""One read-only corpus/result revision contract; explicit local reevaluation."""
from datetime import date
from uuid import UUID, uuid4
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func

from qian_labor.models.core import (
    AnalysisBatch, UploadedFile, ParsedDocument, EmploymentFact, SourceLocator,
    Employee, EmployeeSnapshotBinding, EmployeeMatchCandidate, EmployeeMatchDecision,
    EffectiveFactRevision, AssessmentDecision, AssessmentResult, CompanyAnalysisBinding,
    RiskFinding, ContractClauseObservation, FindingReview, ContractAdvisoryHandling,
)
from qian_labor.services.effective_facts import (
    ReasonRequest, digest, iso_date, fail, require_owner, require_record, latest_decision,
    effective_projection, contract_dependency, CONTRACT_TYPES,
)


class AssessmentDecisionRequest(ReasonRequest):
    kind: Literal["check_date", "current_contract"]
    record_id: UUID | None = None
    value: Any
    expected_dependency_signature: str = Field(default="", max_length=64)


class ReevaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_input_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def check_date(s, analysis):
    choice = latest_decision(s, analysis.id, "check_date")
    return (choice.value, True) if choice else (analysis.created_at.date().isoformat(), False)


def page_rows(s, query, page, page_size, serializer):
    total = s.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    return {"items": [serializer(row) for row in s.scalars(query.offset((page-1)*page_size).limit(page_size))],
            "total": total, "page": page, "page_size": page_size, "pages": (total+page_size-1)//page_size}


def _table_rows(s, model, analysis_id, fields):
    return [[getattr(row, name) for name in fields] for row in s.scalars(
        select(model).where(model.analysis_id == analysis_id).order_by(model.id if hasattr(model, "id") else model.snapshot_id))]


def input_revision(s, analysis_id):
    analysis = s.get(AnalysisBatch, analysis_id)
    contents = {
        "profile": analysis.assessment_profile, "check_date": check_date(s, analysis),
        "files": _table_rows(s, UploadedFile, analysis_id, ["id", "sha256", "classified_kind", "status", "error_code"]),
        "facts": _table_rows(s, EmploymentFact, analysis_id, ["id", "employee_id", "file_id", "fact_type", "value_json", "normalized_value_json", "verification_status"]),
        "sources": _table_rows(s, SourceLocator, analysis_id, ["id", "file_id", "fact_id", "locator_type", "location", "excerpt", "content_hash"]),
        "employees": _table_rows(s, Employee, analysis_id, ["id", "match_status", "employment_status"]),
        "bindings": _table_rows(s, EmployeeSnapshotBinding, analysis_id, ["snapshot_id", "employee_record_id"]),
        "candidates": _table_rows(s, EmployeeMatchCandidate, analysis_id, ["id", "status", "extracted_fields"]),
        "matching": _table_rows(s, EmployeeMatchDecision, analysis_id, ["id", "candidate_id", "decision", "target_employee_id"]),
        "human": _table_rows(s, EffectiveFactRevision, analysis_id, ["id", "fact_id", "version", "value", "owner_signature", "source_signature", "context_signature"]),
        "decisions": _table_rows(s, AssessmentDecision, analysis_id, ["id", "scope", "version", "value", "dependency_signature"]),
        "parsed_documents": [[row.file_id, row.content_hash, row.warnings] for row in s.scalars(select(ParsedDocument).join(
            UploadedFile, UploadedFile.id == ParsedDocument.file_id).where(UploadedFile.analysis_id == analysis_id).order_by(ParsedDocument.file_id))],
    }
    return digest(contents)


def material_state(s, analysis_id):
    files = list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id)))
    owner = s.get(CompanyAnalysisBinding, analysis_id)
    available = any(row.state.valid and row.employee_id is not None and
                    (not owner or owner.role != "current" or row.state.owned)
                    for row in effective_projection(s, analysis_id))
    pending = s.scalar(select(EmployeeMatchCandidate.id).where(EmployeeMatchCandidate.analysis_id == analysis_id,
        EmployeeMatchCandidate.status == "pending").limit(1)) is not None
    warnings = any(row.warnings for row in s.scalars(select(ParsedDocument).join(UploadedFile,
        UploadedFile.id == ParsedDocument.file_id).where(UploadedFile.analysis_id == analysis_id)))
    incomplete = warnings or any(file.status != "processed" or file.error_code for file in files)
    completeness = "pending" if not available or pending else "partial" if incomplete else "complete"
    return {"availability": "available" if available else "none", "completeness": completeness}


def latest_result(s, analysis_id):
    return s.scalar(select(AssessmentResult).where(AssessmentResult.analysis_id == analysis_id)
        .order_by(AssessmentResult.evaluated_at.desc(), AssessmentResult.id.desc()).limit(1))


def result_payload(row):
    return None if row is None else {"id": row.id, "result_revision": row.id, "analysis_id": row.analysis_id,
        "input_revision": row.input_revision, "check_date": row.check_date, "evaluated_at": row.evaluated_at.isoformat()}


def assessment_metadata(s, analysis_id):
    analysis = s.get(AnalysisBatch, analysis_id)
    owner = s.get(CompanyAnalysisBinding, analysis_id)
    current_input = input_revision(s, analysis_id)
    result = latest_result(s, analysis_id)
    day, chosen = check_date(s, analysis)
    # Review events are immutable; the result UUID covers evaluation/reopening.
    # This does not turn a paged employee summary into a full findings scan.
    reviews = [list(row) for row in s.execute(select(FindingReview.id, FindingReview.new_status).join(
        RiskFinding, RiskFinding.id == FindingReview.finding_id).where(RiskFinding.analysis_id == analysis_id).order_by(FindingReview.id))]
    advisory = [list(row) for row in s.execute(select(ContractAdvisoryHandling.id, ContractAdvisoryHandling.version).join(
        ContractClauseObservation, ContractClauseObservation.id == ContractAdvisoryHandling.observation_id).where(
        ContractClauseObservation.analysis_id == analysis_id).order_by(ContractAdvisoryHandling.id))]
    return {"input_revision": current_input, "result_revision": result.id if result else None,
        "evaluated_input_revision": result.input_revision if result else None,
        "fresh": bool(result and result.input_revision == current_input),
        "check_date": day, "check_date_explicit": chosen,
        "evaluated_at": result.evaluated_at.isoformat() if result else None,
        "read_only": not owner or owner.role != "current",
        "report_review_revision": digest([result.id if result else None, reviews, advisory]),
        **material_state(s, analysis_id)}


def record_evaluation(s, analysis, request_id=None):
    owner = s.get(CompanyAnalysisBinding, analysis.id)
    if not owner or owner.role != "current":
        return
    s.flush()
    row = AssessmentResult(id=request_id or str(uuid4()), analysis_id=analysis.id,
        input_revision=input_revision(s, analysis.id), check_date=check_date(s, analysis)[0])
    s.add(row)
    s.flush()


def decision_payload(row):
    return None if row is None else {"id": row.id, "kind": row.kind, "version": row.version,
        "record_id": row.record_id, "employee_id": row.employee_id, "value": row.value,
        "reason": row.reason, "actor": "local-user", "created_at": row.created_at.isoformat(),
        "dependency_signature": row.dependency_signature}


class AssessmentStateService:
    def __init__(self, database): self.database = database

    def decisions(self, company_id, analysis_id, record_id=None, request_id=None, page=1, page_size=20):
        with self.database.session() as s:
            owner, analysis = require_owner(s, company_id, analysis_id)
            employee = require_record(s, company_id, analysis_id, record_id) if record_id else None
            query = select(AssessmentDecision).where(AssessmentDecision.analysis_id == analysis_id)
            if employee:
                query = query.where((AssessmentDecision.employee_id == employee.id) | (AssessmentDecision.kind == "check_date"))
            result = page_rows(s, query.order_by(AssessmentDecision.created_at.desc(), AssessmentDecision.id.desc()), page, page_size, decision_payload)
            request = s.get(AssessmentDecision, str(request_id)) if request_id else None
            if request and (request.company_id != company_id or request.analysis_id != analysis_id or
                            (record_id and request.record_id not in {None, record_id})):
                fail("FACT_OWNERSHIP_INVALID", 404)
            day = latest_decision(s, analysis_id, "check_date")
            contract = latest_decision(s, analysis_id, "current_contract:"+employee.id) if employee else None
            rows = effective_projection(s, analysis_id)
            dependency = contract_dependency(rows, employee.id) if employee else ""
            candidates = sorted({r.file_id for r in rows if employee and r.employee_id == employee.id and
                r.fact_type in CONTRACT_TYPES and r.state.valid and r.state.file.classified_kind == "contract"})
            return {**result, "request_decision": decision_payload(request), "read_only": owner.role != "current",
                "check_date": check_date(s, analysis)[0], "check_date_version": day.version if day else 0,
                "current_contract": decision_payload(contract), "current_contract_version": contract.version if contract else 0,
                "current_contract_valid": bool(contract and contract.dependency_signature == dependency),
                "contract_dependency_signature": dependency, "contract_file_ids": candidates[:50],
                "assessment_revision": assessment_metadata(s, analysis_id)}

    def decide(self, company_id, analysis_id, request):
        from qian_labor.services.company_workspaces import CompanyWorkspaceService
        with CompanyWorkspaceService(self.database)._write() as s:
            require_owner(s, company_id, analysis_id, write=True)
            eid = None
            dependency = ""
            if request.kind == "check_date":
                if request.record_id:
                    fail("ASSESSMENT_DECISION_INVALID", 422)
                try: value = iso_date(request.value)
                except (ValueError, TypeError): fail("ASSESSMENT_DECISION_INVALID", 422)
                scope = "check_date"
            else:
                if not request.record_id:
                    fail("ASSESSMENT_DECISION_INVALID", 422)
                employee = require_record(s, company_id, analysis_id, str(request.record_id))
                eid = employee.id
                scope = "current_contract:" + eid
                rows = effective_projection(s, analysis_id)
                dependency = contract_dependency(rows, eid)
                value = request.value
                if type(value) is not str or not any(r.employee_id == eid and r.file_id == value and
                        r.fact_type in CONTRACT_TYPES and r.state.valid and r.state.file.classified_kind == "contract" for r in rows):
                    fail("ASSESSMENT_CONTRACT_INVALID", 422)
                if request.expected_dependency_signature != dependency:
                    fail("ASSESSMENT_VERSION_CONFLICT")
            previous = latest_decision(s, analysis_id, scope)
            if s.get(AssessmentDecision, str(request.id)) or (previous.version if previous else 0) != request.expected_version:
                fail("ASSESSMENT_VERSION_CONFLICT")
            row = AssessmentDecision(id=str(request.id), analysis_id=analysis_id, company_id=company_id,
                employee_id=eid, record_id=str(request.record_id) if request.record_id else None,
                scope=scope, kind=request.kind, version=request.expected_version+1, value=value,
                reason=request.reason, dependency_signature=dependency)
            s.add(row)
            s.flush()
            return decision_payload(row)

    def results(self, company_id, analysis_id, request_id=None, page=1, page_size=20):
        with self.database.session() as s:
            require_owner(s, company_id, analysis_id)
            request = s.get(AssessmentResult, str(request_id)) if request_id else None
            if request and request.analysis_id != analysis_id: fail("FACT_OWNERSHIP_INVALID", 404)
            return {**page_rows(s, select(AssessmentResult).where(AssessmentResult.analysis_id == analysis_id)
                .order_by(AssessmentResult.evaluated_at.desc(), AssessmentResult.id.desc()), page, page_size, result_payload),
                "request_result": result_payload(request), "assessment_revision": assessment_metadata(s, analysis_id)}

    def reevaluate(self, company_id, analysis_id, request):
        from qian_labor.services.company_workspaces import CompanyWorkspaceService
        from qian_labor.services.risk_evaluation import RiskEvaluationService
        with CompanyWorkspaceService(self.database)._write() as s:
            _, analysis = require_owner(s, company_id, analysis_id, write=True)
            if s.get(AssessmentResult, str(request.id)) or input_revision(s, analysis_id) != request.expected_input_revision:
                fail("ASSESSMENT_VERSION_CONFLICT")
            if any(row.file_id and not row.state.valid for row in effective_projection(s, analysis_id)):
                fail("FACT_SOURCE_INVALID")
            if any(row.employee_id and not row.state.owned for row in effective_projection(s, analysis_id)):
                fail("FACT_OWNERSHIP_INVALID")
            s.info["assessment_request_id"] = str(request.id)
            RiskEvaluationService(self.database).evaluate_analysis(analysis_id, db_session=s)
            return assessment_metadata(s, analysis_id)
