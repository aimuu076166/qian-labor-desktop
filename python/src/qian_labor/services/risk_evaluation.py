from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qian_labor.database import Database
from qian_labor.models.core import (
    AnalysisBatch,
    CompanyAnalysisBinding,
    Employee,
    EmployeeMatchCandidate,
    EmploymentFact,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)
from qian_labor.rules.engine import evaluate_rules
from qian_labor.rules.registry import RULE_REGISTRY
from qian_labor.rules.types import FactValue, RuleContext
from qian_labor.services.finding_review import (
    latest_signature, sync_evaluation_review, update_risk_counts,
)
from qian_labor.services.finding_lifecycle import retire_findings
from qian_labor.services.assessment_scope import DESKTOP_PROFILE, EXCLUDED_CODES, validate_profile, scoped_evidence_rows
from qian_labor.services.dashboard import DashboardService
from qian_labor.services.assessment_scope import coverage_evidence


class RiskEvaluationError(RuntimeError):
    pass


def _json_value_key(value: Any) -> tuple:
    """Compare JSON numbers exactly, preserving every other JSON type boundary."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) in (int, float):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("FACT_VALUE_NOT_JSON")
        return ("number", number)
    if type(value) is str:
        return ("string", value)
    if type(value) is list:
        return ("array", tuple(_json_value_key(item) for item in value))
    if type(value) is dict and all(type(key) is str for key in value):
        return ("object", tuple((key, _json_value_key(value[key])) for key in sorted(value)))
    raise ValueError("FACT_VALUE_NOT_JSON")


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
        desktop_scope = validate_profile(analysis.assessment_profile) == DESKTOP_PROFILE
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
            retire_findings(session,
                select(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.category != "data_quality",
                ), reason="matching_gate",
            )
        else:
            analysis.status = "evaluating"
            analysis.current_stage = "evaluating"
        # Acquire the SQLite write lock before reading findings. Review submissions
        # cannot interleave with this evaluation transaction and overwrite decisions.
        session.flush()
        employees = list(
            session.scalars(
                select(Employee)
                .where(Employee.analysis_id == analysis_id)
                .order_by(Employee.employee_number)
            )
        )
        persisted: list[RiskFinding] = []
        coverage_values: list[float] = []
        if desktop_scope:
            coverage_rows = scoped_evidence_rows(session, analysis_id)
            employee_map = {employee.id: employee for employee in employees}
            scoped_coverage = DashboardService._material_coverage(employee_map, coverage_rows, profile=DESKTOP_PROFILE)
            evidence = coverage_evidence(employee_map, coverage_rows)
        for employee in employees:
            facts = list(
                session.scalars(
                    select(EmploymentFact).where(
                        EmploymentFact.analysis_id == analysis_id,
                        EmploymentFact.employee_id == employee.id,
                    )
                )
            )
            if not facts and not desktop_scope:
                self._retire_stale_findings(
                    session,
                    analysis_id,
                    employee.id,
                    set(),
                    data_quality_only=assessment_gated,
                )
                continue
            context_facts = self._context_facts(session, facts)
            supporting_fact_ids = None
            owner = session.get(CompanyAnalysisBinding, analysis_id)
            if owner and owner.role == "current":
                from qian_labor.services.effective_facts import effective_projection
                supporting_fact_ids = {row.id for row in effective_projection(session, analysis_id) if row.selected_support}
            if desktop_scope:
                # Model-supplied percentages never define the desktop denominator.
                # Link derived values to actual scoped evidence and retain source validation.
                supporting_rows = [row for rows in evidence["contributors"].values() for row in rows
                                   if row.source_id and row.file_id]
                for name, value, rows, uncertain in (
                    ("employment.material_coverage", evidence["rates"][employee.id],
                     [row for row in supporting_rows if row.employee_id == employee.id], evidence["uncertain"][employee.id]),
                    ("analysis.minimum_core_coverage", evidence["overall"], supporting_rows, any(evidence["uncertain"].values())),
                ):
                    context_facts.pop(name, None)
                    if rows:
                        context_facts[name] = FactValue(id=rows[0].fact_id, value=value,
                            source_locator_ids=tuple(sorted({row.source_id for row in rows})),
                            conflicted=uncertain)
            identity = context_facts.get("employment.identity.match_status")
            if identity is not None and not identity.conflicted:
                context_facts["employment.identity.match_status"] = FactValue(
                    id=identity.id,
                    value=employee.match_status,
                    source_locator_ids=identity.source_locator_ids,
                )
            coverage = context_facts.get("employment.material_coverage")
            if coverage and not coverage.conflicted:
                coverage_values.append(float(coverage.value))
            context = RuleContext(
                analysis_date=self._analysis_date(analysis, session),
                employee_id=employee.id,
                facts=context_facts,
            )
            results = (tuple(rule.evaluate(context) for code, rule in RULE_REGISTRY.items()
                       if code not in {*EXCLUDED_CODES, "R16"}
                       and (not assessment_gated or rule.metadata.category == "data_quality"))
                       if desktop_scope else evaluate_rules(context, category="data_quality" if assessment_gated else None))
            triggered_rule_ids: set[str] = set()
            for result in results:
                if not result.triggered:
                    continue
                metadata = next(
                    rule.metadata
                    for rule in RULE_REGISTRY.values()
                    if rule.metadata.rule_id == result.rule_id
                )
                participating = {
                    name: context_facts[name]
                    for name in metadata.required_facts
                    if name in context_facts
                    and context_facts[name].id in result.trigger_fact_ids
                }
                valid_sources = {
                    source for fact in participating.values() for source in fact.source_locator_ids
                }
                source_locator_ids = sorted(set(result.source_locator_ids))
                # Each contributing fact type needs its own related source. A source
                # for an unrelated field cannot substantiate an unsourced conclusion.
                if (
                    not set(source_locator_ids).issubset(valid_sources)
                    or set(result.trigger_fact_ids) != {fact.id for fact in participating.values()}
                    or (
                        result.assessment_status != "insufficient_data"
                        and (
                            not source_locator_ids
                            or any(
                                not set(fact.source_locator_ids).intersection(source_locator_ids)
                                for fact in participating.values()
                            )
                        )
                    )
                ):
                    analysis.status = "failed"
                    analysis.failure_reason = "FINDING_SOURCE_REQUIRED"
                    raise RiskEvaluationError("FINDING_SOURCE_REQUIRED")
                trigger_fact_ids = sorted(
                    fact.id for fact in facts if fact.fact_type in participating and
                    (supporting_fact_ids is None or fact.id in supporting_fact_ids)
                )
                if desktop_scope and result.rule_id == "MATERIAL_COVERAGE_LOW":
                    trigger_fact_ids = sorted({row.fact_id for row in coverage_rows
                                               if row.source_id in source_locator_ids})
                summary = str(result.message_params["finding_phrase"])
                if result.assessment_status == "requires_human_review" and any(
                    fact.conflicted for fact in participating.values()
                ):
                    summary = "事实尚未核实或材料值不一致，需要人工复核"
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
                retire_findings(session,
                    select(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.employee_id == employee.id,
                        RiskFinding.rule_id == result.rule_id,
                        RiskFinding.rule_version != result.rule_version,
                    ), reason="rule_version_changed",
                )
                existing = session.scalar(
                    select(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.employee_id == employee.id,
                        RiskFinding.rule_id == result.rule_id,
                        RiskFinding.rule_version == result.rule_version,
                    )
                )
                is_new = existing is None
                was_retired = existing is not None and not existing.is_current
                previous_signature = None
                if existing is not None:
                    previous_signature = latest_signature(session, existing)
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
                        summary=summary,
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
                    existing.is_current = True
                    existing.retired_at = None
                    existing.category = metadata.category
                    existing.severity = result.severity
                    existing.assessment_status = result.assessment_status
                    existing.title = metadata.name
                    existing.summary = summary
                    existing.trigger_fact_ids = trigger_fact_ids
                    existing.source_locator_ids = source_locator_ids
                    existing.missing_fact_types = list(result.missing_fact_types)
                    existing.legal_basis = basis_metadata
                    existing.recommended_actions = [metadata.recommended_action]
                    existing.requires_human_review = result.requires_human_review
                sync_evaluation_review(session, existing, previous_signature, is_new=is_new,
                                       force_reopen=was_retired)
                triggered_rule_ids.add(result.rule_id)
                persisted.append(existing)
            self._retire_stale_findings(
                session,
                analysis_id,
                employee.id,
                triggered_rule_ids,
                data_quality_only=assessment_gated,
            )

        self._update_aggregates(session, analysis, coverage_values)
        if desktop_scope:
            analysis.coverage_rate = scoped_coverage["overall"]
        if assessment_gated:
            analysis.status = "matching_review"
            analysis.current_stage = "matching_review"
            analysis.progress = 90
        else:
            analysis.status = "completed"
            analysis.current_stage = "completed"
            analysis.progress = 100
        from qian_labor.services.assessment_state import material_state, record_evaluation
        if session.get(CompanyAnalysisBinding, analysis_id):
            state = material_state(session, analysis_id)
            if not assessment_gated and state["completeness"] != "complete":
                analysis.status = analysis.current_stage = "partial"
            record_evaluation(session, analysis, session.info.pop("assessment_request_id", None))
        return tuple(persisted)

    @staticmethod
    def _retire_stale_findings(
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
            retire_findings(session,
                select(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.employee_id == employee_id,
                    RiskFinding.rule_id.in_(stale_rule_ids),
                ), reason="no_longer_triggered",
            )

    @staticmethod
    def _context_facts(session: Any, facts: list[EmploymentFact]) -> dict[str, FactValue]:
        from qian_labor.services.source_provenance import uncertain_grounded_fact_ids
        from qian_labor.services.effective_facts import effective_projection, validate_manual_value
        ids = {fact.id for fact in facts}
        current_ids = {aid for aid in {f.analysis_id for f in facts}
                       if (owner := session.get(CompanyAnalysisBinding, aid)) and owner.role == "current"}
        facts = [f for f in facts if f.analysis_id not in current_ids] + [
            row for aid in current_ids for row in effective_projection(session, aid) if row.id in ids and row.selected_support]
        grounding_pending = set().union(*(uncertain_grounded_fact_ids(session, aid) for aid in {f.analysis_id for f in facts}))
        # This is a conservative safety gate, not a period-selection algorithm.
        # Until periods are explicitly modelled, never choose the last imported value.
        grouped: dict[str, list[EmploymentFact]] = defaultdict(list)
        for fact in facts:
            grouped[fact.fact_type].append(fact)
        result: dict[str, FactValue] = {}
        for fact_type, group in sorted(grouped.items()):
            ordered = sorted(group, key=lambda fact: fact.id)
            period_union = None
            if fact_type == "employment.probation.periods" and all(f.analysis_id in current_ids for f in ordered):
                try:
                    period_union = sorted({tuple(period) for fact in ordered for period in
                        validate_manual_value(fact_type, fact.normalized_value_json)})
                except (ValueError, TypeError):
                    period_union = None
            try:
                values = {_json_value_key(fact.normalized_value_json) for fact in ordered}
                values_disagree = len(values) > 1 and period_union is None
            except (ValueError, RecursionError):
                # Invalid JSON cannot become an accepted fact merely because it
                # occurs once or every extraction repeated the same invalid value.
                values_disagree = True
            review_required = values_disagree or any(
                (fact.id in grounding_pending and not getattr(fact, "human_confirmed", False)) or fact.verification_status in {
                    "conflicted", "pending_review", "needs_human_confirmation",
                }
                for fact in ordered
            )
            source_ids: set[str] = set()
            for fact in ordered:
                if fact.analysis_id in current_ids:
                    source_ids.update(source.id for source in fact.state.sources)
                    continue
                source_ids.update(session.scalars(
                    select(SourceLocator.id)
                    .join(UploadedFile, UploadedFile.id == SourceLocator.file_id)
                    .where(
                        SourceLocator.fact_id == fact.id,
                        SourceLocator.analysis_id == fact.analysis_id,
                        SourceLocator.file_id == fact.file_id,
                        UploadedFile.analysis_id == fact.analysis_id,
                    )
                ))
            result[fact_type] = FactValue(
                id=ordered[0].id,
                value=None if review_required else period_union if period_union is not None else ordered[0].normalized_value_json,
                source_locator_ids=tuple(sorted(source_ids)),
                conflicted=review_required,
            )
        return result

    @staticmethod
    def _analysis_date(analysis: AnalysisBatch, session=None) -> date:
        if session is not None:
            from qian_labor.services.assessment_state import check_date
            return date.fromisoformat(check_date(session, analysis)[0])
        return analysis.created_at.date()

    @staticmethod
    def _update_aggregates(
        session: Any, analysis: AnalysisBatch, coverage_values: list[float]
    ) -> None:
        session.flush()
        update_risk_counts(session, analysis)
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
