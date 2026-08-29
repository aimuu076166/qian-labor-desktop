from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class DesktopStatusResponse(BaseModel):
    status: str
    database_path: str


class CreateAnalysisRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_display_name: str = Field(default="", max_length=200)


class ImportPathsRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


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
