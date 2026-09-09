from __future__ import annotations
from qian_labor.security.filenames import display_filename

from typing import Any

from sqlalchemy import func, select, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qian_labor.database import Database
from qian_labor.matching.scoring import AMBIGUITY_MARGIN, AUTO_MATCH_THRESHOLD, score_candidate
from qian_labor.matching.types import CandidateIdentity, RankedMatch
from qian_labor.models.core import (
    AnalysisBatch,
    AuditEvent,
    Employee,
    EmployeeMatchCandidate,
    EmployeeMatchDecision,
    EmploymentFact,
    UploadedFile,
    CompanyWorkspace, CompanyAnalysisBinding, EmployeeRecord, EmployeeSnapshotBinding,
)
from qian_labor.security.masking import mask_identity, mask_sensitive
from qian_labor.services.risk_evaluation import RiskEvaluationService
from qian_labor.services.company_workspaces import require_material_mutation, WorkspaceError
from qian_labor.desktop.company_schemas import EmployeeView
from qian_labor.sqlite_migrations import assert_no_pending_recovery


class MatchDecisionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def rank_candidates(
    source: CandidateIdentity, candidates: list[tuple[str, CandidateIdentity]]
) -> RankedMatch:
    ranked = sorted(
        (
            (employee_id, score_candidate(source, candidate))
            for employee_id, candidate in candidates
        ),
        key=lambda item: item[1].score,
        reverse=True,
    )
    if not ranked:
        return RankedMatch("unknown", None, 0, (), ())
    top_id, top_score = ranked[0]
    close = len(ranked) > 1 and top_score.score - ranked[1][1].score < AMBIGUITY_MARGIN
    conflict = any(score.stable_identifier_conflict for _, score in ranked)
    if conflict or close:
        return RankedMatch("ambiguous", None, top_score.score, top_score.reasons, tuple(ranked[:3]))
    if top_score.score >= AUTO_MATCH_THRESHOLD:
        return RankedMatch(
            "auto_matched", top_id, top_score.score, top_score.reasons, tuple(ranked[:3])
        )
    return RankedMatch("unknown", None, top_score.score, top_score.reasons, tuple(ranked[:3]))


