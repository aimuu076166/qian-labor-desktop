"""One read-only ownership boundary for displayed and reviewed evidence."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from qian_labor.models.core import AnalysisBatch, Employee, EmploymentFact, RiskFinding, SourceLocator, UploadedFile
from qian_labor.services.assessment_scope import DESKTOP_PROFILE, coverage_evidence, scoped_evidence_rows
from qian_labor.rules.registry import RULE_REGISTRY


@dataclass
class OwnedFindingEvidence:
    facts: list[EmploymentFact]
    sources: list[tuple[SourceLocator, UploadedFile]]
    consistent: bool


def owned_finding_evidence(session: Session, finding: RiskFinding) -> OwnedFindingEvidence:
    """Reject malformed declared references without substituting invented evidence.

    Evaluation stores all participating same-type facts, not just the representative
    fact. Desktop R20 additionally stores actual aggregate contributors across the
    analysis; no other finding gets that cross-employee allowance.
    """
    analysis = session.get(AnalysisBatch, finding.analysis_id)
    employee = session.get(Employee, finding.employee_id) if finding.employee_id else None
    owner_valid = analysis is not None and employee is not None and employee.analysis_id == finding.analysis_id
    aggregate = analysis is not None and analysis.assessment_profile == DESKTOP_PROFILE and finding.rule_id == "MATERIAL_COVERAGE_LOW"
    allowed_derived_sources = None
    allowed_derived_facts = None
    if aggregate:
        employees = {item.id: item for item in session.scalars(select(Employee).where(Employee.analysis_id == finding.analysis_id))}
        evidence = coverage_evidence(employees, scoped_evidence_rows(session, finding.analysis_id))
        rows = [row for group in evidence["contributors"].values() for row in group if row.source_id and row.file_id]
        allowed_derived_sources = {row.source_id for row in rows}
        allowed_derived_facts = {row.fact_id for row in rows}

    fact_ids = set(finding.trigger_fact_ids or [])
    statement = select(EmploymentFact).join(UploadedFile, UploadedFile.id == EmploymentFact.file_id).join(
        Employee, Employee.id == EmploymentFact.employee_id
    ).where(EmploymentFact.id.in_(fact_ids), EmploymentFact.analysis_id == finding.analysis_id,
            UploadedFile.analysis_id == finding.analysis_id, Employee.analysis_id == finding.analysis_id)
    statement = statement.where(EmploymentFact.id.in_(allowed_derived_facts)) if aggregate else statement.where(
        EmploymentFact.employee_id == finding.employee_id)
    if not aggregate:
        metadata = next((rule.metadata for code, rule in RULE_REGISTRY.items()
                         if finding.rule_id in {code, rule.metadata.rule_id}), None)
        statement = statement.where(EmploymentFact.fact_type.in_(metadata.required_facts if metadata else ()))
    facts = list(session.scalars(statement.order_by(EmploymentFact.id))) if owner_valid else []
    owned_ids = {fact.id for fact in facts}
    source_ids = list(dict.fromkeys(finding.source_locator_ids or []))
    rows = session.execute(select(SourceLocator, UploadedFile).join(
        UploadedFile, UploadedFile.id == SourceLocator.file_id
    ).join(EmploymentFact, EmploymentFact.id == SourceLocator.fact_id).where(
        SourceLocator.id.in_(source_ids), SourceLocator.fact_id.in_(owned_ids),
        SourceLocator.analysis_id == finding.analysis_id, UploadedFile.analysis_id == finding.analysis_id,
        SourceLocator.file_id == EmploymentFact.file_id,
    ))
    sources_by_id = {source.id: (source, uploaded) for source, uploaded in rows
                     if not aggregate or source.id in allowed_derived_sources}
    sources = [sources_by_id[sid] for sid in source_ids if sid in sources_by_id]
    consistent = owner_valid and owned_ids == fact_ids and set(source_ids) == set(sources_by_id)
    if finding.assessment_status != "insufficient_data":
        sourced_fact_ids = {source.fact_id for source, _ in sources}
        consistent = consistent and bool(sources) and (
            aggregate or {fact.fact_type for fact in facts} ==
            {fact.fact_type for fact in facts if fact.id in sourced_fact_ids}
        )
    return OwnedFindingEvidence(facts, sources, consistent)
