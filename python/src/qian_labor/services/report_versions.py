"""Immutable local report artifacts; no evaluation, parsing or provider entry points."""
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import OperationalError
from qian_labor.ai.grounding import EXTRACTION_VERSION, PROOF_KEY

from qian_labor.models.core import (
    AnalysisBatch, CompanyAnalysisBinding, CompanyWorkspace, Employee, EmployeeRecord,
    EmployeeSnapshotBinding, UploadedFile, EmploymentFact, SourceLocator, ParsedDocument,
    RiskFinding, ContractAdvisoryRun, ContractClauseObservation, TaskRun, ProcessingJob,
    ReportVersion, ReportGenerationRequest,
)
from qian_labor.security.masking import mask_sensitive
from qian_labor.services.company_workspaces import WorkspaceError
from qian_labor.services.effective_facts import effective_projection, public_fact, safe_value, valid_source_metadata
from qian_labor.services.finding_sources import owned_finding_evidence
from qian_labor.services.contract_advisory import ContractAdvisoryService
from qian_labor.services.report import ReportService
from qian_labor.security.filenames import display_filename
from qian_labor.sqlite_migrations import assert_no_pending_recovery


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def masked_display(value, key=""):
    """Mask complete display content without damaging opaque IDs or dates."""
    if key == "location":
        # Location labels are human-readable text, not trusted metadata names.
        return safe_value(value)
    if isinstance(value, dict):
        return {k: masked_display(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [masked_display(v, key) for v in value]
    if isinstance(value, str) and not (key == "id" or key.endswith(("_id", "_ids", "_revision", "_signature", "_hash", "_sha256", "_at", "_date"))):
        return mask_sensitive(value)
    return value


class GenerateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_input_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_result_revision: UUID | None
    expected_review_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_context_signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportVersionService:
    def __init__(self, database, queue):
        self.database, self.queue = database, queue

    @staticmethod
    def owner(s, company_id, analysis_id):
        owner = s.get(CompanyAnalysisBinding, analysis_id)
        analysis = s.get(AnalysisBatch, analysis_id)
        company = s.get(CompanyWorkspace, company_id)
        if not company or not owner or owner.company_id != company_id or not analysis or analysis.deleted_at or analysis.status == "deleting":
            raise WorkspaceError("REPORT_NOT_FOUND", 404)
        return owner, analysis, company

    @staticmethod
    def metadata(row):
        return {"id": row.id, "company_id": row.company_id, "analysis_id": row.analysis_id,
                "version": row.version, "created_at": row.created_at.replace(tzinfo=UTC).isoformat(),
                "content_sha256": row.content_sha256, "input_revision": row.input_revision,
                "result_revision": row.result_revision, "review_revision": row.review_revision,
                "context_signature": row.context_signature}

    def snapshot(self, row):
        if hashlib.sha256(row.payload_json.encode()).hexdigest() != row.content_sha256:
            raise WorkspaceError("REPORT_SNAPSHOT_INVALID")
        return {**self.metadata(row), "payload": json.loads(row.payload_json)}

    def receipt(self, s, row):
        saved = s.get(ReportVersion, row.version_id) if row.version_id else None
        if saved and (saved.company_id != row.company_id or saved.analysis_id != row.analysis_id or saved.content_sha256 != row.content_sha256):
            raise WorkspaceError("REPORT_SNAPSHOT_INVALID")
        if row.outcome == "accepted" and not saved:
            raise WorkspaceError("REPORT_SNAPSHOT_INVALID")
        return {"request": {**json.loads(row.body_json), "outcome": row.outcome,
                    "error_code": row.error_code, "version_id": row.version_id,
                    "content_sha256": row.content_sha256},
                "snapshot": self.snapshot(saved) if saved else None}

    def duplicate(self, s, identity):
        row = s.get(ReportGenerationRequest, identity["request_id"])
        if not row:
            return None
        if row.body_json != canonical(identity):
            raise WorkspaceError("REPORT_REQUEST_CONFLICT")
        return self.receipt(s, row), 200 if row.outcome == "accepted" else 409

    def capture(self, s, company_id, analysis_id):
        try:
            return self._capture(s, company_id, analysis_id)
        except (TypeError, ValueError):
            # Non-finite/malformed stored JSON cannot enter canonical report bytes.
            # Preserve already saved artifacts while refusing a new projection.
            raise WorkspaceError("REPORT_SOURCE_INVALID") from None

    def _capture(self, s, company_id, analysis_id):
        owner, analysis, company = self.owner(s, company_id, analysis_id)
        files = list(s.scalars(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id).order_by(UploadedFile.id)))
        by_file = {f.id: f for f in files}
        facts = {f.id: f for f in s.scalars(select(EmploymentFact).where(EmploymentFact.analysis_id == analysis_id))}
        # Include declarations attached to our facts even if a corrupt row moved
        # its analysis_id; a no-findings draft must not silently omit that source.
        for source in s.scalars(select(SourceLocator).where(or_(SourceLocator.analysis_id == analysis_id, SourceLocator.fact_id.in_(facts)))):
            fact = facts.get(source.fact_id) if source.fact_id else None
            if source.analysis_id != analysis_id or source.file_id not in by_file or not valid_source_metadata(source) or (source.fact_id and (not fact or fact.file_id != source.file_id)):
                raise WorkspaceError("REPORT_SOURCE_INVALID")
        for fact in facts.values():
            employee = s.get(Employee, fact.employee_id) if fact.employee_id else None
            if fact.file_id not in by_file or (fact.employee_id and (not employee or employee.analysis_id != analysis_id)):
                raise WorkspaceError("REPORT_SOURCE_INVALID")
        for parsed in s.scalars(select(ParsedDocument).where(ParsedDocument.file_id.in_(by_file))):
            if parsed.content_hash != by_file[parsed.file_id].sha256:
                raise WorkspaceError("REPORT_SOURCE_INVALID")
        for finding in s.scalars(select(RiskFinding).where(RiskFinding.analysis_id == analysis_id, RiskFinding.is_current.is_(True))):
            if not owned_finding_evidence(s, finding).consistent:
                raise WorkspaceError("REPORT_SOURCE_INVALID")
        payload = ReportService(self.database).get(analysis_id, session=s)
        payload.pop("generated_at")  # Reading or adding an artifact is not an input change.
        # Do not freeze a stale cached batch headcount over the actual captured roster.
        payload["summary"]["employee_count"] = len(payload["employees"])
        payload["company"] = {"id": company.id, "display_name": mask_sensitive(company.display_name), "version": company.version}
        payload["ownership"] = {"company_id": company_id, "analysis_id": analysis_id, "role": owner.role}
        payload["canonical_employees"] = []
        for binding in s.scalars(select(EmployeeSnapshotBinding).where(EmployeeSnapshotBinding.analysis_id == analysis_id).order_by(EmployeeSnapshotBinding.snapshot_id)):
            rec, employee = s.get(EmployeeRecord, binding.employee_record_id), s.get(Employee, binding.snapshot_id)
            if binding.company_id != company_id or not rec or rec.company_id != company_id or not employee or employee.analysis_id != analysis_id:
                raise WorkspaceError("REPORT_SOURCE_INVALID")
            payload["canonical_employees"].append({"snapshot_id": binding.snapshot_id, "record_id": rec.id,
                "masked_name": mask_sensitive(rec.masked_name), "employee_number": mask_sensitive(rec.employee_number or ""),
                "department": mask_sensitive(rec.department or ""), "job_title": mask_sensitive(rec.job_title or ""),
                "lifecycle_status": rec.lifecycle_status, "version": rec.version})
        payload["facts"] = [public_fact(row, True) for row in sorted(effective_projection(s, analysis_id), key=lambda row: row.id)]
        # public_fact's revision value is the original durable value, so mask that nested field too.
        for fact in payload["facts"]:
            if fact["latest_revision"]:
                fact["latest_revision"]["value"] = safe_value(fact["latest_revision"]["value"])
        payload["materials"] = [{"id": f.id, "filename": display_filename(f.original_filename),
            "kind": f.classified_kind or f.detected_kind, "status": f.status, "error_code": f.error_code} for f in files]
        advisory = ContractAdvisoryService(self.database)
        runs = [r for r in s.scalars(select(ContractAdvisoryRun).where(ContractAdvisoryRun.analysis_id == analysis_id).order_by(ContractAdvisoryRun.id)) if advisory._latest(s, r)]
        run_ids = {r.id for r in runs}
        for run in runs:
            if run.file_id not in by_file or run.content_hash != by_file[run.file_id].sha256:
                raise WorkspaceError("REPORT_SOURCE_INVALID")
        payload["advisory_runs"] = [{"id": r.id, "file_id": r.file_id, "execution_status": r.execution_status,
            "contract_version": r.contract_version, "input_statuses": r.input_statuses} for r in runs]
        payload["advisories"] = []
        for row in s.scalars(select(ContractClauseObservation).where(
                ContractClauseObservation.run_id.in_(run_ids)).order_by(ContractClauseObservation.id)):
            location = row.source_location
            if not isinstance(location, dict) or not isinstance(row.source_excerpt, str):
                raise WorkspaceError("REPORT_SOURCE_INVALID")
            if PROOF_KEY in location:
                proof = location[PROOF_KEY]
                if not isinstance(proof, dict) or proof.get("version") != EXTRACTION_VERSION or proof.get("status") not in {
                        "locally_located", "unlocated_needs_review"} or type(proof.get("requires_review", False)) is not bool:
                    raise WorkspaceError("REPORT_SOURCE_INVALID")
            # Existing serializer validates the source digest and ownership after
            # the report boundary validates the shape it is allowed to consume.
            payload["advisories"].append(advisory._observation(s, company_id, analysis_id, row, True))
        revision = payload["assessment_revision"]
        limitations = ["本报告为已保存的复核草稿，不构成无风险保证；模型建议未经核验且不计入确定性风险统计。"]
        if not files or revision["availability"] == "none":
            limitations.append("没有足够可用材料或证据；零条风险不代表安全。")
        if revision["completeness"] != "complete":
            limitations.append("材料、人员匹配或解析仍不完整，结论覆盖受限。")
        if not revision["fresh"] or not revision["result_revision"]:
            limitations.append("当前资料尚无同版本体检结果；须复核并显式重新体检。")
        if any(not f["human_confirmed"] or not f["source_valid"] or f["basis_pending"] for f in payload["facts"]):
            limitations.append("存在待确认事实、当前合同依据或待核对来源。")
        payload["limitations"] = limitations
        payload["report_status"] = "draft"
        payload["dataset_label"] = "合成演示资料" if payload["is_demo"] else "本地资料（不代表已调用真实模型）"
        # Facts and advisories are appended above; protect them before both the
        # deterministic context hash and immutable payload persistence.
        payload = masked_display(payload)
        return payload, {"input_revision": revision["input_revision"], "result_revision": revision["result_revision"],
            "review_revision": revision["report_review_revision"], "context_signature": digest(payload)}

    def current(self, s, company_id, analysis_id):
        try:
            _, context = self.capture(s, company_id, analysis_id)
            return {"current_context_available": True, "current_context": context, "warning": None}
        except WorkspaceError as error:
            if error.status == 404:
                raise
            return {"current_context_available": False, "current_context": None, "warning": "REPORT_SOURCE_INVALID"}

    def listing(self, company_id, analysis_id, page=1, page_size=20):
        with self.database.session() as s:
            s.execute(text("BEGIN"))
            self.owner(s, company_id, analysis_id)
            filters = (ReportVersion.company_id == company_id, ReportVersion.analysis_id == analysis_id)
            total = s.scalar(select(func.count()).select_from(ReportVersion).where(*filters)) or 0
            rows = s.scalars(select(ReportVersion).where(*filters).order_by(ReportVersion.version.desc()).offset((page - 1) * page_size).limit(page_size))
            return {"items": [self.metadata(r) for r in rows], "total": total, "page": page, "page_size": page_size,
                    "pages": (total + page_size - 1) // page_size, **self.current(s, company_id, analysis_id)}

    def detail(self, company_id, analysis_id, version_id):
        with self.database.session() as s:
            s.execute(text("BEGIN"))
            self.owner(s, company_id, analysis_id)
            row = s.get(ReportVersion, version_id)
            if not row or row.company_id != company_id or row.analysis_id != analysis_id:
                raise WorkspaceError("REPORT_NOT_FOUND", 404)
            current = self.current(s, company_id, analysis_id)
            return {"snapshot": self.snapshot(row), **current,
                    "stale": not current["current_context_available"] or row.context_signature != current["current_context"]["context_signature"]}

    def request(self, company_id, analysis_id, request_id):
        with self.database.session() as s:
            s.execute(text("BEGIN"))
            self.owner(s, company_id, analysis_id)
            row = s.get(ReportGenerationRequest, request_id)
            if row and (row.company_id != company_id or row.analysis_id != analysis_id):
                raise WorkspaceError("REPORT_NOT_FOUND", 404)
            return self.receipt(s, row) if row else {"request": None, "snapshot": None}

    def generate(self, company_id, analysis_id, request):
        identity = {"company_id": company_id, "analysis_id": analysis_id, **request.model_dump(mode="json")}
        with self.database.session() as s:
            s.execute(text("BEGIN"))
            self.owner(s, company_id, analysis_id)
            duplicate = self.duplicate(s, identity)
            if duplicate:
                return duplicate
        if self.database.path is not None:
            assert_no_pending_recovery(self.database.path.parent)
        try:
            with self.queue.mutation(analysis_id), self.database.session() as s:
                s.execute(text("BEGIN IMMEDIATE"))
                _, analysis, _ = self.owner(s, company_id, analysis_id)
                duplicate = self.duplicate(s, identity)
                if duplicate:
                    return duplicate
                if analysis.status in {"processing", "queued"} or s.scalar(select(TaskRun.id).where(TaskRun.analysis_id == analysis_id, TaskRun.state.in_(["queued", "running", "cancel_requested"])).limit(1)) or s.scalar(select(ProcessingJob.id).where(ProcessingJob.analysis_id == analysis_id, ProcessingJob.status.in_(["queued", "running"])).limit(1)):
                    raise WorkspaceError("DESKTOP_ANALYSIS_BUSY")
                error_code, saved = None, None
                try:
                    payload, context = self.capture(s, company_id, analysis_id)
                    if any(identity["expected_" + k] != v for k, v in context.items()):
                        error_code = "REPORT_VERSION_CONFLICT"
                except WorkspaceError as error:
                    if error.status == 404:
                        raise
                    error_code = "REPORT_SOURCE_INVALID"
                if not error_code:
                    now = datetime.now(UTC).replace(tzinfo=None)
                    payload["generated_at"] = now.isoformat() + "+00:00"
                    content = canonical(payload)
                    version = (s.scalar(select(func.max(ReportVersion.version)).where(ReportVersion.analysis_id == analysis_id)) or 0) + 1
                    saved = ReportVersion(company_id=company_id, analysis_id=analysis_id, version=version,
                        created_at=now, payload_json=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(), **context)
                    s.add(saved)
                    s.flush()
                receipt = ReportGenerationRequest(id=identity["request_id"], company_id=company_id, analysis_id=analysis_id,
                    body_json=canonical(identity), outcome="rejected" if error_code else "accepted", error_code=error_code,
                    version_id=saved.id if saved else None, content_sha256=saved.content_sha256 if saved else None)
                s.add(receipt)
                s.flush()
                output = self.receipt(s, receipt)
                s.commit()
                return output, 409 if error_code else 201
        except OperationalError:
            raise WorkspaceError("DESKTOP_DB_BUSY") from None
        except RuntimeError as error:
            if str(error) == "DESKTOP_ANALYSIS_BUSY":
                raise WorkspaceError("DESKTOP_ANALYSIS_BUSY") from None
            raise


def report_versions_router(database, queue):
    router = APIRouter(prefix="/api/company-workspaces/{company_id}/analyses/{analysis_id}/report-versions")
    service = ReportVersionService(database, queue)

    @router.get("")
    def listing(company_id: str, analysis_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return service.listing(company_id, analysis_id, page, page_size)

    @router.get("/requests/{request_id}")
    def receipt(company_id: str, analysis_id: str, request_id: UUID):
        return service.request(company_id, analysis_id, str(request_id))

    @router.get("/{version_id}")
    def detail(company_id: str, analysis_id: str, version_id: UUID):
        return service.detail(company_id, analysis_id, str(version_id))

    @router.post("")
    def generate(company_id: str, analysis_id: str, request: GenerateReportRequest):
        payload, status = service.generate(company_id, analysis_id, request)
        return JSONResponse(payload, status_code=status)

    return router
