from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class DesktopStatusResponse(BaseModel):
    status: str
    database_path: str


class CreateAnalysisRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_display_name: str = Field(default="", max_length=200)


class AssessmentScope(BaseModel):
    identifier: Literal["legacy_full_v1", "labor_materials_v1"]
    display_label: str
    excluded_rule_codes: list[str]
    excluded_rule_ids: list[str]
    not_evaluated_reasons: dict[str, str]
    payroll_evaluated: bool
    attendance_evaluated: bool
    settlement_document_label: str


class ImportPathsRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class MatchDecisionRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    decision: Literal["assign", "create_unknown", "merge", "unmatched"]
    employee_id: str | None = Field(default=None, max_length=36)
    display_name: str | None = Field(default=None, max_length=100)
    employee_number: str | None = Field(default=None, max_length=80)
    source_employee_id: str | None = Field(default=None, max_length=36)
    target_employee_id: str | None = Field(default=None, max_length=36)
    fact_ids: list[str] = Field(default_factory=list, max_length=500)
    employee_record_id: str | None = Field(default=None, min_length=1, max_length=36)
    expected_record_version: int | None = Field(default=None, ge=0, strict=True)


class DashboardSummary(BaseModel):
    analysis_id: str
    status: str
    employee_count: int
    finding_count: int
    high_count: int
    medium_count: int
    insufficient_data_count: int


class FindingSummary(BaseModel):
    id: str
    rule_id: str
    title: str
    severity: str
    assessment_status: str
    requires_human_review: bool


class FindingSource(BaseModel):
    id: str
    file_id: str
    file_name: str
    locator_type: str
    location: dict[str, Any]
    excerpt: str


class FindingReviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expected_version: int = Field(ge=0, strict=True)
    status: Literal["reviewed", "dismissed", "not_applicable", "needs_material", "open"]
    note: str = Field(min_length=1, max_length=500)
