from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from qian_labor.database import Database
from qian_labor.models.core import (
    AnalysisBatch, AuditEvent, Employee, FindingReview, RiskFinding, CompanyAnalysisBinding,
)
from qian_labor.security.masking import mask_sensitive
from qian_labor.security.filenames import display_filename
from qian_labor.services.finding_sources import owned_finding_evidence
from qian_labor.services.source_provenance import projected_source

REVIEW_STATUSES = {"reviewed", "dismissed", "not_applicable", "needs_material", "open"}
REVIEWABLE_ANALYSES = {"completed", "partial"}
SIGNATURE_EVENT = "finding_evidence_signature"


class FindingReviewError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def review_payload(finding: RiskFinding) -> dict[str, Any]:
    return {
        "is_current": finding.is_current,
        "retired_at": finding.retired_at.isoformat() if finding.retired_at else None,
        "review_status": finding.review_status,
        "version": finding.version,
        "missing_fact_types": finding.missing_fact_types or [],
        "legal_basis": finding.legal_basis or {},
        "recommended_actions": finding.recommended_actions or [],
        "reviews": [
            {"status": review.new_status, "note": review.note,
             "created_at": review.created_at.isoformat()}
            for review in sorted(finding.reviews, key=lambda item: (item.created_at, item.id))
        ],
    }


def finding_detail_payload(session: Session, finding: RiskFinding) -> dict[str, Any]:
    from qian_labor.services.effective_facts import effective_projection
    from qian_labor.services.assessment_state import assessment_metadata
    owner = session.get(CompanyAnalysisBinding, finding.analysis_id)
    current = bool(owner and owner.role == "current")
    effective = {r.id: r for r in effective_projection(session, finding.analysis_id)} if current else {}
    sources = []
    for source, uploaded in owned_finding_evidence(session, finding).sources:
        sources.append({
            "id": source.id, "file_id": source.file_id, "file_name": display_filename(uploaded.original_filename),
            **projected_source(source),
            **({"human_confirmed": bool(source.fact_id in effective and effective[source.fact_id].human_confirmed)} if current else {}),
        })
    return {
        "id": finding.id, "analysis_id": finding.analysis_id, "rule_id": finding.rule_id,
        "title": finding.title, "severity": finding.severity,
        "assessment_status": finding.assessment_status,
        "requires_human_review": finding.requires_human_review or any(s["provenance"] != "locally_located" and not s.get("human_confirmed", False) for s in sources),
        "assessment_revision": assessment_metadata(session, finding.analysis_id),
        "summary": finding.summary, "sources": sources, **review_payload(finding),
    }


