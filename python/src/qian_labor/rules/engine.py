from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from qian_labor.rules.registry import RULE_REGISTRY
from qian_labor.rules.types import (
    AssessmentStatus,
    FactValue,
    RuleContext,
    RuleResult,
    assessment_status_label,
)


def evaluate_rules(context: RuleContext, *, category: str | None = None) -> tuple[RuleResult, ...]:
    return tuple(
        rule.evaluate(context)
        for rule in RULE_REGISTRY.values()
        if category is None or rule.metadata.category == category
    )


@dataclass
class RiskFinding:
    id: str
    rule_id: str
    title: str
    severity: str
    assessment_status: AssessmentStatus
    status_label: str
    employee_id: str | None
    category: str
    source_id: str | None = None
    missing_evidence: list[str] | None = None
    requires_human_review: bool = True
    recommended_action: str = "核对材料并由人工复核"
    review_state: str = "open"
    explanation: str = ""


def evaluate(context: dict) -> list[RiskFinding]:
    """Legacy facade retained while old clients migrate to ``evaluate_rules``."""

    legacy = {
        "missing_contract": "R01",
        "expiring_contract": "R02",
        "expired_continued": "R03",
        "probation_missing": "R08",
        "entity_mismatch": "R11",
        "insurance_missing": "R10",
        "overtime_gap": "R13",
        "terminated_payroll": "R16",
        "delivery_missing": "R17",
        "coverage_low": "R20",
    }
    selected = next((code for key, code in legacy.items() if context.get(key)), None)
    if selected is None:
        return []
    rule = RULE_REGISTRY[selected]
    facts = {
        name: FactValue(f"legacy-{index}", None, ("legacy-source",))
        for index, name in enumerate(rule.metadata.required_facts)
    }
    result = rule.evaluate(
        RuleContext(
            analysis_date=date(2026, 8, 24),
            employee_id=context.get("employee_id"),
            facts=facts,
        )
    )
    if context.get("overtime_gap") or context.get("coverage_low"):
        result = RuleResult(
            rule_id=result.rule_id,
            rule_version=result.rule_version,
            triggered=True,
            assessment_status="insufficient_data",
            severity="info",
            trigger_fact_ids=result.trigger_fact_ids,
            source_locator_ids=result.source_locator_ids,
            missing_fact_types=result.missing_fact_types,
            message_params={"finding_phrase": "资料不足，暂时无法判断"},
            requires_human_review=result.requires_human_review,
        )
    return [
        RiskFinding(
            id=f"{result.rule_id}-{context.get('employee_id') or 'summary'}",
            rule_id=result.rule_id,
            title=rule.metadata.name,
            severity=result.severity,
            assessment_status=result.assessment_status,
            status_label=assessment_status_label(result.assessment_status),
            employee_id=context.get("employee_id"),
            category=rule.metadata.category,
            source_id=next(iter(result.source_locator_ids), None),
            missing_evidence=list(result.missing_fact_types),
            requires_human_review=result.requires_human_review,
            explanation=result.message_params["finding_phrase"],
        )
    ]
