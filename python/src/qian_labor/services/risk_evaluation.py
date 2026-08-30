from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qian_labor.database import Database
from qian_labor.models.core import (
    AnalysisBatch,
    Employee,
    EmployeeMatchCandidate,
    EmploymentFact,
    RiskFinding,
    SourceLocator,
)
from qian_labor.rules.engine import evaluate_rules
from qian_labor.rules.registry import RULE_REGISTRY
from qian_labor.rules.types import FactValue, RuleContext


class RiskEvaluationError(RuntimeError):
    pass


class RiskEvaluationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate_data_quality(
        self, analysis_id: str, *, db_session: Session | None = None
    ) -> tuple[RiskFinding, ...]:
        return self.evaluate_analysis(analysis_id, data_quality_only=True, db_session=db_session)

    def evaluate_analysis(
        self,
        analysis_id: str,
        *,
        data_quality_only: bool = False,
        db_session: Session | None = None,
    ) -> tuple[RiskFinding, ...]:
        session_context = (
            nullcontext(db_session) if db_session is not None else self.database.session()
        )
        with session_context as session:
            persisted = self._evaluate(session, analysis_id, data_quality_only)
            if db_session is None:
                session.commit()
            return persisted

    def _evaluate(
        self, session: Session, analysis_id: str, data_quality_only: bool
    ) -> tuple[RiskFinding, ...]:
        analysis = session.get(AnalysisBatch, analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        has_pending_matches = bool(
            session.scalar(
                select(EmployeeMatchCandidate.id).where(
                    EmployeeMatchCandidate.analysis_id == analysis_id,
                    EmployeeMatchCandidate.status == "pending",
                )
            )
        )
        assessment_gated = data_quality_only or has_pending_matches
        if assessment_gated:
            analysis.status = "matching_review"
            analysis.current_stage = "matching_review"
            analysis.progress = min(analysis.progress or 90, 90)
            session.execute(
                delete(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.category != "data_quality",
                )
            )
        else:
            analysis.status = "evaluating"
            analysis.current_stage = "evaluating"
        employees = list(
            session.scalars(
                select(Employee)
                .where(Employee.analysis_id == analysis_id)
                .order_by(Employee.employee_number)
            )
        )
        persisted: list[RiskFinding] = []
        coverage_values: list[float] = []
        for employee in employees:
            facts = list(
                session.scalars(
                    select(EmploymentFact).where(
                        EmploymentFact.analysis_id == analysis_id,
                        EmploymentFact.employee_id == employee.id,
                    )
                )
            )
            if not facts:
                self._delete_stale_findings(
                    session,
                    analysis_id,
                    employee.id,
                    set(),
                    data_quality_only=assessment_gated,
                )
                continue
            context_facts = self._context_facts(session, facts)
            identity = context_facts.get("employment.identity.match_status")
            if facts:
                fallback = identity or next(iter(context_facts.values()))
                context_facts["employment.identity.match_status"] = FactValue(
                    id=identity.id if identity else facts[0].id,
                    value=employee.match_status,
                    source_locator_ids=fallback.source_locator_ids,
                )
            coverage = context_facts.get("employment.material_coverage")
            if coverage:
                coverage_values.append(float(coverage.value))
            context = RuleContext(
                analysis_date=self._analysis_date(analysis),
                employee_id=employee.id,
                facts=context_facts,
            )
            results = evaluate_rules(
                context,
                category="data_quality" if assessment_gated else None,
            )
            triggered_rule_ids: set[str] = set()
            for result in results:
                if not result.triggered:
                    continue
                trigger_fact_ids = list(result.trigger_fact_ids)
                source_locator_ids = list(result.source_locator_ids)
                if not source_locator_ids and facts:
                    fallback_sources = tuple(
                        session.scalars(
                            select(SourceLocator.id).where(SourceLocator.fact_id == facts[0].id)
                        )
                    )
                    trigger_fact_ids = [facts[0].id]
                    source_locator_ids = list(fallback_sources)
                if not source_locator_ids:
                    analysis.status = "failed"
                    analysis.failure_reason = "FINDING_SOURCE_REQUIRED"
                    raise RiskEvaluationError("FINDING_SOURCE_REQUIRED")
                metadata = next(
                    rule.metadata
                    for rule in RULE_REGISTRY.values()
                    if rule.metadata.rule_id == result.rule_id
                )
                basis_type = result.basis_type or metadata.basis_type
                legal_sources = (
                    metadata.legal_source if result.legal_source is None else result.legal_source
                )
                management_parameters = (
                    metadata.management_parameters
                    if result.management_parameters is None
                    else result.management_parameters
                )
                basis_metadata = {
                    "basis_type": basis_type,
                    "legal_sources": list(legal_sources),
                    "references": list(legal_sources),
                    "management_parameters": [
                        asdict(parameter) for parameter in management_parameters
                    ],
                    "effective_date": metadata.effective_date,
                    "last_verified_at": metadata.last_verified_at,
                }
                session.execute(
                    delete(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.employee_id == employee.id,
                        RiskFinding.rule_id == result.rule_id,
                        RiskFinding.rule_version != result.rule_version,
                    )
                )
                existing = session.scalar(
                    select(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.employee_id == employee.id,
                        RiskFinding.rule_id == result.rule_id,
                        RiskFinding.rule_version == result.rule_version,
                    )
                )
                if existing is None:
                    existing = RiskFinding(
                        analysis_id=analysis_id,
                        employee_id=employee.id,
                        rule_id=result.rule_id,
                        rule_version=result.rule_version,
                        category=metadata.category,
                        severity=result.severity,
                        assessment_status=result.assessment_status,
                        title=metadata.name,
                        summary=str(result.message_params["finding_phrase"]),
                        trigger_fact_ids=trigger_fact_ids,
                        source_locator_ids=source_locator_ids,
                        missing_fact_types=list(result.missing_fact_types),
                        legal_basis=basis_metadata,
                        recommended_actions=[metadata.recommended_action],
                        requires_human_review=result.requires_human_review,
                    )
                    session.add(existing)
                    session.flush()
                else:
                    existing.category = metadata.category
                    existing.severity = result.severity
                    existing.assessment_status = result.assessment_status
                    existing.title = metadata.name
                    existing.summary = str(result.message_params["finding_phrase"])
                    existing.trigger_fact_ids = trigger_fact_ids
                    existing.source_locator_ids = source_locator_ids
                    existing.missing_fact_types = list(result.missing_fact_types)
                    existing.legal_basis = basis_metadata
                    existing.recommended_actions = [metadata.recommended_action]
                    existing.requires_human_review = result.requires_human_review
                triggered_rule_ids.add(result.rule_id)
                persisted.append(existing)
            self._delete_stale_findings(
                session,
                analysis_id,
                employee.id,
                triggered_rule_ids,
                data_quality_only=assessment_gated,
            )

        self._update_aggregates(session, analysis, coverage_values)
        if assessment_gated:
            analysis.status = "matching_review"
            analysis.current_stage = "matching_review"
            analysis.progress = 90
        else:
            analysis.status = "completed"
            analysis.current_stage = "completed"
            analysis.progress = 100
        return tuple(persisted)

    @staticmethod
    def _delete_stale_findings(
        session: Session,
        analysis_id: str,
        employee_id: str,
        triggered_rule_ids: set[str],
        *,
        data_quality_only: bool,
    ) -> None:
        managed_rule_ids = {
            rule.metadata.rule_id
            for rule in RULE_REGISTRY.values()
            if not data_quality_only or rule.metadata.category == "data_quality"
        }
        stale_rule_ids = managed_rule_ids - triggered_rule_ids
        if stale_rule_ids:
            session.execute(
                delete(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.employee_id == employee_id,
                    RiskFinding.rule_id.in_(stale_rule_ids),
                )
            )

    @staticmethod
    def _context_facts(session: Any, facts: list[EmploymentFact]) -> dict[str, FactValue]:
        result: dict[str, FactValue] = {}
        for fact in facts:
            source_ids = tuple(
                session.scalars(select(SourceLocator.id).where(SourceLocator.fact_id == fact.id))
            )
            result[fact.fact_type] = FactValue(
                id=fact.id,
                value=fact.normalized_value_json,
                source_locator_ids=source_ids,
                conflicted=fact.verification_status == "conflicted",
            )
        return result

    @staticmethod
    def _analysis_date(analysis: AnalysisBatch) -> date:
        return analysis.created_at.date()

    @staticmethod
    def _update_aggregates(
        session: Any, analysis: AnalysisBatch, coverage_values: list[float]
    ) -> None:
        findings = list(
            session.scalars(select(RiskFinding).where(RiskFinding.analysis_id == analysis.id))
        )
        analysis.high_count = sum(
            item.severity == "high" and item.assessment_status != "insufficient_data"
            for item in findings
        )
        analysis.medium_count = sum(
            item.severity == "medium" and item.assessment_status != "insufficient_data"
            for item in findings
        )
        analysis.low_count = sum(
            item.severity == "low" and item.assessment_status != "insufficient_data"
            for item in findings
        )
        analysis.insufficient_data_count = sum(
            item.assessment_status == "insufficient_data" for item in findings
        )
        analysis.coverage_rate = (
            round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else 0
        )
        analysis.employee_count = int(
            session.scalar(
                select(func.count())
                .select_from(Employee)
                .where(Employee.analysis_id == analysis.id)
            )
            or 0
        )