def finding_signature(session: Session, finding: RiskFinding) -> str:
    """Persist only a digest; never copy original facts into the audit event."""
    result_fields = (
        "rule_id", "rule_version", "employee_id", "category", "severity", "assessment_status",
        "title", "summary", "missing_fact_types", "legal_basis", "recommended_actions",
        "requires_human_review",
    )
    payload = {field: getattr(finding, field) for field in result_fields}
    analysis = session.get(AnalysisBatch, finding.analysis_id)
    if finding.rule_id == "MATERIAL_COVERAGE_LOW" and analysis.assessment_profile == "labor_materials_v1":
        from qian_labor.services.assessment_scope import coverage_evidence, scoped_evidence_rows
        employees = {item.id: item for item in session.scalars(select(Employee).where(Employee.analysis_id == analysis.id))}
        evidence = coverage_evidence(employees, scoped_evidence_rows(session, analysis.id))
        payload["derived_coverage"] = {
            "profile": analysis.assessment_profile, "rates": evidence["rates"], "overall": evidence["overall"],
            "statuses": evidence["statuses"], "uncertain": evidence["uncertain"],
            "fallback_statuses": {eid: employee.employment_status for eid, employee in employees.items()},
            "dependencies": sorted({(row.fact_id, row.file_id or "", row.classified_kind or "", row.source_id or "")
                                    for rows in evidence["contributors"].values() for row in rows}),
        }
    payload["trigger_fact_ids"] = sorted(finding.trigger_fact_ids or [])
    payload["source_locator_ids"] = sorted(finding.source_locator_ids or [])
    owned = owned_finding_evidence(session, finding)
    if not owned.consistent:
        payload["provenance_unavailable"] = True
    payload["facts"] = [
        {"id": fact.id, "fact_type": fact.fact_type, "file_id": fact.file_id,
         "employee_id": fact.employee_id, "value": fact.value_json,
         "normalized_value": fact.normalized_value_json,
         "verification_status": fact.verification_status}
        for fact in owned.facts
    ]
    from qian_labor.services.effective_facts import effective_projection
    from qian_labor.services.assessment_state import check_date
    owner = session.get(CompanyAnalysisBinding, finding.analysis_id)
    if owner and owner.role == "current":
        payload["effective_facts"] = [{"id": row.id, "value": row.normalized_value_json,
            "version": row.version, "human_confirmed": row.human_confirmed,
            "owner_signature": row.state.owner_signature, "source_signature": row.state.source_signature,
            "context_signature": row.context_signature, "selected_support": row.selected_support}
            for row in effective_projection(session, finding.analysis_id) if row.id in (finding.trigger_fact_ids or [])]
        payload["check_date"] = check_date(session, analysis)
    payload["sources"] = [
        {"id": source.id, "analysis_id": source.analysis_id, "fact_id": source.fact_id,
         "file_id": source.file_id, "locator_type": source.locator_type,
         "location": source.location, "excerpt": source.excerpt, "content_hash": source.content_hash}
        for source, _ in sorted(owned.sources, key=lambda row: row[0].id)
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def latest_signature(session: Session, finding: RiskFinding) -> str | None:
    event = session.scalar(select(AuditEvent).where(
        AuditEvent.analysis_id == finding.analysis_id,
        AuditEvent.event_type == SIGNATURE_EVENT,
        AuditEvent.metadata_json["finding_id"].as_string() == finding.id,
    ).order_by(AuditEvent.metadata_json["version"].as_integer().desc(),
               AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1))
    return event.metadata_json["signature"] if event is not None else None


def record_signature(session: Session, finding: RiskFinding, signature: str) -> None:
    session.add(AuditEvent(
        analysis_id=finding.analysis_id, event_type=SIGNATURE_EVENT, actor="system",
        metadata_json={"finding_id": finding.id, "version": finding.version, "signature": signature},
    ))


def update_risk_counts(session: Session, analysis: AnalysisBatch) -> None:
    findings = list(session.scalars(select(RiskFinding).where(
        RiskFinding.analysis_id == analysis.id,
        RiskFinding.is_current.is_(True),
        RiskFinding.review_status.not_in({"resolved", "dismissed", "not_applicable"}),
    )))
    for severity in ("high", "medium", "low"):
        setattr(analysis, f"{severity}_count", sum(
            item.severity == severity and item.assessment_status != "insufficient_data"
            for item in findings
        ))
    analysis.insufficient_data_count = sum(
        item.assessment_status == "insufficient_data" for item in findings
    )


def sync_evaluation_review(
    session: Session, finding: RiskFinding, previous_signature: str | None, *, is_new: bool,
    force_reopen: bool = False,
) -> None:
    signature = finding_signature(session, finding)
    if signature == previous_signature and not force_reopen:
        return
    if not is_new:
        old_status, version = finding.review_status, finding.version
        updated = session.execute(update(RiskFinding).where(
            RiskFinding.id == finding.id, RiskFinding.version == version,
        ).values(review_status="open", version=version + 1))
        if updated.rowcount != 1:
            raise FindingReviewError("DESKTOP_REVIEW_STALE")
        session.add(FindingReview(
            finding_id=finding.id, actor="system", old_status=old_status, new_status="open",
            note="判断依据、结论或适用状态已变化，请重新核对；原复核记录保留。",
        ))
    record_signature(session, finding, signature)


class FindingReviewService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def review(self, finding_id: str, *, expected_version: int, status: str, note: str) -> dict[str, Any]:
        note = note.strip()
        if (status not in REVIEW_STATUSES or not 1 <= len(note) <= 500
                or type(expected_version) is not int or expected_version < 0):
            raise FindingReviewError("DESKTOP_REVIEW_INVALID")
        try:
            return self._save(finding_id, expected_version=expected_version, status=status, note=note)
        except OperationalError as error:
            error_code = getattr(error.orig, "sqlite_errorcode", 0)
            if error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise FindingReviewError("DESKTOP_REVIEW_UNAVAILABLE") from error
            raise

    def _save(self, finding_id: str, *, expected_version: int, status: str, note: str) -> dict[str, Any]:
        with self.database.session() as session:
            finding = session.get(RiskFinding, finding_id)
            if finding is None:
                raise KeyError(finding_id)
            analysis = session.get(AnalysisBatch, finding.analysis_id)
            if analysis is None or analysis.status not in REVIEWABLE_ANALYSES or not finding.is_current:
                raise FindingReviewError("DESKTOP_REVIEW_UNAVAILABLE")
            old_status = finding.review_status
            updated = session.execute(update(RiskFinding).where(
                RiskFinding.id == finding_id, RiskFinding.version == expected_version,
                RiskFinding.is_current.is_(True),
                RiskFinding.analysis_id.in_(select(AnalysisBatch.id).where(
                    AnalysisBatch.status.in_(REVIEWABLE_ANALYSES),
                )),
            ).values(review_status=status, version=expected_version + 1))
            if updated.rowcount != 1:
                session.refresh(analysis)
                session.refresh(finding)
                code = ("DESKTOP_REVIEW_UNAVAILABLE" if analysis.status not in REVIEWABLE_ANALYSES or not finding.is_current
                        else "DESKTOP_REVIEW_STALE")
                raise FindingReviewError(code)
            # Validate under the write lock already acquired by the versioned
            # update. Any invalid provenance rolls back that update as well.
            if not owned_finding_evidence(session, finding).consistent:
                raise FindingReviewError("DESKTOP_REVIEW_UNAVAILABLE")
            if latest_signature(session, finding) is None:
                record_signature(session, finding, finding_signature(session, finding))
            session.add(FindingReview(
                finding_id=finding_id, actor="local-user", old_status=old_status,
                new_status=status, note=mask_sensitive(note),
            ))
            update_risk_counts(session, analysis)
            session.flush()
            session.expire(finding, ["reviews"])
            # Assemble the response under the same write transaction. A later
            # evaluation cannot make a successful save look like a failed GET.
            payload = finding_detail_payload(session, finding)
            session.commit()
            return payload
