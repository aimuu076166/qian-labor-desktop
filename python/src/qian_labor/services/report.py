from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from qian_labor.database import Database
from qian_labor.models.core import RiskFinding, SourceLocator, UploadedFile
from qian_labor.services.dashboard import DashboardService


class ReportService:
    """Build a read-only report from the same facts used by the desktop views."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, analysis_id: str) -> dict[str, Any]:
        dashboard = DashboardService(self.database)
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
            analysis_id, {str(item["id"]) for item in findings}
        )
        report_findings = [
            {**item, "sources": sources_by_finding.get(str(item["id"]), [])}
            for item in findings
        ]
        return {
            "analysis_id": analysis_id,
            "company_name": overview["company_name"],
            "generated_at": datetime.now(UTC).isoformat(),
            "status": overview["status"],
            "is_demo": overview["is_demo"],
            "summary": overview["summary"],
            "material_coverage": overview["material_coverage"],
            "categories": overview["categories"],
            "departments": overview["departments"],
            "employees": employees,
            "findings": report_findings,
        }

    def _source_references(
        self, analysis_id: str, finding_ids: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not finding_ids:
            return {}
        with self.database.session() as session:
            findings = list(
                session.scalars(
                    select(RiskFinding).where(
                        RiskFinding.analysis_id == analysis_id,
                        RiskFinding.id.in_(finding_ids),
                    )
                )
            )
            source_ids = {
                source_id
                for finding in findings
                for source_id in (finding.source_locator_ids or [])
            }
            sources = {
                source.id: source
                for source in session.scalars(
                    select(SourceLocator).where(SourceLocator.id.in_(source_ids))
                )
            }
            file_ids = {source.file_id for source in sources.values()}
            files = {
                uploaded.id: uploaded
                for uploaded in session.scalars(
                    select(UploadedFile).where(UploadedFile.id.in_(file_ids))
                )
            }
            result: dict[str, list[dict[str, Any]]] = {}
            for finding in findings:
                references = []
                for source_id in finding.source_locator_ids or []:
                    source = sources.get(source_id)
                    uploaded = files.get(source.file_id) if source else None
                    if source is None or uploaded is None:
                        continue
                    references.append(
                        {
                            "file_name": uploaded.original_filename,
                            "locator_type": source.locator_type,
                            "location": source.location,
                        }
                    )
                result[finding.id] = references
            return result
