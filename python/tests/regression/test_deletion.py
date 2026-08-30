from pathlib import Path

import pytest
from sqlalchemy import func, select

from qian_labor.database import create_database
from qian_labor.models.core import (
    AIUsageRecord,
    AnalysisBatch,
    DeletionTombstone,
    Employee,
    EmploymentFact,
    RiskFinding,
    SourceLocator,
    UploadedFile,
)
from qian_labor.services.deletion import DeletionService
from qian_labor.storage.local import LocalStorage


def test_delete_is_idempotent_and_removes_sensitive_derivatives(tmp_path: Path) -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    storage = LocalStorage(str(tmp_path / "uploads"))
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构待删除")
        session.add(analysis)
        session.flush()
        uploaded = UploadedFile(
            analysis_id=analysis.id,
            original_filename="虚构名单.csv",
            storage_key=f"analyses/{analysis.id}/fictional.csv",
            mime_type="text/csv",
            extension=".csv",
            size_bytes=10,
            sha256="a" * 64,
        )
        employee = Employee(
            analysis_id=analysis.id,
            masked_name="虚构员*甲",
            normalized_name="虚构员*甲",
            employee_number="F-001",
        )
        session.add_all([uploaded, employee])
        session.flush()
        fact = EmploymentFact(
            analysis_id=analysis.id,
            employee_id=employee.id,
            file_id=uploaded.id,
            fact_type="employment.status",
            value_json="active",
            normalized_value_json="active",
            extraction_method="fixture",
            confidence=1,
            dedupe_key="b" * 64,
        )
        session.add(fact)
        session.flush()
        source = SourceLocator(
            analysis_id=analysis.id,
            file_id=uploaded.id,
            fact_id=fact.id,
            locator_type="cell",
            location={"row": 2},
            excerpt="虚构行",
            content_hash="c" * 64,
        )
        session.add(source)
        session.flush()
        session.add_all(
            [
                RiskFinding(
                    analysis_id=analysis.id,
                    employee_id=employee.id,
                    rule_id="FICTIONAL_RULE",
                    rule_version="cn-labor-mvp-1.0.0",
                    category="data_quality",
                    severity="info",
                    assessment_status="insufficient_data",
                    title="虚构资料不足",
                    summary="资料不足，暂时无法判断",
                    trigger_fact_ids=[fact.id],
                    source_locator_ids=[source.id],
                ),
                AIUsageRecord(
                    analysis_id=analysis.id,
                    file_id=uploaded.id,
                    provider="fake",
                    model="fake",
                    operation="extraction",
                    status="succeeded",
                    idempotency_key="d" * 64,
                ),
            ]
        )
        session.commit()
        analysis_id, storage_key = analysis.id, uploaded.storage_key
    storage.save_bytes(b"fictional", storage_key)

    first = DeletionService(database, str(storage.root)).delete(analysis_id)
    second = DeletionService(database, str(storage.root)).delete(analysis_id)

    assert first["status"] == second["status"] == "deleted"
    assert not storage.exists(storage_key)
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id) is None
        assert session.get(DeletionTombstone, analysis_id) is not None
        for model in (UploadedFile, EmploymentFact, RiskFinding, SourceLocator, AIUsageRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_delete_does_not_commit_tombstone_until_file_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database("sqlite+pysqlite:///:memory:", create_schema=True)
    storage = LocalStorage(str(tmp_path / "uploads"))
    with database.session() as session:
        analysis = AnalysisBatch(name="虚构删除重试")
        session.add(analysis)
        session.flush()
        storage_key = f"analyses/{analysis.id}/fictional-sensitive.csv"
        session.add(
            UploadedFile(
                analysis_id=analysis.id,
                original_filename="虚构敏感材料.csv",
                storage_key=storage_key,
                mime_type="text/csv",
                extension=".csv",
                size_bytes=10,
                sha256="e" * 64,
            )
        )
        session.commit()
        analysis_id = analysis.id
    storage.save_bytes(b"fictional", storage_key)
    raw_path = (storage.root / storage_key).resolve()
    original_unlink = Path.unlink
    failed_once = False

    def fail_once(path: Path, *args, **kwargs):
        nonlocal failed_once
        if path.resolve() == raw_path and not failed_once:
            failed_once = True
            raise OSError("synthetic unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    service = DeletionService(database, str(storage.root))

    with pytest.raises(OSError, match="synthetic unlink failure"):
        service.delete(analysis_id)

    assert raw_path.exists()
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id) is not None
        assert session.get(DeletionTombstone, analysis_id) is None

    assert service.delete(analysis_id)["status"] == "deleted"
    assert not raw_path.exists()
    with database.session() as session:
        assert session.get(AnalysisBatch, analysis_id) is None
        assert session.get(DeletionTombstone, analysis_id) is not None
