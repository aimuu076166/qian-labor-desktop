from __future__ import annotations

from typing import Any

from qian_labor.database import Database
from qian_labor.domain.enums import ALLOWED_TRANSITIONS, AnalysisStatus
from qian_labor.models.core import AnalysisBatch


class InvalidAnalysisTransition(Exception):
    def __init__(self, current: AnalysisStatus, target: AnalysisStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"{current}->{target}")


def validate_transition(current: AnalysisStatus, target: AnalysisStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidAnalysisTransition(current, target)


class AnalysisService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, name: str, company_display_name: str, is_demo: bool = False) -> AnalysisBatch:
        with self.database.session() as session:
            item = AnalysisBatch(
                name=name.strip(),
                company_display_name=company_display_name.strip(),
                is_demo=is_demo,
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def get(self, analysis_id: str) -> AnalysisBatch:
        with self.database.session() as session:
            item = session.get(AnalysisBatch, analysis_id)
            if item is None:
                raise KeyError(analysis_id)
            session.expunge(item)
            return item

    def transition(self, analysis_id: str, target: AnalysisStatus | str) -> AnalysisBatch:
        target_status = AnalysisStatus(target)
        with self.database.session() as session:
            item = session.get(AnalysisBatch, analysis_id)
            if item is None:
                raise KeyError(analysis_id)
            current = AnalysisStatus(item.status)
            validate_transition(current, target_status)
            item.status = target_status.value
            item.current_stage = target_status.value
            item.version += 1
            session.commit()
            session.refresh(item)
            return item

    @staticmethod
    def payload(item: AnalysisBatch) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "company_display_name": item.company_display_name,
            "status": item.status,
            "file_count": item.file_count,
            "employee_count": item.employee_count,
            "high_count": item.high_count,
            "medium_count": item.medium_count,
            "low_count": item.low_count,
            "insufficient_data_count": item.insufficient_data_count,
            "coverage_rate": item.coverage_rate,
            "progress": item.progress,
            "current_stage": item.current_stage,
            "is_demo": item.is_demo,
            "purge_at": item.purge_at.isoformat(),
            "created_at": item.created_at.isoformat(),
            "version": item.version,
        }
