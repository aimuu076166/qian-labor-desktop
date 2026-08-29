from __future__ import annotations

# Provider outputs are intentionally restricted to facts consumed by the frozen
# R01-R20 catalog. The regression suite compares this tuple to the catalog's
# required_facts union so model prompts cannot silently drift from rule inputs.
CANONICAL_FACT_TYPES = (
    "analysis.minimum_core_coverage",
    "employment.attendance.comp_time_evidence",
    "employment.attendance.overtime_hours",
    "employment.attendance.overtime_type",
    "employment.attendance.present",
    "employment.attendance_payroll.mismatch",
    "employment.contract.employer",
    "employment.contract.end_date",
    "employment.contract.exists",
    "employment.contract.start_date",
    "employment.contract.term_readable",
    "employment.contract.type",
    "employment.entities",
    "employment.entity_mismatch_explained",
    "employment.evidence_after_contract_end",
    "employment.identity.match_status",
    "employment.material_coverage",
    "employment.pay.actual_wage",
    "employment.pay.comparable",
    "employment.pay.contract_wage",
    "employment.pay.overtime_evidence",
    "employment.post_termination_record",
    "employment.probation.assessment_exists",
    "employment.probation.end_date",
    "employment.probation.periods",
    "employment.probation.start_date",
    "employment.social_insurance.entity",
    "employment.social_insurance.period_matches",
    "employment.social_insurance.present",
    "employment.social_insurance.waiver_language",
    "employment.start_date",
    "employment.status",
    "employment.termination.delivery_exists",
    "employment.termination.notice_exists",
    "employment.termination.occurred",
    "employment.termination.settlement_materials",
)

CANONICAL_FACT_TYPE_SET = frozenset(CANONICAL_FACT_TYPES)
