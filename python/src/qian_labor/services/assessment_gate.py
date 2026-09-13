from typing import Any

from sqlalchemy import Select

from qian_labor.models.core import AnalysisBatch, RiskFinding
from qian_labor.services.assessment_scope import validate_profile

_GATED_STATUSES = {
    "created",
    "uploading",
    "uploaded",
    "queued",
    "parsing",
    "extracting",
    "matching_review",
    "evaluating",
    "cancelled",
    "interrupted",
    "deleting",
    "deleted",
}


def _is_gated(analysis: AnalysisBatch) -> bool:
    validate_profile(analysis.assessment_profile)
    return analysis.status in _GATED_STATUSES


def restrict_findings(statement: Select[Any], analysis: AnalysisBatch, *, current: bool = True) -> Select[Any]:
    statement = statement.where(RiskFinding.is_current.is_(current))
    if _is_gated(analysis):
        return statement.where(RiskFinding.category == "data_quality")
    return statement


def ensure_finding_access(session: Any, finding: RiskFinding) -> None:
    analysis = session.get(AnalysisBatch, finding.analysis_id)
    if analysis is None or (_is_gated(analysis) and finding.category != "data_quality"):
        raise KeyError(finding.id)
