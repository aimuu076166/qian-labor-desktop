"""Versioned desktop assessment selection; the generic rule catalog stays frozen."""
from qian_labor.rules.registry import RULE_REGISTRY
from sqlalchemy import and_, select
from sqlalchemy.orm import aliased
from qian_labor.models.core import EmploymentFact, SourceLocator, UploadedFile

LEGACY_PROFILE = "legacy_full_v1"
DESKTOP_PROFILE = "labor_materials_v1"
EXCLUDED_CODES = ("R09", "R13", "R14", "R15")
UNCERTAIN = {"conflicted", "pending_review", "needs_human_confirmation"}
PRESENCE_TYPES = {"contract": "employment.contract.exists", "social_insurance": "employment.social_insurance.present"}


def coverage_evidence(employees, rows):
    """Calculate desktop coverage and retain exactly the facts that determine it.

    A document-level model percentage is never document evidence. Presence
    conflicts matter; unrelated notes and their review flags do not.
    """
    statuses, covered, contributors, uncertain, rates = {}, {}, {}, {}, {}
    for eid, employee in employees.items():
        own = [row for row in rows if row.employee_id == eid]
        status_rows = [row for row in own if row.fact_type == "employment.status"]
        if not status_rows:
            status = employee.employment_status if employee.employment_status in {"active", "probation", "terminated"} else "unknown"
        elif any(row.verification_status in UNCERTAIN or type(row.normalized_value_json) is not str for row in status_rows):
            status = "unknown"
        else:
            values = {row.normalized_value_json for row in status_rows}
            status = next(iter(values)) if len(values) == 1 and values <= {"active", "probation", "terminated"} else "unknown"
        statuses[eid], covered[eid] = status, set()
        used = list(status_rows)
        pending = status == "unknown"
        applicable = tuple(PRESENCE_TYPES) if status in {"active", "probation"} else ("termination",) if status == "terminated" else ()
        for kind in applicable:
            if kind in PRESENCE_TYPES:
                candidates = [row for row in own if row.fact_type == PRESENCE_TYPES[kind]]
                local = [row for row in candidates if row.file_id and row.classified_kind == kind]
                if local:
                    used.extend(candidates)
                    pending |= any(row.verification_status in UNCERTAIN for row in candidates)
                    if all(row.normalized_value_json is True and row.verification_status not in UNCERTAIN for row in candidates):
                        covered[eid].add(kind)
            else:
                candidates = [row for row in own if row.file_id and row.classified_kind == "termination"
                              and row.fact_type.startswith("employment.termination.")]
                confirmed = [row for row in candidates if row.normalized_value_json is not None
                             and row.verification_status not in UNCERTAIN]
                if confirmed:
                    # One actual termination-document fact proves the document;
                    # other notes do not participate merely by sharing its file.
                    used.append(sorted(confirmed, key=lambda row: (row.fact_id, row.source_id or ""))[0])
                    covered[eid].add(kind)
                else:
                    used.extend(candidates)
                    pending |= any(row.verification_status in UNCERTAIN for row in candidates)
        contributors[eid] = used
        uncertain[eid] = pending
        rates[eid] = round(len(covered[eid]) / len(applicable), 4) if applicable else 0.0
    denominator = sum(2 if status in {"active", "probation"} else 1 if status == "terminated" else 0 for status in statuses.values())
    return {"statuses": statuses, "covered": covered, "contributors": contributors,
            "uncertain": uncertain, "rates": rates,
            "overall": round(sum(map(len, covered.values())) / denominator, 4) if denominator else 0.0,
            "scope_pending": not employees or "unknown" in statuses.values()}


def scoped_evidence_rows(session, analysis_id, *, employee_ids=None):
    """Only a locator attached to its own fact/file/analysis can prove coverage."""
    from types import SimpleNamespace
    from qian_labor.models.core import CompanyAnalysisBinding
    from qian_labor.services.effective_facts import effective_projection
    owner = session.get(CompanyAnalysisBinding, analysis_id)
    if owner and owner.role == "current":
        rows = []
        for fact in effective_projection(session, analysis_id, employee_ids=employee_ids):
            if not fact.selected_support:
                continue
            sources = [x for x in fact.state.sources if x.analysis_id == analysis_id and x.file_id == fact.file_id
                       and fact.state.file and fact.state.file.analysis_id == analysis_id]
            for source in sources or [None]:
                rows.append(SimpleNamespace(fact_id=fact.id, employee_id=fact.employee_id, fact_type=fact.fact_type,
                    normalized_value_json=fact.normalized_value_json,
                    verification_status=fact.verification_status if fact.state.valid else "needs_human_confirmation",
                    created_at=fact.created_at, file_id=source.file_id if source else None,
                    source_id=source.id if source else None,
                    classified_kind=fact.state.file.classified_kind if source else None))
        return rows
    owning_file = aliased(UploadedFile)
    statement = select(
        EmploymentFact.id.label("fact_id"), EmploymentFact.employee_id, EmploymentFact.fact_type,
        EmploymentFact.normalized_value_json, EmploymentFact.verification_status,
        EmploymentFact.created_at, UploadedFile.id.label("file_id"), UploadedFile.classified_kind,
        SourceLocator.id.label("source_id"),
    ).join(owning_file, and_(owning_file.id == EmploymentFact.file_id,
        owning_file.analysis_id == EmploymentFact.analysis_id
    )).outerjoin(SourceLocator, and_(SourceLocator.fact_id == EmploymentFact.id,
        SourceLocator.file_id == EmploymentFact.file_id, SourceLocator.analysis_id == EmploymentFact.analysis_id
    )).outerjoin(UploadedFile, and_(UploadedFile.id == SourceLocator.file_id,
        UploadedFile.analysis_id == EmploymentFact.analysis_id
    )).where(EmploymentFact.analysis_id == analysis_id)
    if employee_ids is not None:
        statement = statement.where(EmploymentFact.employee_id.in_(employee_ids))
    from qian_labor.services.source_provenance import uncertain_grounded_fact_ids
    pending = uncertain_grounded_fact_ids(session, analysis_id)
    return [SimpleNamespace(**{**dict(row._mapping), "verification_status": "needs_human_confirmation"})
            if row.fact_id in pending else row for row in session.execute(statement)]


def validate_profile(identifier: str) -> str:
    if identifier not in {LEGACY_PROFILE, DESKTOP_PROFILE}:
        raise ValueError("ASSESSMENT_PROFILE_UNSUPPORTED")
    return identifier


def scope_payload(identifier: str) -> dict:
    desktop = validate_profile(identifier) == DESKTOP_PROFILE
    return {
        "identifier": identifier,
        "display_label": "用工材料体检 v1" if desktop else "历史完整规则范围 v1",
        "excluded_rule_codes": list(EXCLUDED_CODES) if desktop else [],
        "excluded_rule_ids": [RULE_REGISTRY[code].metadata.rule_id for code in EXCLUDED_CODES] if desktop else [],
        "not_evaluated_reasons": {
            "R16": "现有离职后记录为工资、考勤、社保合并事实，无法核实社保延续来源，本项暂未评估。"
        } if desktop else {},
        "payroll_evaluated": not desktop,
        "attendance_evaluated": not desktop,
        "settlement_document_label": "结算文件（final_pay），不要求工资表，不计算工资或补偿",
    }
