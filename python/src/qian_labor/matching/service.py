from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
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
)
from qian_labor.security.masking import mask_identity
from qian_labor.services.risk_evaluation import RiskEvaluationService


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
                            uploaded_file.original_filename if uploaded_file else None
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
        with self.database.session() as session:
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
                target = Employee(
                    analysis_id=analysis_id,
                    masked_name=mask_identity(payload.display_name or "未识别人员"),
                    normalized_name=mask_identity(payload.display_name or "未识别人员"),
                    employee_number=None,
                    match_status="confirmed",
                )
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

            if candidate:
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
            return {
                "id": decision.id,
                "analysis_id": analysis_id,
                "decision": decision.decision,
                "target_employee_id": target_employee_id,
                "status": "confirmed",
                "analysis_status": analysis.status,
            }

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
