"""Excel retry/cache regressions using isolated SQLite and a counted provider."""
import hashlib
from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from qian_labor.ai.schemas import ExtractionResult
from qian_labor.desktop.app import create_desktop_app
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import AuditEvent, EmploymentFact, ProcessingJob, SourceLocator, UploadedFile
from qian_labor.storage.local import LocalStorage


class CountedProvider:
    name, is_external = "fake", False

    def __init__(self):
        self.calls = 0
        self.located = False

    def extract(self, filename, content):
        self.calls += 1
        return ExtractionResult.model_validate({
            "employee_number": "SYN-001", "facts": [{
                "employee_id": "SYN-001", "fact_type": "employment.probation.start_date",
                "value": "2026-01-01", "confidence": 1,
                "source": {"file_name": filename, "sheet": "Sheet", "row": 2,
                           "column": "2", "excerpt": "2026-01-01" if self.located else ""},
            }],
        })


@pytest.fixture
def excel_case(tmp_path):
    token = "synthetic-excel-cache-token"
    app = create_desktop_app(data_dir=tmp_path / "app", launch_token=token)
    with TestClient(app, headers={"X-Qian-Desktop-Token": token}) as client:
        aid = client.post("/api/analyses", json={
            "name": "synthetic retry", "company_display_name": "fictional",
        }).json()["id"]
        book = Workbook()
        book.active.append(["员工编号", "试用期开始"])
        book.active.append(["SYN-001", "2026-01-01"])
        content = BytesIO()
        book.save(content)
        path = tmp_path / "synthetic.xlsx"
        path.write_bytes(content.getvalue())
        client.post(f"/api/analyses/{aid}/import-paths", json={"paths": [str(path)]}).raise_for_status()
        db = app.state.database
        with db.session() as session:
            fid = session.scalar(select(UploadedFile).where(UploadedFile.analysis_id == aid)).id
        provider = CountedProvider()
        pipeline = ProcessingPipeline(db, LocalStorage(str(app.state.storage_root)), provider)
        yield client, db, aid, fid, provider, pipeline


def test_successful_retry_reuses_cache_without_deleting_old_unlocated_proof(excel_case):
    client, db, aid, fid, provider, pipeline = excel_case
    pipeline._process_file(aid, fid)
    assert provider.calls == 1
    with db.session() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.analysis_id == aid,
            AuditEvent.event_type == 'extraction_grounding_completed'))
        assert event.metadata_json['failure_counts'] == {'source_missing': 1}
        assert event.metadata_json['first_failure'] == {'reason': 'source_missing'}
        old = session.scalar(select(SourceLocator).where(SourceLocator.file_id == fid))
        old_id, old_location, old_excerpt = old.id, deepcopy(old.location), old.excerpt
        assert not pipeline.cached_extraction(session, aid, session.get(UploadedFile, fid), provider)
    provider.located = True
    pipeline._process_file(aid, fid)
    assert provider.calls == 2
    pipeline._process_file(aid, fid)
    assert provider.calls == 2
    assert client.get(f"/api/analyses/{aid}/workspace").json()["files"][0]["needs_reextraction"] is False
    assert client.get(f"/api/analyses/{aid}/report").status_code == 200
    with db.session() as session:
        old = session.get(SourceLocator, old_id)
        assert (old.location, old.excerpt) == (old_location, old_excerpt)
        from qian_labor.services.effective_facts import fact_projection
        from qian_labor.services.source_provenance import uncertain_grounded_fact_ids
        fact = session.scalar(select(EmploymentFact).where(EmploymentFact.file_id == fid))
        projected = fact_projection(session, fact)
        assert projected.verification_status == 'unverified'
        assert len(projected.state.sources) == 1
        assert projected.state.sources[0].location['_grounding']['status'] == 'locally_located'
        assert fact.id not in uncertain_grounded_fact_ids(session, aid)
        # This cache decision must survive a new pipeline instance/session.
        restarted = ProcessingPipeline(db, pipeline.storage, provider)
        assert restarted.cached_extraction(session, aid, session.get(UploadedFile, fid), provider)
    assert provider.calls == 2


