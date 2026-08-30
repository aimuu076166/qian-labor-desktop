from typing import Any

from sqlalchemy import Select

from qian_labor.models.core import AnalysisBatch, RiskFinding

_GATED_STATUSES = {
    "created",
    "uploading",
    "uploaded",
    "queued",
    "parsing",
    "extracting",
    "matching_review",
    "evaluating",
    "deleting",
    "deleted",
}


def _is_gated(analysis: AnalysisBatch) -> bool:
    return analysis.status in _GATED_STATUSES


def restrict_findings(statement: Select[Any], analysis: AnalysisBatch) -> Select[Any]:
    if _is_gated(analysis):
        return statement.where(RiskFinding.category == "data_quality")
    return statement


def ensure_finding_access(session: Any, finding: RiskFinding) -> None:
    analysis = session.get(AnalysisBatch, finding.analysis_id)
    if analysis is None or (_is_gated(analysis) and finding.category != "data_quality"):
        raise KeyError(finding.id)
