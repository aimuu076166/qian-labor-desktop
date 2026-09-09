"""Small typed company/identity API. Create IDs are caller-generated for reconciliation."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qian_labor.desktop.schemas import AssessmentScope
from qian_labor.security.masking import mask_sensitive


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CompanyCreate(Input):
    id: UUID
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def safe_display(cls, value):
        return mask_sensitive(value)


class RecordCreate(Input):
    id: UUID
    display_name: str = Field(min_length=1, max_length=100)
    employee_number: str | None = Field(default=None, max_length=80)
    department: str | None = Field(default=None, max_length=100)
    job_title: str | None = Field(default=None, max_length=100)

    @field_validator("employee_number")
    @classmethod
    def safe_number(cls, value):
        value = value or None
        if value and mask_sensitive(value) != value:
            raise ValueError("WORKSPACE_IDENTIFIER_INVALID")
        return value

    @field_validator("department", "job_title")
    @classmethod
    def safe_text(cls, value):
        return mask_sensitive(value) if value else None


class EmployeeCreate(RecordCreate):
    expected_company_version: int = Field(ge=0, strict=True)


class CurrentAnalysisCreate(Input):
    id: UUID
    expected_company_version: int = Field(ge=0, strict=True)


class HistoricalImportRequest(Input):
    source_analysis_id: UUID
    file_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("file_ids")
    @classmethod
    def distinct_files(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("WORKSPACE_REQUEST_INVALID")
        return value


class HistoricalImportOutcome(BaseModel):
    source_file_id: str
    file_id: str | None
    status: Literal["imported", "duplicate", "error"]
    error_code: str | None = None


class HistoricalImportView(BaseModel):
    analysis_id: str
    source_analysis_id: str
    results: list[HistoricalImportOutcome]
    requires_explicit_start: Literal[True] = True
    provider_quota_notice: str = "材料复用不会自动分析；手动开始重新分析可能消耗模型服务额度。"


class LinkDecision(Input):
    action: Literal["link"]
    snapshot_id: str = Field(min_length=1, max_length=36)
    employee_record_id: UUID
    expected_record_version: int = Field(ge=0, strict=True)


class CreateDecision(Input):
    action: Literal["create"]
    snapshot_id: str = Field(min_length=1, max_length=36)
    record: RecordCreate


class AdoptionRequest(Input):
    expected_company_version: int = Field(ge=0, strict=True)
    expected_analysis_version: int = Field(ge=0, strict=True)
    decisions: list[Annotated[LinkDecision | CreateDecision, Field(discriminator="action")]] = Field(max_length=10000)


class PreferenceUpdate(Input):
    last_company_id: UUID | None
    expected_version: int = Field(ge=0, strict=True)


class CompanyView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    display_name: str
    created_at: datetime
    version: int


class EmployeeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    company_id: str
    masked_name: str
    employee_number: str | None
    department: str | None
    job_title: str | None
    lifecycle_status: Literal["active", "archived"]
    version: int
    created_at: datetime


class SnapshotBindingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    snapshot_id: str
    employee_record_id: str
    analysis_id: str
    company_id: str
    created_at: datetime


class EmployeeDetail(EmployeeView):
    bindings: list[SnapshotBindingView]
    current_binding: SnapshotBindingView | None = None
    historical_bindings: list[SnapshotBindingView] = Field(default_factory=list)


class EmployeePage(BaseModel):
    items: list[EmployeeView]
    total: int
    page: int
    page_size: int
    pages: int


class BindingView(BaseModel):
    company_id: str
    analysis_id: str
    bound: bool
    role: Literal["historical", "current"] | None
    company_version: int
    analysis_version: int
    bindings: list[SnapshotBindingView]
    excluded_merged_snapshot_ids: list[str]


class CompanyAnalysisView(BaseModel):
    id: str
    assessment_scope: AssessmentScope
    name: str
    company_display_name: str
    status: str
    file_count: int
    employee_count: int
    high_count: int
    medium_count: int
    low_count: int
    insufficient_data_count: int
    coverage_rate: float
    progress: int
    current_stage: str
    is_demo: bool
    purge_at: datetime
    created_at: datetime
    version: int
    relation: Literal["historical", "unbound"]
    company_id: str | None
    pending_identity_count: int
    can_adopt: bool
    adoption_blocker_code: str | None


class CompanyAnalysisPage(BaseModel):
    items: list[CompanyAnalysisView]
    total: int
    page: int
    page_size: int
    pages: int


class PreferenceView(BaseModel):
    last_company_id: str | None
    version: int
