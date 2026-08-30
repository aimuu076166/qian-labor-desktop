from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, cast

PublicAssessmentStatus = Literal[
    "management_reminder",
    "confirmed_anomaly",
    "suspected_risk",
    "insufficient_data",
    "requires_human_review",
]
AssessmentStatus = Literal[
    "management_reminder",
    "confirmed_anomaly",
    "suspected_risk",
    "insufficient_data",
    "requires_human_review",
    "not_triggered",
]
RuntimeAssessmentStatus = Literal[
    "management_reminder",
    "confirmed_anomaly",
    "suspected_risk",
    "insufficient_data",
    "requires_human_review",
    "unknown",
]
BasisType = Literal["legal_basis", "system_management_parameter"]

ASSESSMENT_STATUS_LABELS: dict[PublicAssessmentStatus, str] = {
    "management_reminder": "管理提醒",
    "confirmed_anomaly": "确定性异常",
    "suspected_risk": "疑似风险",
    "insufficient_data": "资料不足",
    "requires_human_review": "需要人工复核",
}


def assessment_status_label(status: str) -> str:
    public_status = cast(PublicAssessmentStatus, status)
    return ASSESSMENT_STATUS_LABELS.get(public_status, "未知状态")


def normalize_assessment_status(status: str) -> RuntimeAssessmentStatus:
    if status in ASSESSMENT_STATUS_LABELS:
        return cast(PublicAssessmentStatus, status)
    return "unknown"


@dataclass(frozen=True)
class FactValue:
    id: str
    value: Any
    source_locator_ids: tuple[str, ...]
    conflicted: bool = False


@dataclass(frozen=True)
class RuleContext:
    analysis_date: date
    employee_id: str | None
    facts: dict[str, FactValue]
    applicable_region: str = "CN"


@dataclass(frozen=True)
class ManagementParameter:
    basis_type: Literal["system_management_parameter"]
    name: str
    value: int | float | str
    unit: str
    description: str


@dataclass(frozen=True)
class RuleMetadata:
    rule_id: str
    version: str
    name: str
    category: str
    applicable_region: str
    effective_date: str
    basis_type: BasisType
    legal_source: tuple[str, ...]
    management_parameters: tuple[ManagementParameter, ...]
    last_verified_at: str
    required_facts: tuple[str, ...]
    severity: str
    assessment_status: AssessmentStatus
    requires_human_review: bool
    recommended_action: str


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    rule_version: str
    triggered: bool
    assessment_status: AssessmentStatus
    severity: str
    trigger_fact_ids: tuple[str, ...] = ()
    source_locator_ids: tuple[str, ...] = ()
    missing_fact_types: tuple[str, ...] = ()
    message_params: dict[str, Any] = field(default_factory=dict)
    requires_human_review: bool = False
    basis_type: BasisType | None = None
    legal_source: tuple[str, ...] | None = None
    management_parameters: tuple[ManagementParameter, ...] | None = None
