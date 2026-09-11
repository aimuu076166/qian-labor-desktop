import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from qian_labor.desktop.app import create_desktop_app
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import AnalysisBatch, ProcessingJob, UploadedFile
from qian_labor.ai.schemas import EmploymentFact, ExtractionResult, SourceLocator
from qian_labor.security.local_redaction import PrivacyBoundaryError
from qian_labor.storage.local import LocalStorage


def test_local_privacy_failure_is_actionable_and_never_calls_provider(tmp_path):
    class RejectingBoundary:
        def prepare(self, *args, **kwargs):
            raise PrivacyBoundaryError("synthetic private detail must not leave the boundary")

    class Provider:
        name = "synthetic-external"
        is_external = True
        calls = 0

        def extract(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("provider must not receive an unredacted image")

    image_path = tmp_path / "synthetic-scan.png"
    Image.new("RGB", (160, 80), "white").save(image_path)
    headers = {"X-Qian-Desktop-Token": "synthetic-local-privacy-failure"}
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=headers["X-Qian-Desktop-Token"])
    with TestClient(app) as client:
        analysis_id = client.post("/api/analyses", headers=headers, json={
            "name": "虚构隐私失败测试", "company_display_name": "虚构企业",
        }).json()["id"]
        assert client.post(f"/api/analyses/{analysis_id}/import-paths", headers=headers,
                           json={"paths": [str(image_path)]}).status_code == 200
        provider = Provider()
        result = ProcessingPipeline(app.state.database, LocalStorage(str(app.state.storage_root)),
                                    provider, privacy_boundary=RejectingBoundary()).process(analysis_id)
        assert provider.calls == 0
        assert result["status"] == "failed"
        assert result["files"][0]["error_code"] == "AI_LOCAL_REDACTION_FAILED"
        assert result["files"][0]["error_diagnostic"] == {"category": "privacy"}
        with app.state.database.session() as session:
            assert session.get(AnalysisBatch, analysis_id).failure_reason == "AI_LOCAL_REDACTION_FAILED"
        assert "synthetic private detail" not in str(result)


@pytest.mark.parametrize("details", [{}, {
    "fact_index": 1, "fact_type": "employment.probation.assessment_exists",
    "actual_value_type": "text", "expected_value_types": ("boolean", "null"),
}])
def test_provider_diagnostic_is_persisted_as_safe_workspace_metadata(tmp_path, details):
    from qian_labor.ai.providers import AIDiagnostic, AIProviderError
    failure = {"category": "semantic", "path": "facts[].value_type", "validation_type": "invalid_value"} if details else {
        "category": "json", "path": "response", "validation_type": "invalid_json"}

    class Provider:
        name = "synthetic-external"
        is_external = True

        def extract(self, *args, **kwargs):
            raise AIProviderError("AI_SCHEMA_INVALID", AIDiagnostic(
                attempt=1, **failure, **details,
            ))

    token = "synthetic-provider-diagnostic"
    headers = {"X-Qian-Desktop-Token": token}
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=token)
    source = tmp_path / "synthetic.csv"
    source.write_text("员工,事实\nSYN-001,合成合同\n", encoding="utf-8")
    with TestClient(app) as client:
        aid = client.post("/api/analyses", headers=headers, json={
            "name": "合成诊断", "company_display_name": "虚构企业",
        }).json()["id"]
        client.post(f"/api/analyses/{aid}/import-paths", headers=headers,
                    json={"paths": [str(source)]})
        result = ProcessingPipeline(app.state.database, LocalStorage(str(app.state.storage_root)), Provider()).process(aid)
        diagnostic = result["files"][0]["error_diagnostic"]
        expected = {"attempt": 1, **failure, **details}
        if "expected_value_types" in expected:
            expected["expected_value_types"] = list(expected["expected_value_types"])
        assert diagnostic == expected
        assert "SYN-001" not in str(diagnostic)
        workspace = client.get(f"/api/analyses/{aid}/workspace", headers=headers).json()
        assert workspace["files"][0]["error_diagnostic"] == diagnostic


@pytest.mark.parametrize("terminal_status", ["completed", "partial", "failed"])
def test_explicit_analysis_retry_reextracts_legacy_empty_success_but_keeps_valid_cache(tmp_path, terminal_status):
    class CountingProvider:
        name = "synthetic-controlled"
        is_external = False
        calls = 0

        def extract(self, filename, content):
            self.calls += 1
            return ExtractionResult(document_type="roster", employee_number="SYN-001", facts=[
                EmploymentFact(employee_id="SYN-001", fact_type="employment.status", value="active",
                               confidence=1, source=SourceLocator(file_name=filename, row=2, excerpt="SYN-001 在职")),
            ])

    source = tmp_path / "synthetic.csv"
    source.write_text("工号,状态\nSYN-001,在职\n", encoding="utf-8")
    token = "synthetic-legacy-empty-retry"
    headers = {"X-Qian-Desktop-Token": token}
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=token)
    with TestClient(app) as client:
        analysis_id = client.post("/api/analyses", headers=headers, json={
            "name": "虚构旧空结果", "company_display_name": "虚构企业",
        }).json()["id"]
        client.post(f"/api/analyses/{analysis_id}/import-paths", headers=headers, json={"paths": [str(source)]})
        with app.state.database.session() as session:
            file = session.scalar(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id))
            file.status = "processed"
            session.add(ProcessingJob(analysis_id=analysis_id, file_id=file.id, job_type="extract",
                input_hash=file.sha256, unique_key=ProcessingPipeline._job_key(analysis_id, file.id, "extract", file.sha256),
                status="succeeded", attempts=1))
            session.get(AnalysisBatch, analysis_id).status = terminal_status
            session.commit()
        material = client.get(f"/api/analyses/{analysis_id}/workspace", headers=headers).json()["files"][0]
        assert material["fact_count"] == 0
        assert material["error_code"] == "AI_NO_SUPPORTED_FACTS"
        provider = CountingProvider()
        pipeline = ProcessingPipeline(app.state.database, LocalStorage(str(app.state.storage_root)), provider)
        app.state.processing_queue.pipeline_factory = lambda: pipeline
        assert client.post(f"/api/analyses/{analysis_id}/process", headers=headers).status_code == 202
        # A barrier on the real single-worker executor waits for the submitted API job.
        app.state.processing_queue._executor.submit(lambda: None).result(timeout=5)
        assert provider.calls == 1
        material = client.get(f"/api/analyses/{analysis_id}/workspace", headers=headers).json()["files"][0]
        assert material["fact_count"] == 1 and material["error_code"] is None
        assert material["error_diagnostic"] is None
        processing = client.get(f"/api/analyses/{analysis_id}/processing", headers=headers).json()
        assert processing["files"][0]["error_diagnostic"] is None
        with app.state.database.session() as session:
            session.get(AnalysisBatch, analysis_id).status = "completed"
            session.commit()
        assert client.post(f"/api/analyses/{analysis_id}/process", headers=headers).status_code == 202
        app.state.processing_queue._executor.submit(lambda: None).result(timeout=5)
        assert provider.calls == 1  # An explicit retry must not re-bill already valid material.
