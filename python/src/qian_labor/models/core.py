from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class ReportVersion(Base):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("analysis_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload_json: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    input_revision: Mapped[str] = mapped_column(String(64))
    result_revision: Mapped[str | None] = mapped_column(String(36))
    review_revision: Mapped[str] = mapped_column(String(64))
    context_signature: Mapped[str] = mapped_column(String(64))


class ReportGenerationRequest(Base):
    __tablename__ = "report_generation_requests"
    __table_args__ = (CheckConstraint("outcome IN ('accepted','rejected')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"))
    body_json: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(80))
    version_id: Mapped[str | None] = mapped_column(ForeignKey("report_versions.id", ondelete="CASCADE"))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (CheckConstraint("state IN ('queued','running','cancel_requested','cancelled','interrupted','completed','partial','failed')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"))
    parent_run_id: Mapped[str | None] = mapped_column(String(36))
    owner_nonce: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(24), default="queued")
    version: Mapped[int] = mapped_column(Integer, default=0)
    in_flight: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRequest(Base):
    __tablename__ = "task_requests"
    __table_args__ = (CheckConstraint("outcome IN ('pending','accepted','rejected')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(16))
    expected_run_id: Mapped[str | None] = mapped_column(String(36))
    expected_version: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompanyWorkspace(Base):
    __tablename__ = "company_workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=0)


class EmployeeRecord(Base):
    """Company-owned identity; Employee remains the historical evaluation snapshot."""
    __tablename__ = "employee_records"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_number"),
        UniqueConstraint("id", "company_id"),
        CheckConstraint("lifecycle_status IN ('active', 'archived')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"), index=True)
    masked_name: Mapped[str] = mapped_column(String(100))
    employee_number: Mapped[str | None] = mapped_column(String(80))
    department: Mapped[str | None] = mapped_column(String(100))
    job_title: Mapped[str | None] = mapped_column(String(100))
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="active")
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompanyAnalysisBinding(Base):
    __tablename__ = "company_analysis_bindings"
    __table_args__ = (
        UniqueConstraint("analysis_id", "company_id"),
        CheckConstraint("role IN ('historical', 'current')"),
        Index("uq_company_current_analysis", "company_id", unique=True, sqlite_where=text("role = 'current'")),
    )
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="historical", server_default="historical")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmployeeSnapshotBinding(Base):
    __tablename__ = "employee_snapshot_bindings"
    __table_args__ = (
        ForeignKeyConstraint(["analysis_id", "company_id"], ["company_analysis_bindings.analysis_id", "company_analysis_bindings.company_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["employee_record_id", "company_id"], ["employee_records.id", "employee_records.company_id"], ondelete="RESTRICT"),
    )
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36), index=True)
    company_id: Mapped[str] = mapped_column(String(36))
    employee_record_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspacePreference(Base):
    __tablename__ = "workspace_preferences"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_company_id: Mapped[str | None] = mapped_column(ForeignKey("company_workspaces.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=0)


class AnalysisBatch(Base):
    __tablename__ = "analysis_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    company_display_name: Mapped[str] = mapped_column(String(200), default="")
    assessment_profile: Mapped[str] = mapped_column(String(40), default="legacy_full_v1", server_default="legacy_full_v1")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: utcnow() + timedelta(hours=24)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    employee_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, default=0)
    low_count: Mapped[int] = mapped_column(Integer, default=0)
    insufficient_data_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_rate: Mapped[float] = mapped_column(Float, default=0.0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_stage: Mapped[str] = mapped_column(String(64), default="created")
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=0)

    files: Mapped[list[UploadedFile]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    employees: Mapped[list[Employee]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    match_candidates: Mapped[list[EmployeeMatchCandidate]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    match_decisions: Mapped[list[EmployeeMatchDecision]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    facts: Mapped[list[EmploymentFact]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    sources: Mapped[list[SourceLocator]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    findings: Mapped[list[RiskFinding]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    ai_usage: Mapped[list[AIUsageRecord]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="analysis", cascade="all, delete-orphan")
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(back_populates="analysis", cascade="all, delete-orphan")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (UniqueConstraint("analysis_id", "sha256"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    extension: Mapped[str] = mapped_column(String(12))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    detected_kind: Mapped[str] = mapped_column(String(32), default="unknown")
    classified_kind: Mapped[str] = mapped_column(String(32), default="unknown")
    page_count: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    purge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="files")
    parsed_document: Mapped[ParsedDocument | None] = relationship(back_populates="file", cascade="all, delete-orphan", uselist=False)
    sources: Mapped[list[SourceLocator]] = relationship(back_populates="file", cascade="all, delete-orphan")
    facts: Mapped[list[EmploymentFact]] = relationship(back_populates="file")
    ai_usage: Mapped[list[AIUsageRecord]] = relationship(back_populates="file")


class ParsedDocument(Base):
    __tablename__ = "parsed_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), unique=True)
    parser_name: Mapped[str] = mapped_column(String(80))
    parser_version: Mapped[str] = mapped_column(String(32), default="1")
    detected_kind: Mapped[str] = mapped_column(String(32), default="unknown")
    needs_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file: Mapped[UploadedFile] = relationship(back_populates="parsed_document")
    blocks: Mapped[list[ParsedBlock]] = relationship(back_populates="document", cascade="all, delete-orphan", order_by="ParsedBlock.position")


class ParsedBlock(Base):
    __tablename__ = "parsed_blocks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("parsed_documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    block_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, default="")
    locator: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[ParsedDocument] = relationship(back_populates="blocks")


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("analysis_id", "employee_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    masked_name: Mapped[str] = mapped_column(String(100))
    normalized_name: Mapped[str] = mapped_column(String(100))
    employee_number: Mapped[str | None] = mapped_column(String(80))
    id_number_hash: Mapped[str | None] = mapped_column(String(64))
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    bank_card_hash: Mapped[str | None] = mapped_column(String(64))
    department: Mapped[str | None] = mapped_column(String(100))
    job_title: Mapped[str | None] = mapped_column(String(100))
    employment_status: Mapped[str] = mapped_column(String(32), default="unknown")
    hire_date: Mapped[date | None] = mapped_column(Date)
    termination_date: Mapped[date | None] = mapped_column(Date)
    match_status: Mapped[str] = mapped_column(String(32), default="unknown")
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="employees")
    facts: Mapped[list[EmploymentFact]] = relationship(back_populates="employee")
    findings: Mapped[list[RiskFinding]] = relationship(back_populates="employee")


class EmployeeMatchCandidate(Base):
    __tablename__ = "employee_match_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"))
    candidate_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="match_candidates")


class EmployeeMatchDecision(Base):
    __tablename__ = "employee_match_decisions"
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_employee_match_decision_candidate"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("employee_match_candidates.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(32))
    target_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    corrected_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(80), default="competition-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="match_decisions")


class EffectiveFactRevision(Base):
    __tablename__ = "effective_fact_revisions"
    __table_args__ = (UniqueConstraint("fact_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fact_id: Mapped[str] = mapped_column(ForeignKey("employment_facts.id", ondelete="CASCADE"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="CASCADE"))
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    record_id: Mapped[str] = mapped_column(ForeignKey("employee_records.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[Any] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(40), default="local-user")
    owner_signature: Mapped[str] = mapped_column(String(64))
    source_signature: Mapped[str] = mapped_column(String(64))
    context_signature: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssessmentDecision(Base):
    __tablename__ = "assessment_decisions"
    __table_args__ = (UniqueConstraint("analysis_id", "scope", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company_workspaces.id", ondelete="CASCADE"))
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    record_id: Mapped[str | None] = mapped_column(ForeignKey("employee_records.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    value: Mapped[Any] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(40), default="local-user")
    dependency_signature: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssessmentResult(Base):
    __tablename__ = "assessment_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    input_revision: Mapped[str] = mapped_column(String(64))
    check_date: Mapped[str] = mapped_column(String(10))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class EmploymentFact(Base):
    __tablename__ = "employment_facts"
    __table_args__ = (UniqueConstraint("analysis_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"))
    fact_type: Mapped[str] = mapped_column(String(120), index=True)
    value_json: Mapped[Any] = mapped_column(JSON)
    normalized_value_json: Mapped[Any] = mapped_column(JSON)
    extraction_method: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified")
    dedupe_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="facts")
    employee: Mapped[Employee | None] = relationship(back_populates="facts")
    file: Mapped[UploadedFile | None] = relationship(back_populates="facts")
    sources: Mapped[list[SourceLocator]] = relationship(back_populates="fact")


class SourceLocator(Base):
    __tablename__ = "source_locators"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    fact_id: Mapped[str | None] = mapped_column(ForeignKey("employment_facts.id", ondelete="CASCADE"))
    locator_type: Mapped[str] = mapped_column(String(24))
    location: Mapped[dict[str, Any]] = mapped_column(JSON)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="sources")
    file: Mapped[UploadedFile] = relationship(back_populates="sources")
    fact: Mapped[EmploymentFact | None] = relationship(back_populates="sources")


class RiskFinding(Base):
    __tablename__ = "risk_findings"
    __table_args__ = (
        UniqueConstraint("analysis_id", "employee_id", "rule_id", "rule_version"),
        Index("ix_risk_findings_analysis_current", "analysis_id", "is_current"),
        CheckConstraint(
            "assessment_status IN ('management_reminder', 'confirmed_anomaly', 'suspected_risk', 'insufficient_data', 'requires_human_review')",
            name="ck_risk_findings_assessment_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[str] = mapped_column(String(80), index=True)
    rule_version: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(16))
    assessment_status: Mapped[str] = mapped_column(String(32))
    review_status: Mapped[str] = mapped_column(String(32), default="open")
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    trigger_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_locator_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_fact_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    legal_basis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="findings")
    employee: Mapped[Employee | None] = relationship(back_populates="findings")
    reviews: Mapped[list[FindingReview]] = relationship(back_populates="finding", cascade="all, delete-orphan")


class FindingReview(Base):
    __tablename__ = "finding_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("risk_findings.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(80), default="competition-user")
    old_status: Mapped[str] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finding: Mapped[RiskFinding] = relationship(back_populates="reviews")


class AIUsageRecord(Base):
    __tablename__ = "ai_usage_records"
    __table_args__ = (UniqueConstraint("analysis_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(50))
    input_units: Mapped[int] = mapped_column(Integer, default=0)
    output_units: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="ai_usage")
    file: Mapped[UploadedFile | None] = relationship(back_populates="ai_usage")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (UniqueConstraint("unique_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"))
    job_type: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    unique_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="processing_jobs")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(80), default="system")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis: Mapped[AnalysisBatch] = relationship(back_populates="audit_events")


class DeletionTombstone(Base):
    __tablename__ = "deletion_tombstones"
    analysis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deletion_reason: Mapped[str] = mapped_column(String(40), default="user_requested")


class ContractAdvisoryRun(Base):
    __tablename__ = "contract_advisory_runs"
    __table_args__ = (UniqueConstraint("analysis_id", "file_id", "input_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    input_key: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(40))
    execution_status: Mapped[str] = mapped_column(String(32))
    input_statuses: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContractClauseObservation(Base):
    __tablename__ = "contract_clause_observations"
    __table_args__ = (UniqueConstraint("run_id", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("contract_advisory_runs.id", ondelete="CASCADE"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_batches.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), index=True)
    match_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("employee_match_candidates.id", ondelete="SET NULL"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    issue: Mapped[str] = mapped_column(Text)
    checks: Mapped[list[str]] = mapped_column(JSON)
    next_action: Mapped[str] = mapped_column(Text)
    unverified_references: Mapped[list[str]] = mapped_column(JSON)
    source_location: Mapped[dict] = mapped_column(JSON)
    source_excerpt: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContractAdvisoryHandling(Base):
    __tablename__ = "contract_advisory_handlings"
    __table_args__ = (UniqueConstraint("observation_id", "version"),
                      CheckConstraint("decision IN ('pending','checking','addressed','dismissed')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("contract_clause_observations.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(40), default="local-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
