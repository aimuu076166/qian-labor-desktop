"""Separate unverified model observations; neither facts nor deterministic findings."""
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from qian_labor.ai.grounding import PROOF_KEY
from qian_labor.ai.schemas import ADVISORY_VERSION
from qian_labor.models.core import (
    AnalysisBatch, CompanyAnalysisBinding, Employee, EmployeeRecord, EmployeeSnapshotBinding,
    EmployeeMatchCandidate, EmployeeMatchDecision, UploadedFile, ContractAdvisoryRun,
    ContractClauseObservation, ContractAdvisoryHandling,
)
from qian_labor.security.filenames import display_filename
from qian_labor.security.masking import mask_sensitive
from qian_labor.services.company_workspaces import WorkspaceError
from qian_labor.services.source_provenance import projected_source
from qian_labor.sqlite_migrations import assert_no_pending_recovery


class AdvisoryHandlingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_version: int = Field(ge=0, strict=True)
    decision: Literal["pending", "checking", "addressed", "dismissed"]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank_masked(cls, value):
        value = mask_sensitive(value.strip())
        if not value or len(value) > 500:
            raise ValueError("ADVISORY_REASON_INVALID")
        return value


def source_digest(file_id, location, excerpt):
    return hashlib.sha256(json.dumps([file_id, location, excerpt], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def aggregate_status(statuses, has_warnings=False):
    if not statuses or all(s == "not_executed" for s in statuses):
        return "not_executed"
    if has_warnings:
        return "partial"
    if all(s == "unreadable" for s in statuses):
        return "unreadable"
    if all(s == "not_applicable" for s in statuses):
        return "not_applicable"
    if all(s in {"completed", "not_applicable"} for s in statuses):
        return "completed"
    return "partial"


def persist_run(session, analysis_id, file, key, results, grounded, assignments, *, has_warnings=False):
    existing = session.scalar(select(ContractAdvisoryRun).where(
        ContractAdvisoryRun.analysis_id == analysis_id, ContractAdvisoryRun.file_id == file.id,
        ContractAdvisoryRun.input_key == key))
    if existing:
        return existing
    statuses = [r.contract_advisory.status if r.contract_advisory else "not_executed" for r, _, _ in results]
    run = ContractAdvisoryRun(analysis_id=analysis_id, file_id=file.id, input_key=key,
        content_hash=file.sha256, contract_version=ADVISORY_VERSION, input_statuses=statuses,
        execution_status=aggregate_status(statuses, has_warnings))
    session.add(run)
    session.flush()
    seen = set()
    pending_scopes = {}
    for index, rows in enumerate(grounded):
        employee, candidates = assignments[index]
        result = results[index][0]
        for row in rows:
            number = row.observation.employee_number or result.employee_number
            candidates_for_row = [c for c in candidates if not number or
                number in (c.extracted_fields or {}).get("employee_ids", [])]
            candidate = candidates_for_row[0] if len(candidates_for_row) == 1 else None
            employee_id = employee.id if employee and (not number or employee.employee_number == number) else None
            if employee_id is None and candidate is None:
                # Multiple possible employee targets require one explicit clause scope,
                # never an arbitrary first target or an EmploymentFact placeholder.
                scope = (index, number)
                candidate = pending_scopes.get(scope)
                if candidate is None:
                    candidate = EmployeeMatchCandidate(analysis_id=analysis_id, file_id=file.id,
                        score=0, reason="advisory_identity_confirmation_required", status="pending",
                        extracted_fields={"employee_ids": [mask_sensitive(number)] if number else [], "fact_ids": []})
                    session.add(candidate)
                    session.flush()
                    pending_scopes[scope] = candidate
            if candidate and candidate.status == "confirmed":
                decision = session.scalar(select(EmployeeMatchDecision).where(
                    EmployeeMatchDecision.candidate_id == candidate.id))
                employee_id = decision.target_employee_id if decision else None
            location = row.source.model_dump(exclude={"file_name", "excerpt"}, exclude_none=True)
            location[PROOF_KEY] = row.proof
            excerpt = mask_sensitive(row.source.excerpt)
            original = row.observation.model_dump(exclude={"source"})
            original = {k: [mask_sensitive(v) for v in value] if isinstance(value, list)
                        else mask_sensitive(value) if isinstance(value, str) else value
                        for k, value in original.items()}
            dedupe = hashlib.sha256(json.dumps([original, location, excerpt], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if dedupe in seen:
                continue
            seen.add(dedupe)
            session.add(ContractClauseObservation(run_id=run.id, analysis_id=analysis_id, file_id=file.id,
                employee_id=employee_id, match_candidate_id=candidate.id if candidate else None,
                dedupe_key=dedupe, issue=original["issue"], checks=original["checks"], next_action=original["next_action"],
                unverified_references=original["unverified_references"], source_location=location,
                source_excerpt=excerpt, source_hash=source_digest(file.id, location, excerpt)))
    return run


def _handling(row):
    return None if row is None else {"id": row.id, "observation_id": row.observation_id, "version": row.version,
        "decision": row.decision, "reason": mask_sensitive(row.reason), "actor": "local-user", "created_at": row.created_at.isoformat()}


class ContractAdvisoryService:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _owner(s, company_id, analysis_id):
        owner = s.get(CompanyAnalysisBinding, analysis_id)
        analysis = s.get(AnalysisBatch, analysis_id)
        if not owner or owner.company_id != company_id or not analysis or analysis.deleted_at:
            raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
        return owner

    @staticmethod
    def _validate(s, company_id, analysis_id, row):
        if row is None:
            raise WorkspaceError("ADVISORY_SOURCE_INVALID")
        run, file = s.get(ContractAdvisoryRun, row.run_id), s.get(UploadedFile, row.file_id)
        if not run or not file or any(a != analysis_id for a in (row.analysis_id, run.analysis_id, file.analysis_id)) or run.file_id != file.id or run.content_hash != file.sha256:
            raise WorkspaceError("ADVISORY_SOURCE_INVALID")
        if row.source_hash != source_digest(row.file_id, row.source_location, row.source_excerpt):
            raise WorkspaceError("ADVISORY_SOURCE_INVALID")
        if row.employee_id:
            employee, binding = s.get(Employee, row.employee_id), s.get(EmployeeSnapshotBinding, row.employee_id)
            if not employee or employee.analysis_id != analysis_id or not binding or binding.company_id != company_id or binding.analysis_id != analysis_id:
                raise WorkspaceError("ADVISORY_SOURCE_INVALID")
            record = s.get(EmployeeRecord, binding.employee_record_id)
            if not record or record.company_id != company_id:
                raise WorkspaceError("ADVISORY_SOURCE_INVALID")
        if row.match_candidate_id:
            candidate = s.get(EmployeeMatchCandidate, row.match_candidate_id)
            if not candidate or candidate.analysis_id != analysis_id or candidate.file_id != row.file_id:
                raise WorkspaceError("ADVISORY_SOURCE_INVALID")
        return run, file

    @staticmethod
    def _latest(s, run):
        return s.scalar(select(ContractAdvisoryRun.id).where(ContractAdvisoryRun.analysis_id == run.analysis_id,
            ContractAdvisoryRun.file_id == run.file_id).order_by(ContractAdvisoryRun.created_at.desc(), ContractAdvisoryRun.id.desc()).limit(1)) == run.id

    def _observation(self, s, company_id, analysis_id, row, readonly):
        run, file = self._validate(s, company_id, analysis_id, row)
        latest = self._latest(s, run)
        source = projected_source(SimpleNamespace(location=row.source_location, excerpt=row.source_excerpt, locator_type="document"))
        handling = s.scalar(select(ContractAdvisoryHandling).where(ContractAdvisoryHandling.observation_id == row.id)
                            .order_by(ContractAdvisoryHandling.version.desc()).limit(1))
        candidate = s.get(EmployeeMatchCandidate, row.match_candidate_id) if row.match_candidate_id else None
        return {"id": row.id, "run_id": run.id, "file_id": file.id, "filename": display_filename(file.original_filename),
            "employee_id": row.employee_id, "assignment_status": "assigned" if row.employee_id else
                "unmatched" if candidate and candidate.status == "unmatched" else "pending_matching",
            "match_candidate_id": row.match_candidate_id, "issue": mask_sensitive(row.issue),
            "checks": [mask_sensitive(x) for x in row.checks], "next_action": mask_sensitive(row.next_action),
            "unverified_references": [mask_sensitive(x) for x in row.unverified_references],
            "source": source, "requires_source_review": row.source_location.get(PROOF_KEY, {}).get("requires_review", True),
            "legal_verification": "unverified", "version": row.version, "handling": _handling(handling),
            "is_latest": latest, "read_only": readonly or not latest}

    def listing(self, company_id, analysis_id, *, file_id=None, record_id=None, request_id=None, page=1, page_size=20, history=False):
        with self.database.session() as s:
            owner = self._owner(s, company_id, analysis_id)
            filters = [ContractClauseObservation.analysis_id == analysis_id]
            run_filters = [ContractAdvisoryRun.analysis_id == analysis_id]
            if file_id:
                file = s.get(UploadedFile, file_id)
                if not file or file.analysis_id != analysis_id:
                    raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
                filters.append(ContractClauseObservation.file_id == file_id)
                run_filters.append(ContractAdvisoryRun.file_id == file_id)
            if record_id:
                rec = s.get(EmployeeRecord, record_id)
                if not rec or rec.company_id != company_id:
                    raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
                snapshot_ids = select(EmployeeSnapshotBinding.snapshot_id).where(EmployeeSnapshotBinding.company_id == company_id,
                    EmployeeSnapshotBinding.analysis_id == analysis_id, EmployeeSnapshotBinding.employee_record_id == record_id)
                filters.append(ContractClauseObservation.employee_id.in_(snapshot_ids))
                run_filters.append(ContractAdvisoryRun.id.in_(select(ContractClauseObservation.run_id).where(*filters)))
            if not history:
                from sqlalchemy.orm import aliased
                newer = aliased(ContractAdvisoryRun)
                latest_ids = select(ContractAdvisoryRun.id).where(~select(newer.id).where(
                    newer.analysis_id == ContractAdvisoryRun.analysis_id, newer.file_id == ContractAdvisoryRun.file_id,
                    (newer.created_at > ContractAdvisoryRun.created_at) | ((newer.created_at == ContractAdvisoryRun.created_at) & (newer.id > ContractAdvisoryRun.id))).exists())
                filters.append(ContractClauseObservation.run_id.in_(latest_ids))
                run_filters.append(ContractAdvisoryRun.id.in_(latest_ids))
            total = s.scalar(select(func.count()).select_from(ContractClauseObservation).where(*filters)) or 0
            rows = list(s.scalars(select(ContractClauseObservation).where(*filters).order_by(
                ContractClauseObservation.created_at.desc(), ContractClauseObservation.id).offset((page - 1) * page_size).limit(page_size)))
            runs = list(s.scalars(select(ContractAdvisoryRun).where(*run_filters).order_by(
                ContractAdvisoryRun.created_at.desc(), ContractAdvisoryRun.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            run_total = s.scalar(select(func.count()).select_from(ContractAdvisoryRun).where(*run_filters)) or 0
            run_payloads = []
            for run in runs:
                file = s.get(UploadedFile, run.file_id)
                if not file or file.analysis_id != analysis_id or run.content_hash != file.sha256:
                    raise WorkspaceError("ADVISORY_SOURCE_INVALID")
                run_payloads.append({"id": run.id, "file_id": run.file_id, "filename": display_filename(file.original_filename),
                    "contract_version": run.contract_version, "execution_status": run.execution_status,
                    "input_count": len(run.input_statuses), "completed_input_count": run.input_statuses.count("completed"),
                    "is_latest": self._latest(s, run), "created_at": run.created_at.isoformat()})
            reconciled = s.get(ContractAdvisoryHandling, str(request_id)) if request_id else None
            if reconciled:
                row = s.get(ContractClauseObservation, reconciled.observation_id)
                self._validate(s, company_id, analysis_id, row)
                if file_id and row.file_id != file_id:
                    raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
                if record_id:
                    binding = s.get(EmployeeSnapshotBinding, row.employee_id) if row.employee_id else None
                    if not binding or binding.employee_record_id != record_id:
                        raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
            return {"runs": run_payloads, "observations": [self._observation(s, company_id, analysis_id, r, owner.role != "current") for r in rows],
                "total": total, "run_total": run_total, "page": page, "page_size": page_size,
                "pages": (max(total, run_total) + page_size - 1) // page_size, "read_only": owner.role != "current",
                "request_handling": _handling(reconciled), "legal_verification": "unverified"}

    def handle(self, company_id, analysis_id, observation_id, request):
        if self.database.path:
            assert_no_pending_recovery(self.database.path.parent)
        try:
            with self.database.session() as s:
                s.execute(text("BEGIN IMMEDIATE"))
                owner = self._owner(s, company_id, analysis_id)
                if owner.role != "current":
                    raise WorkspaceError("WORKSPACE_HISTORICAL_READ_ONLY")
                row = s.get(ContractClauseObservation, observation_id)
                if not row or row.analysis_id != analysis_id:
                    raise WorkspaceError("ADVISORY_NOT_FOUND", 404)
                run, _ = self._validate(s, company_id, analysis_id, row)
                if not self._latest(s, run):
                    raise WorkspaceError("ADVISORY_HISTORICAL_READ_ONLY")
                if s.get(ContractAdvisoryHandling, str(request.id)) or s.execute(update(ContractClauseObservation).where(
                    ContractClauseObservation.id == row.id, ContractClauseObservation.version == request.expected_version)
                    .values(version=ContractClauseObservation.version + 1)).rowcount != 1:
                    raise WorkspaceError("ADVISORY_VERSION_CONFLICT")
                s.add(ContractAdvisoryHandling(id=str(request.id), observation_id=row.id,
                    version=request.expected_version + 1, decision=request.decision, reason=request.reason))
                s.flush()
                output = self._observation(s, company_id, analysis_id, row, False)
                s.commit()
                return output
        except IntegrityError:
            raise WorkspaceError("ADVISORY_VERSION_CONFLICT") from None