def test_legacy_current_success_ignores_old_version_unlocated_sources(excel_case):
    client, db, aid, fid, provider, pipeline = excel_case
    provider.located = True
    pipeline._process_file(aid, fid)
    with db.session() as session:
        # Simulate a successful pre-fix installation without new completion metadata.
        for event in session.scalars(select(AuditEvent).where(AuditEvent.analysis_id == aid)):
            session.delete(event)
        original = session.scalar(select(EmploymentFact).where(EmploymentFact.file_id == fid))
        old = EmploymentFact(analysis_id=aid, file_id=fid, employee_id=original.employee_id,
            fact_type=original.fact_type, value_json=original.value_json,
            normalized_value_json=original.normalized_value_json, extraction_method="fake",
            confidence=1, verification_status="needs_human_confirmation", dedupe_key=str(uuid4()))
        session.add(old)
        session.flush()
        session.add(SourceLocator(analysis_id=aid, file_id=fid, fact_id=old.id,
            locator_type="document", location={"_grounding": {
                "version": "parser-grounding-v2", "status": "unlocated_needs_review"}},
            excerpt="", content_hash=hashlib.sha256(b"").hexdigest()))
        session.commit()
        assert pipeline.cached_extraction(session, aid, session.get(UploadedFile, fid), provider)
    pipeline._process_file(aid, fid)
    assert provider.calls == 1


def test_repeated_unlocated_result_is_still_explicitly_retryable(excel_case):
    client, db, aid, fid, provider, pipeline = excel_case
    pipeline._process_file(aid, fid)
    pipeline._process_file(aid, fid)
    assert provider.calls == 2
    assert client.get(f"/api/analyses/{aid}/workspace").json()["files"][0]["needs_reextraction"] is True
    assert provider.calls == 2  # Reading the warning does not initiate a request.


def test_current_source_attempt_keeps_latest_failure_and_same_attempt_ambiguity(excel_case):
    _, db, aid, fid, provider, pipeline = excel_case
    from qian_labor.services.source_provenance import current_source_attempt, grounding_requires_review
    pipeline._process_file(aid, fid)
    provider.located = True
    pipeline._process_file(aid, fid)
    with db.session() as session:
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.file_id == fid, ProcessingJob.job_type == 'extract'))
        job.status = 'failed'
        session.commit()
    provider.located = False
    pipeline._process_file(aid, fid)
    with db.session() as session:
        sources = list(session.scalars(select(SourceLocator).where(SourceLocator.file_id == fid)))
        assert len(sources) == 2, 'Identical historical evidence must be reused, not duplicated'
        selected = current_source_attempt(session, sources)
        assert len(selected) == 1 and grounding_requires_review(selected[0].location)


@pytest.mark.parametrize("stale_field,stale_value", [("attempt", 0), ("job_key", "other-file-key")])
def test_unrelated_completion_cannot_hide_current_unlocated_result(excel_case, stale_field, stale_value):
    client, db, aid, fid, provider, pipeline = excel_case
    pipeline._process_file(aid, fid)
    with db.session() as session:
        job = session.scalar(select(ProcessingJob).where(
            ProcessingJob.file_id == fid, ProcessingJob.job_type == "extract"))
        metadata = {"file_id": fid, "job_key": job.unique_key, "attempt": job.attempts,
                    "unlocated_count": 0, stale_field: stale_value}
        session.add(AuditEvent(analysis_id=aid, event_type="extraction_grounding_completed", metadata_json=metadata))
        session.commit()
        assert not pipeline.cached_extraction(session, aid, session.get(UploadedFile, fid), provider)
    assert client.get(f"/api/analyses/{aid}/workspace").json()["files"][0]["needs_reextraction"] is True