def match_employee(candidates: list[dict[str, Any]], record: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper used by the deterministic matching unit tests."""

    source = CandidateIdentity(
        name=record.get("name", ""),
        employee_number=record.get("employee_no"),
        id_number_hash=record.get("id_hash"),
        phone_hash=record.get("phone_hash"),
        bank_card_hash=record.get("bank_card_hash"),
        department=record.get("department"),
        hire_date=record.get("hire_date"),
    )
    indexed = [
        (
            str(candidate.get("id") or candidate.get("employee_no") or index),
            CandidateIdentity(
                name=candidate.get("name", ""),
                employee_number=candidate.get("employee_no"),
                id_number_hash=candidate.get("id_hash"),
                phone_hash=candidate.get("phone_hash"),
                bank_card_hash=candidate.get("bank_card_hash"),
                department=candidate.get("department"),
                hire_date=candidate.get("hire_date"),
            ),
        )
        for index, candidate in enumerate(candidates)
    ]
    result = rank_candidates(source, indexed)
    payload: dict[str, Any] = {
        "status": result.status,
        "score": result.score,
        "reasons": result.reasons,
        "candidates": [item for _, item in zip(result.candidates, candidates, strict=False)],
    }
    if result.employee_id:
        payload["employee"] = next(
            candidate
            for employee_id, candidate in zip(
                (item[0] for item in indexed), candidates, strict=False
            )
            if employee_id == result.employee_id
        )
    return payload


class EmployeeMatcher:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record_options(self, analysis_id: str):
        with self.database.session() as s:
            owner = s.get(CompanyAnalysisBinding, analysis_id)
            if owner is None or owner.role != "current":
                return {}
            return {"current_company_id": owner.company_id, "employee_record_options": [
                EmployeeView.model_validate(r).model_dump() for r in s.scalars(select(EmployeeRecord).where(
                    EmployeeRecord.company_id == owner.company_id, EmployeeRecord.lifecycle_status == "active"
                ).order_by(EmployeeRecord.masked_name, EmployeeRecord.id))]}

    @staticmethod
    def _selected_record(s, owner, payload):
        rid = getattr(payload, "employee_record_id", None)
        version = getattr(payload, "expected_record_version", None)
        if (rid is None) != (version is None):
            raise WorkspaceError("WORKSPACE_RECORD_SELECTION_REQUIRED")
        if rid is None:
            return None
        if payload.decision == "unmatched":
            raise WorkspaceError("WORKSPACE_RECORD_SELECTION_FORBIDDEN")
        if owner is None:
            raise WorkspaceError("WORKSPACE_RECORD_SELECTION_FORBIDDEN")
        record = s.get(EmployeeRecord, rid)
        if record is None or record.company_id != owner.company_id:
            raise WorkspaceError("WORKSPACE_CROSS_COMPANY_FORBIDDEN")
        if record.version != version:
            raise WorkspaceError("WORKSPACE_EMPLOYEE_VERSION_CONFLICT")
        if record.lifecycle_status != "active":
            raise WorkspaceError("WORKSPACE_EMPLOYEE_ARCHIVED")
        return record

    @staticmethod
    def _check_record_number(record, number):
        if record.employee_number and number and record.employee_number != number:
            raise WorkspaceError("WORKSPACE_EMPLOYEE_NUMBER_MISMATCH")

    def _bind_current(self, s, owner, target, selected, payload, source_numbers):
        if target.employee_number and mask_sensitive(target.employee_number) != target.employee_number:
            raise WorkspaceError("WORKSPACE_IDENTIFIER_INVALID")
        if target.employment_status == "merged":
            raise WorkspaceError("WORKSPACE_SNAPSHOT_IDENTITY_CONFLICT")
        binding = s.get(EmployeeSnapshotBinding, target.id)
        if len(source_numbers) == 1 and isinstance(source_numbers[0], str) and source_numbers[0].strip():
            source_number = source_numbers[0].strip()
            self._check_record_number(target, source_number)
            if selected is not None:
                self._check_record_number(selected, source_number)
            canonical_id = s.scalar(select(EmployeeRecord.id).where(
                EmployeeRecord.company_id == owner.company_id,
                EmployeeRecord.employee_number == source_number))
            intended_id = selected.id if selected is not None else binding.employee_record_id if binding else None
            if canonical_id is not None and canonical_id != intended_id:
                raise WorkspaceError("WORKSPACE_EMPLOYEE_NUMBER_EXISTS")
        if binding:
            if selected is not None and binding.employee_record_id != selected.id:
                raise WorkspaceError("WORKSPACE_SNAPSHOT_IDENTITY_CONFLICT")
            record = s.get(EmployeeRecord, binding.employee_record_id)
            self._check_record_number(record, target.employee_number)
            return record
        if selected is None:
            if payload.decision != "create_unknown":
                raise WorkspaceError("WORKSPACE_RECORD_SELECTION_REQUIRED")
            if target.employee_number and s.scalar(select(EmployeeRecord.id).where(
                EmployeeRecord.company_id == owner.company_id, EmployeeRecord.employee_number == target.employee_number)):
                raise WorkspaceError("WORKSPACE_EMPLOYEE_NUMBER_EXISTS")
            selected = EmployeeRecord(company_id=owner.company_id, masked_name=target.masked_name,
                employee_number=target.employee_number, department=target.department, job_title=target.job_title)
            s.add(selected)
            s.flush()
        else:
            self._check_record_number(selected, target.employee_number)
            other = s.scalar(select(EmployeeSnapshotBinding).where(
                EmployeeSnapshotBinding.analysis_id == owner.analysis_id,
                EmployeeSnapshotBinding.employee_record_id == selected.id))
            if other:
                raise WorkspaceError("WORKSPACE_RECORD_ALREADY_BOUND")
            selected.version += 1
        s.add(EmployeeSnapshotBinding(snapshot_id=target.id, analysis_id=owner.analysis_id,
            company_id=owner.company_id, employee_record_id=selected.id))
        s.get(CompanyWorkspace, owner.company_id).version += 1
        return selected

    def list_candidates(self, analysis_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            if session.get(AnalysisBatch, analysis_id) is None:
                raise KeyError(analysis_id)
            candidates = list(
                session.scalars(
                    select(EmployeeMatchCandidate)
                    .where(
                        EmployeeMatchCandidate.analysis_id == analysis_id,
                        EmployeeMatchCandidate.status == "pending",
                    )
                    .order_by(EmployeeMatchCandidate.score.desc())
                )
            )
            employees = list(
                session.scalars(
                    select(Employee)
                    .where(
                        Employee.analysis_id == analysis_id,
                        Employee.employment_status != "merged",
                    )
                    .order_by(Employee.masked_name, Employee.employee_number)
                )
            )
            employee_options = [
                {
                    "employee_id": item.id,
                    "employee_name": item.masked_name,
                    "employee_number": item.employee_number,
                    "department": item.department,
                }
                for item in employees
            ]
            result = []
            for candidate in candidates:
                employee = (
                    session.get(Employee, candidate.candidate_employee_id)
                    if candidate.candidate_employee_id
                    else None
                )
                uploaded_file = (
                    session.get(UploadedFile, candidate.file_id) if candidate.file_id else None
                )
                result.append(
                    {
                        "id": candidate.id,
                        "file_id": candidate.file_id,
                        "material_name": (
                            display_filename(uploaded_file.original_filename) if uploaded_file else None
                        ),
                        "employee_id": candidate.candidate_employee_id,
                        "employee_name": employee.masked_name if employee else "未识别人员",
                        "employee_number": employee.employee_number if employee else None,
                        "extracted_fields": candidate.extracted_fields,
                        "fact_ids": self._candidate_fact_ids(session, analysis_id, candidate),
                        "score": candidate.score,
                        "reasons": candidate.reason.split(",") if candidate.reason else [],
                        "status": candidate.status,
                        "employee_options": employee_options,
                    }
                )
            return result

    def decide(self, analysis_id: str, payload: Any) -> dict[str, Any]:
        try:
            return self._decide(analysis_id, payload)
        except IntegrityError as error:
            raise MatchDecisionError("MATCH_DECISION_CONFLICT") from error

    def _decide(self, analysis_id: str, payload: Any) -> dict[str, Any]:
        candidate_required = True
        if self.database.path is not None:
            assert_no_pending_recovery(self.database.path.parent)
        with self.database.session() as session:
            if self.database.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            owner = require_material_mutation(session, analysis_id)
            selected = self._selected_record(session, owner, payload)
            analysis = session.scalar(
                select(AnalysisBatch).where(AnalysisBatch.id == analysis_id).with_for_update()
            )
            if analysis is None:
                raise KeyError(analysis_id)
            candidate = None
            if payload.candidate_id:
                candidate = session.scalar(
                    select(EmployeeMatchCandidate)
                    .where(EmployeeMatchCandidate.id == payload.candidate_id)
                    .with_for_update()
                )
                if (
                    candidate is None
                    or candidate.analysis_id != analysis_id
                    or candidate.status != "pending"
                ):
                    raise MatchDecisionError("MATCH_DECISION_STALE")
                if session.scalar(
                    select(EmployeeMatchDecision.id).where(
                        EmployeeMatchDecision.candidate_id == candidate.id
                    )
                ):
                    raise MatchDecisionError("MATCH_DECISION_CONFLICT")
            if analysis.status != "matching_review":
                raise MatchDecisionError("MATCH_ANALYSIS_NOT_REVIEW")
            if candidate_required and not payload.candidate_id:
                raise MatchDecisionError("MATCH_CANDIDATE_REQUIRED")

            candidate_facts = (
                self._candidate_facts(session, analysis_id, candidate, payload.fact_ids)
                if candidate_required and candidate is not None
                else []
            )
            target_employee_id: str | None = None
            if payload.decision == "assign":
                if not payload.employee_id:
                    raise MatchDecisionError("MATCH_DECISION_INVALID")
                target = session.get(Employee, payload.employee_id)
                if target is None or target.analysis_id != analysis_id:
                    raise MatchDecisionError("MATCH_CROSS_ANALYSIS_FORBIDDEN")
                self._assign_facts(candidate_facts, target.id)
                target.match_status = "confirmed"
                target_employee_id = target.id
            elif payload.decision == "create_unknown":
                employee_number = (getattr(payload, "employee_number", None) or "").strip() or None
                if owner and selected:
                    self._check_record_number(selected, employee_number)
                    employee_number = employee_number or selected.employee_number
                existing_binding = session.scalar(select(EmployeeSnapshotBinding).where(
                    EmployeeSnapshotBinding.analysis_id == analysis_id,
                    EmployeeSnapshotBinding.employee_record_id == selected.id)) if selected else None
                if not existing_binding and employee_number and session.scalar(
                    select(Employee.id).where(
                        Employee.analysis_id == analysis_id,
                        Employee.employee_number == employee_number,
                    )
                ):
                    raise MatchDecisionError("MATCH_EMPLOYEE_NUMBER_EXISTS")
                target = session.get(Employee, existing_binding.snapshot_id) if existing_binding else Employee(
                    analysis_id=analysis_id,
                    masked_name=mask_identity(payload.display_name or "未识别人员"),
                    normalized_name=mask_identity(payload.display_name or "未识别人员"),
                    employee_number=employee_number,
                    match_status="confirmed",
                )
                target.match_status = "confirmed"
                session.add(target)
                session.flush()
                self._assign_facts(candidate_facts, target.id)
                target_employee_id = target.id
            elif payload.decision == "merge":
                source = session.get(Employee, payload.source_employee_id)
                target = session.get(Employee, payload.target_employee_id)
                if (
                    source is None
                    or target is None
                    or source.analysis_id != analysis_id
                    or target.analysis_id != analysis_id
                    or source.id == target.id
                ):
                    raise MatchDecisionError("MATCH_CROSS_ANALYSIS_FORBIDDEN")
                if candidate is None or candidate.candidate_employee_id != source.id:
                    raise MatchDecisionError("MATCH_MERGE_SOURCE_MISMATCH")
                if owner and session.get(EmployeeSnapshotBinding, source.id):
                    raise WorkspaceError("WORKSPACE_BOUND_EMPLOYEE_MERGE_FORBIDDEN")
                session.execute(
                    update(EmploymentFact)
                    .where(EmploymentFact.employee_id == source.id)
                    .values(employee_id=target.id)
                )
                self._assign_facts(candidate_facts, target.id)
                source.employment_status = "merged"
                source.match_status = "confirmed"
                target.match_status = "confirmed"
                target_employee_id = target.id
            elif payload.decision == "unmatched":
                self._assign_facts(candidate_facts, None)
            else:
                raise MatchDecisionError("MATCH_DECISION_INVALID")

            record = None
            if owner and target_employee_id:
                source_numbers = candidate.extracted_fields.get("employee_ids", []) if candidate else []
                record = self._bind_current(session, owner, target, selected, payload, source_numbers)
                self._check_record_number(target, (getattr(payload, "employee_number", None) or "").strip() or None)
                if len(source_numbers) == 1:
                    self._check_record_number(record, source_numbers[0])
                    self._check_record_number(target, source_numbers[0])
                analysis.version += 1

            if candidate:
                from qian_labor.models.core import ContractClauseObservation
                session.execute(update(ContractClauseObservation).where(
                    ContractClauseObservation.match_candidate_id == candidate.id,
                    ContractClauseObservation.analysis_id == analysis_id,
                    ContractClauseObservation.file_id == candidate.file_id,
                ).values(employee_id=target_employee_id))
                self._supersede_fact_scope_alternatives(session, candidate)
                candidate.status = "unmatched" if payload.decision == "unmatched" else "confirmed"
            decision = EmployeeMatchDecision(
                analysis_id=analysis_id,
                candidate_id=candidate.id if candidate else None,
                decision=payload.decision,
                target_employee_id=target_employee_id,
                corrected_fields={},
            )
            session.add(decision)
            session.add(
                AuditEvent(
                    analysis_id=analysis_id,
                    event_type="match_decision",
                    actor="competition-user",
                    metadata_json={
                        "decision": payload.decision,
                        "candidate_id": payload.candidate_id,
                    },
                )
            )
            session.flush()
            unresolved = int(
                session.scalar(
                    select(func.count())
                    .select_from(EmployeeMatchCandidate)
                    .where(
                        EmployeeMatchCandidate.analysis_id == analysis_id,
                        EmployeeMatchCandidate.status == "pending",
                    )
                )
                or 0
            )
            if unresolved == 0:
                analysis.status = "evaluating"
                analysis.current_stage = "evaluating"
                RiskEvaluationService(self.database).evaluate_analysis(
                    analysis_id, db_session=session
                )
            session.commit()
            result = {
                "id": decision.id,
                "analysis_id": analysis_id,
                "decision": decision.decision,
                "target_employee_id": target_employee_id,
                "status": "confirmed",
                "analysis_status": analysis.status,
            }
            if owner:
                result["employee_record_id"] = record.id if record else None
            return result

    @staticmethod
    def _candidate_facts(
        session: Session,
        analysis_id: str,
        candidate: EmployeeMatchCandidate,
        requested_fact_ids: list[str],
    ) -> list[EmploymentFact]:
        scoped_ids = EmployeeMatcher._candidate_fact_ids(session, analysis_id, candidate)
        facts = list(
            session.scalars(
                select(EmploymentFact).where(
                    EmploymentFact.analysis_id == analysis_id,
                    EmploymentFact.file_id == candidate.file_id,
                    EmploymentFact.id.in_(scoped_ids),
                )
            )
        )
        derived_ids = {fact.id for fact in facts}
        if derived_ids != set(scoped_ids) or (
            requested_fact_ids and set(requested_fact_ids) != derived_ids
        ):
            raise MatchDecisionError("MATCH_FACT_SCOPE_INVALID")
        return facts

    @staticmethod
    def _candidate_fact_ids(
        session: Session, analysis_id: str, candidate: EmployeeMatchCandidate
    ) -> list[str]:
        stored_scope = (candidate.extracted_fields or {}).get("fact_ids")
        if isinstance(stored_scope, list) and all(
            isinstance(fact_id, str) for fact_id in stored_scope
        ):
            return list(dict.fromkeys(stored_scope))
        raise MatchDecisionError("MATCH_FACT_SCOPE_MISSING")

    @staticmethod
    def _assign_facts(facts: list[EmploymentFact], employee_id: str | None) -> None:
        for fact in facts:
            fact.employee_id = employee_id

    @staticmethod
    def _supersede_fact_scope_alternatives(
        session: Session, candidate: EmployeeMatchCandidate
    ) -> None:
        selected_scope = (candidate.extracted_fields or {}).get("fact_ids")
        if not isinstance(selected_scope, list):
            return
        selected_ids = set(selected_scope)
        # Empty fact scopes can represent different clause-only employee groups.
        # They are not equivalent evidence and must remain independently matchable.
        if not selected_ids:
            return
        alternatives = session.scalars(
            select(EmployeeMatchCandidate).where(
                EmployeeMatchCandidate.analysis_id == candidate.analysis_id,
                EmployeeMatchCandidate.file_id == candidate.file_id,
                EmployeeMatchCandidate.status == "pending",
                EmployeeMatchCandidate.id != candidate.id,
            )
        )
        for alternative in alternatives:
            alternative_scope = (alternative.extracted_fields or {}).get("fact_ids")
            if isinstance(alternative_scope, list) and set(alternative_scope) == selected_ids:
                alternative.status = "superseded"
