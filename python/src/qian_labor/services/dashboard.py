from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from math import ceil
from typing import Any

from sqlalchemy import or_, select

from qian_labor.database import Database
from qian_labor.models.core import (
    AnalysisBatch,
    Employee,
    EmploymentFact,
    ProcessingJob,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)
from qian_labor.rules.types import assessment_status_label, normalize_assessment_status
from qian_labor.services.assessment_gate import restrict_findings

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
SEVERITY_LABELS = {"high": "高风险", "medium": "中风险", "low": "低风险", "info": "提示"}
REAL_RISK_STATUSES = {"suspected_risk", "confirmed_anomaly", "requires_human_review"}
REVIEW_STATUS_LABELS = {
    "open": "待处理",
    "reviewed": "已确认",
    "resolved": "已处理",
    "dismissed": "已驳回",
}
MATERIAL_TYPES = (
    ("contract", "劳动合同", {"active"}),
    ("payroll", "工资", {"active"}),
    ("attendance", "考勤", {"active"}),
    ("social_insurance", "社保", {"active"}),
    ("termination", "离职材料", {"terminated"}),
)


class DashboardService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, analysis_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            findings_statement = select(RiskFinding).where(
                RiskFinding.analysis_id == analysis_id,
                RiskFinding.review_status.not_in({"resolved", "dismissed"}),
            )
            findings_statement = restrict_findings(findings_statement, analysis)
            findings = list(session.scalars(findings_statement))
            employee_ids = {item.employee_id for item in findings if item.employee_id}
            employees = {
                employee.id: employee
                for employee in session.scalars(
                    select(Employee).where(Employee.analysis_id == analysis_id)
                )
            }
            coverage_rows = list(
                session.execute(
                    select(
                        EmploymentFact.employee_id,
                        EmploymentFact.fact_type,
                        EmploymentFact.normalized_value_json,
                        EmploymentFact.created_at,
                        SourceLocator.file_id,
                        UploadedFile.classified_kind,
                    )
                    .outerjoin(SourceLocator, SourceLocator.fact_id == EmploymentFact.id)
                    .outerjoin(UploadedFile, UploadedFile.id == SourceLocator.file_id)
                    .where(EmploymentFact.analysis_id == analysis_id)
                )
            )
            classification_pending = (
                session.scalar(
                    select(UploadedFile.id)
                    .outerjoin(ProcessingJob, ProcessingJob.file_id == UploadedFile.id)
                    .where(
                        UploadedFile.analysis_id == analysis_id,
                        UploadedFile.classified_kind == "unknown",
                        or_(
                            UploadedFile.status == "processed",
                            ProcessingJob.status == "succeeded",
                        ),
                    )
                    .limit(1)
                )
                is not None
            )
            risk_findings = [
                item for item in findings if item.assessment_status != "insufficient_data"
            ]
            categories = Counter(item.category for item in findings)
            departments = Counter(
                employees[item.employee_id].department or "未分组"
                for item in findings
                if item.employee_id in employees
            )
            deadline_windows = (7, 30, 60, 90)
            deadline_buckets: Counter[int] = Counter()
            analysis_date = analysis.created_at.date()
            for item in findings:
                if item.due_date is None:
                    continue
                remaining = (item.due_date - analysis_date).days
                if remaining < 0:
                    continue
                for days in deadline_windows:
                    if remaining <= days:
                        deadline_buckets[days] += 1
            priority = sorted(
                findings,
                key=lambda item: (
                    SEVERITY_ORDER.get(item.severity, 9),
                    not item.requires_human_review,
                    item.due_date is None,
                    item.due_date or date.max,
                    item.created_at,
                ),
            )[:10]
            material_coverage = self._material_coverage(
                employees, coverage_rows, classification_pending
            )
            return {
                "analysis_id": analysis.id,
                "company_name": analysis.company_display_name,
                "status": analysis.status,
                "is_demo": analysis.is_demo,
                "summary": {
                    "employee_count": analysis.employee_count or len(employees),
                    "high_count": sum(item.severity == "high" for item in risk_findings),
                    "medium_count": sum(item.severity == "medium" for item in risk_findings),
                    "low_count": sum(item.severity == "low" for item in risk_findings),
                    "insufficient_data_count": sum(
                        item.assessment_status == "insufficient_data" for item in findings
                    ),
                    "coverage_rate": material_coverage["overall"],
                    "affected_employee_count": len(employee_ids),
                    "requires_human_review_count": sum(
                        item.requires_human_review for item in findings
                    ),
                    "deadline_30_count": deadline_buckets[30],
                    "classification_pending": classification_pending,
                },
                "categories": [
                    {"code": code, "label": self._category_label(code), "count": count}
                    for code, count in sorted(categories.items())
                ],
                "departments": [
                    {"name": name, "count": count} for name, count in sorted(departments.items())
                ],
                "material_coverage": material_coverage,
                "deadline_buckets": [
                    {"label": f"未来{days}天", "days": days, "count": deadline_buckets[days]}
                    for days in deadline_windows
                ],
                "priority_findings": [self.finding_payload(item, employees) for item in priority],
            }

    def employees(
        self,
        analysis_id: str,
        *,
        query: str = "",
        department: str | None = None,
        severity: str | None = None,
        insufficient_data: bool | None = None,
        requires_human_review: bool | None = None,
        match_status: str | None = None,
        sort_by: str = "employee_number",
        sort_order: str = "asc",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            all_employees = list(
                session.scalars(select(Employee).where(Employee.analysis_id == analysis_id))
            )
            department_options = sorted(
                {employee.department for employee in all_employees if employee.department}
            )
            finding_statement = restrict_findings(
                select(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.review_status.not_in({"resolved", "dismissed"}),
                ),
                analysis,
            )
            findings_by_employee: dict[str, list[RiskFinding]] = defaultdict(list)
            for finding in session.scalars(finding_statement):
                if finding.employee_id:
                    findings_by_employee[finding.employee_id].append(finding)
            coverage_rows = list(
                session.execute(
                    select(
                        EmploymentFact.employee_id,
                        EmploymentFact.fact_type,
                        EmploymentFact.normalized_value_json,
                        EmploymentFact.created_at,
                        SourceLocator.file_id,
                        UploadedFile.classified_kind,
                    )
                    .outerjoin(SourceLocator, SourceLocator.fact_id == EmploymentFact.id)
                    .outerjoin(UploadedFile, UploadedFile.id == SourceLocator.file_id)
                    .where(EmploymentFact.analysis_id == analysis_id)
                )
            )
            employees_by_id = {employee.id: employee for employee in all_employees}
            employee_statuses, covered_materials = self._coverage_state(
                employees_by_id, coverage_rows
            )
            items = [
                employee
                for employee in all_employees
                if (
                    not query
                    or query in employee.masked_name
                    or (employee.employee_number is not None and query in employee.employee_number)
                )
                and (department is None or employee.department == department)
                and (match_status is None or employee.match_status == match_status)
            ]
            output = []
            for employee in items:
                findings = findings_by_employee[employee.id]
                real_risk_findings = [
                    item for item in findings if item.assessment_status in REAL_RISK_STATUSES
                ]
                risk_counts = {
                    level: sum(item.severity == level for item in real_risk_findings)
                    for level in ("high", "medium")
                }
                if severity and risk_counts[severity] == 0:
                    continue
                insufficient_count = sum(
                    item.assessment_status == "insufficient_data" for item in findings
                )
                review_count = sum(
                    item.requires_human_review
                    and item.review_status not in {"resolved", "dismissed"}
                    for item in findings
                )
                if insufficient_data is not None and (insufficient_count > 0) != insufficient_data:
                    continue
                if (
                    requires_human_review is not None
                    and (review_count > 0) != requires_human_review
                ):
                    continue
                output.append(
                    {
                        "id": employee.id,
                        "employee_number": employee.employee_number,
                        "masked_name": employee.masked_name,
                        "department": employee.department or "未分组",
                        "job_title": employee.job_title,
                        "employment_status": employee_statuses[employee.id],
                        "match_status": employee.match_status,
                        "risk_counts": risk_counts,
                        "insufficient_data_count": insufficient_count,
                        "requires_human_review_count": review_count,
                        "material_coverage": self._employee_coverage(
                            employee_statuses[employee.id], covered_materials[employee.id]
                        ),
                    }
                )
            output.sort(key=lambda item: (item["employee_number"] or "", item["id"]))
            sort_keys = {
                "employee_number": lambda item: item["employee_number"] or "",
                "masked_name": lambda item: item["masked_name"],
                "department": lambda item: item["department"],
                "high_count": lambda item: item["risk_counts"].get("high", 0),
                "medium_count": lambda item: item["risk_counts"].get("medium", 0),
                "insufficient_data_count": lambda item: item["insufficient_data_count"],
                "requires_human_review_count": lambda item: item["requires_human_review_count"],
                "material_coverage": lambda item: item["material_coverage"],
            }
            output.sort(key=sort_keys[sort_by], reverse=sort_order == "desc")
            size = min(max(page_size, 1), 100)
            start = (max(page, 1) - 1) * size
            total = len(output)
            return {
                "items": output[start : start + size],
                "total": total,
                "page": page,
                "page_size": size,
                "pages": ceil(total / size) if total else 0,
                "department_options": department_options,
            }

    def employee_detail(self, analysis_id: str, employee_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            employee = session.get(Employee, employee_id)
            if analysis is None or employee is None or employee.analysis_id != analysis_id:
                raise KeyError(employee_id)
            statement = restrict_findings(
                select(RiskFinding).where(
                    RiskFinding.analysis_id == analysis_id,
                    RiskFinding.employee_id == employee_id,
                ),
                analysis,
            )
            findings = list(session.scalars(statement.order_by(RiskFinding.created_at.desc())))
            return {
                "employee": {
                    "id": employee.id,
                    "employee_number": employee.employee_number,
                    "masked_name": employee.masked_name,
                    "department": employee.department or "未分组",
                    "job_title": employee.job_title,
                    "employment_status": self._employee_status(employee),
                    "match_status": employee.match_status,
                },
                "findings": [
                    self.finding_payload(item, {employee.id: employee}) for item in findings
                ],
            }

    def findings(self, analysis_id: str, **filters: str | None) -> list[dict[str, Any]]:
        with self.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            if analysis is None:
                raise KeyError(analysis_id)
            statement = select(RiskFinding).where(RiskFinding.analysis_id == analysis_id)
            statement = restrict_findings(statement, analysis)
            for field in ("category", "severity", "assessment_status", "employee_id"):
                if filters.get(field):
                    statement = statement.where(getattr(RiskFinding, field) == filters[field])
            employees = {
                item.id: item
                for item in session.scalars(
                    select(Employee).where(Employee.analysis_id == analysis_id)
                )
            }
            return [
                self.finding_payload(item, employees)
                for item in session.scalars(statement.order_by(RiskFinding.created_at.desc()))
            ]

    @staticmethod
    def finding_payload(item: RiskFinding, employees: dict[str, Employee]) -> dict[str, Any]:
        employee = employees.get(item.employee_id or "")
        return {
            "id": item.id,
            "rule_id": item.rule_id,
            "title": item.title,
            "summary": item.summary,
            "category": item.category,
            "severity": item.severity,
            "severity_label": SEVERITY_LABELS.get(item.severity, "未知等级"),
            "assessment_status": normalize_assessment_status(item.assessment_status),
            "status_label": assessment_status_label(item.assessment_status),
            "requires_human_review": item.requires_human_review,
            "review_status": item.review_status,
            "review_status_label": REVIEW_STATUS_LABELS.get(item.review_status, "复核状态待确认"),
            "employee_id": item.employee_id,
            "employee_name": employee.masked_name if employee else "批次级事项",
            "department": employee.department if employee else None,
            "due_date": item.due_date.isoformat() if item.due_date else None,
        }

    @staticmethod
    def _material_coverage(
        employees: dict[str, Employee],
        coverage_rows: list[Any],
        classification_pending: bool = False,
    ) -> dict[str, Any]:
        employee_statuses, covered_materials = DashboardService._coverage_state(
            employees, coverage_rows
        )

        items: list[dict[str, Any]] = []
        total_covered = 0
        total_applicable = 0
        for code, label, statuses in MATERIAL_TYPES:
            applicable_ids = [
                employee.id
                for employee in employees.values()
                if employee_statuses[employee.id] in statuses
            ]
            covered = sum(code in covered_materials[employee_id] for employee_id in applicable_ids)
            applicable = len(applicable_ids)
            total_covered += covered
            total_applicable += applicable
            item_classification_pending = classification_pending and applicable > 0
            items.append(
                {
                    "code": code,
                    "label": label,
                    "covered": covered,
                    "applicable": applicable,
                    "rate": round(covered / applicable, 4) if applicable else 0.0,
                    "not_applicable": applicable == 0,
                    "classification_pending": item_classification_pending,
                    "status": (
                        "not_applicable"
                        if applicable == 0
                        else "classification_pending"
                        if item_classification_pending
                        else "complete"
                    ),
                }
            )
        return {
            "overall": round(total_covered / total_applicable, 4) if total_applicable else 0.0,
            "classification_pending": classification_pending,
            "status": "classification_pending" if classification_pending else "complete",
            "items": items,
        }

    @staticmethod
    def _coverage_state(
        employees: dict[str, Employee], coverage_rows: list[Any]
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        employee_statuses = {
            employee_id: employee.employment_status for employee_id, employee in employees.items()
        }
        status_fact_dates: dict[str, Any] = {}
        evidence_by_file: dict[tuple[str, str, str], list[tuple[str, Any]]] = {}
        for row in coverage_rows:
            employee_id = row.employee_id
            if employee_id not in employees:
                continue
            if row.fact_type == "employment.status" and (
                employee_id not in status_fact_dates
                or row.created_at >= status_fact_dates[employee_id]
            ):
                employee_statuses[employee_id] = str(row.normalized_value_json)
                status_fact_dates[employee_id] = row.created_at
            if row.file_id and row.classified_kind:
                evidence_by_file.setdefault(
                    (employee_id, row.classified_kind, row.file_id), []
                ).append((row.fact_type, row.normalized_value_json))

        covered_materials: dict[str, set[str]] = {employee_id: set() for employee_id in employees}
        supported_codes = {code for code, _label, _statuses in MATERIAL_TYPES}
        for (employee_id, classified_kind, _file_id), facts in evidence_by_file.items():
            if classified_kind not in supported_codes:
                continue
            required_presence_fact = {
                "contract": "employment.contract.exists",
                "social_insurance": "employment.social_insurance.present",
                "attendance": "employment.attendance.present",
            }.get(classified_kind)
            presence_values = [
                value for fact_type, value in facts if fact_type == required_presence_fact
            ]
            if required_presence_fact and not any(value is True for value in presence_values):
                continue
            covered_materials[employee_id].add(classified_kind)
        return employee_statuses, covered_materials

    @staticmethod
    def _employee_coverage(employment_status: str, covered_materials: set[str]) -> float:
        applicable_codes = {
            code for code, _label, statuses in MATERIAL_TYPES if employment_status in statuses
        }
        if not applicable_codes:
            return 0
        return round(len(applicable_codes & covered_materials) / len(applicable_codes), 4)

    @staticmethod
    def _employee_status(employee: Employee) -> str:
        status_facts = [fact for fact in employee.facts if fact.fact_type == "employment.status"]
        if not status_facts:
            return employee.employment_status
        latest = max(status_facts, key=lambda fact: fact.created_at)
        return str(latest.normalized_value_json)

    @staticmethod
    def _category_label(code: str) -> str:
        return {
            "contract": "劳动合同",
            "probation": "试用期",
            "payroll": "工资",
            "social_insurance": "社会保险",
            "attendance": "考勤与加班",
            "termination": "离职与终止",
            "data_quality": "资料完整度",
        }.get(code, code)
