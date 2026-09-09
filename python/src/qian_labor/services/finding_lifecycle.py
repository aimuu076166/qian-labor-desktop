from typing import Literal

from sqlalchemy import Select, update
from sqlalchemy.orm import Session

from qian_labor.models.core import AuditEvent, RiskFinding, utcnow
from qian_labor.services.finding_review import FindingReviewError


def retire_findings(session: Session, statement: Select[tuple[RiskFinding]], *,
                    reason: Literal["matching_gate", "rule_version_changed", "no_longer_triggered"]) -> None:
    """Retire current results without labelling them resolved or erasing reviews."""
    for finding in session.scalars(statement.where(RiskFinding.is_current.is_(True))):
        version = finding.version
        updated = session.execute(update(RiskFinding).where(
            RiskFinding.id == finding.id, RiskFinding.version == version, RiskFinding.is_current.is_(True),
        ).values(is_current=False, retired_at=utcnow(), version=version + 1))
        if updated.rowcount != 1:
            raise FindingReviewError("DESKTOP_REVIEW_STALE")
        session.add(AuditEvent(analysis_id=finding.analysis_id, event_type="finding_retired", actor="system",
            metadata_json={"finding_id": finding.id, "version": version + 1, "reason": reason}))
