from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from qian_labor.ai.providers import AIProviderError
from qian_labor.desktop.app import create_desktop_app
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import AIUsageRecord, AnalysisBatch, ProcessingJob, UploadedFile
from qian_labor.security.local_redaction import PrivacyBoundary
from qian_labor.storage.local import LocalStorage


TOKEN = "desktop-processing-failure-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}
PEPPER = "desktop-processing-failure-pepper-32-characters"


class TimeoutProvider:
    name = "zhipu"
    is_external = True
    batch_budget_usd = 5.0

    def extract(self, _filename: str, _content: bytes):
        raise AIProviderError("AI_TIMEOUT")


def test_all_provider_extractions_failed_preserves_cause_and_never_returns_zero_partial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "selected.csv"
    source.write_text("员工编号,姓名\nF-101,虚构员工甲\n", encoding="utf-8")
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        analysis_id = client.post(
            "/api/analyses",
            headers=HEADERS,
            json={"name": "失败诊断", "company_display_name": "虚构企业"},
        ).json()["id"]
        imported = client.post(
            f"/api/analyses/{analysis_id}/import-paths",
            headers=HEADERS,
            json={"paths": [str(source)]},
        )
        assert imported.status_code == 200

        result = ProcessingPipeline(
            app.state.database,
            LocalStorage(str(app.state.storage_root)),
            TimeoutProvider(),
            privacy_boundary=PrivacyBoundary(PEPPER),
        ).process(analysis_id)

        assert result["status"] == "failed"
        assert result["files"][0]["error_code"] == "AI_TIMEOUT"
        with app.state.database.session() as session:
            analysis = session.get(AnalysisBatch, analysis_id)
            uploaded = session.scalar(
                select(UploadedFile).where(UploadedFile.analysis_id == analysis_id)
            )
            job = session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.analysis_id == analysis_id,
                    ProcessingJob.job_type == "extract",
                )
            )
            usage = session.scalar(
                select(AIUsageRecord).where(AIUsageRecord.analysis_id == analysis_id)
            )

            assert analysis is not None
            assert analysis.status == "failed"
            assert analysis.failure_reason == "AI_TIMEOUT"
            assert uploaded is not None and uploaded.error_code == "AI_TIMEOUT"
            assert job is not None and job.error_code == "AI_TIMEOUT"
            assert usage is not None and usage.status == "failed"
