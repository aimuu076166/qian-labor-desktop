from __future__ import annotations

from datetime import UTC, datetime
from contextlib import nullcontext
from typing import Any

from sqlalchemy import select, text

from qian_labor.database import Database
from qian_labor.security.filenames import display_filename
from qian_labor.models.core import RiskFinding, CompanyAnalysisBinding
from qian_labor.services.dashboard import DashboardService
from qian_labor.services.finding_sources import owned_finding_evidence
from qian_labor.services.source_provenance import projected_source


class ReportService:
    """Build a read-only report from the same facts used by the desktop views."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, analysis_id: str, *, session=None) -> dict[str, Any]:
        if session is None:
            with self.database.session() as owned_session:
                # SQLite's legacy driver does not BEGIN for SELECT automatically.
                owned_session.execute(text("BEGIN"))
                return self.get(analysis_id, session=owned_session)
        dashboard = DashboardService(self.database, session=session)
        overview = dashboard.get(analysis_id)
        findings = dashboard.findings(analysis_id)

        employees: list[dict[str, Any]] = []
        page = 1
        while True:
            employee_page = dashboard.employees(analysis_id, page=page, page_size=100)
            employees.extend(employee_page["items"])
            if page >= employee_page["pages"]:
                break
            page += 1

        sources_by_finding = self._source_references(
            analysis_id, {str(item["id"]) for item in findings}, session=session
        )
        report_findings = [
            {**item, "sources": sources_by_finding.get(str(item["id"]), [])}
            for item in findings
        ]
        return {
            "analysis_id": analysis_id,
            "assessment_scope": overview["assessment_scope"],
            "assessment_revision": overview["assessment_revision"],
            "company_name": overview["company_name"],
            "generated_at": datetime.now(UTC).isoformat(),
            "status": overview["status"],
            "report_status": "draft",
            "is_demo": overview["is_demo"],
            "summary": overview["summary"],
            "material_coverage": overview["material_coverage"],
            "categories": overview["categories"],
            "departments": overview["departments"],
            "employees": employees,
            "findings": report_findings,
        }

    def _source_references(
        self, analysis_id: str, finding_ids: set[str], *, session=None
    ) -> dict[str, list[dict[str, Any]]]:
        if not finding_ids:
            return {}
        with nullcontext(session) if session is not None else self.database.session() as session:
            findings = list(
                session.scalars(
                    select(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.is_current.is_(True),
                        RiskFinding.id.in_(finding_ids),
                    )
                )
            )
            result: dict[str, list[dict[str, Any]]] = {}
            from qian_labor.services.effective_facts import effective_projection
            owner = session.get(CompanyAnalysisBinding, analysis_id)
            current = bool(owner and owner.role == "current")
            effective = {r.id: r for r in effective_projection(session, analysis_id)} if current else {}
            for finding in findings:
                references = []
                for source, uploaded in owned_finding_evidence(session, finding).sources:
                    references.append(
                        {
                            "file_name": display_filename(uploaded.original_filename),
                            **projected_source(source),
                            **({"human_confirmed": bool(source.fact_id in effective and effective[source.fact_id].human_confirmed)} if current else {}),
                        }
                    )
                result[finding.id] = references
            return result
