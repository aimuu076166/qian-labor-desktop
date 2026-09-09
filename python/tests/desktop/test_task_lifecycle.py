"""Owned desktop task cancellation: actual API, SQLite and pipeline, synthetic only."""
from threading import Event
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from qian_labor.ai.providers import FakeAIProvider
from qian_labor.desktop.app import create_desktop_app
from qian_labor.models.core import AIUsageRecord, AnalysisBatch, EmploymentFact, ProcessingJob, TaskRun, TaskRequest

TOKEN = "synthetic-task-token"


def request_body(run=None, company_id=None):
    return {"request_id": str(uuid4()), "company_id": company_id,
            "expected_run_id": run["id"] if run else None,
            "expected_version": run["version"] if run else None}


def imported(client, tmp_path, count=3):
    aid = client.post("/api/analyses", json={"name": "合成取消测试"}).json()["id"]
    paths = []
    for number in range(count):
        path = tmp_path / f"synthetic-{number}.csv"
        path.write_text(f"员工编号,姓名\nSYN-{number:03d},完全虚构员工\n")
        paths.append(str(path))
    response = client.post(f"/api/analyses/{aid}/import-paths", json={"paths": paths})
    assert response.status_code == 200, response.text
    return aid, [item["id"] for item in response.json()["files"]]


def test_cancel_inflight_preserves_completed_file_and_explicit_resume_reuses_cache(tmp_path, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    class GatedProvider(FakeAIProvider):
        def __init__(self):
            self.calls = []
        def extract(self, filename, content):
            self.calls.append(filename)
            if len(self.calls) == 2:
                entered.set()
                assert release.wait(10)
            return super().extract(filename, content)
    provider = GatedProvider()
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        aid, files = imported(client, tmp_path)
        base = f"/api/analyses/{aid}/task"
        body = request_body()
        start = client.post(base + "/start", json=body)
        assert start.status_code == 202, start.text
        try:
            assert entered.wait(5)
            run = client.get(base).json()["run"]
            assert run["state"] == "running" and run["in_flight"]
            cancel_body = request_body(run)
            cancel = client.post(base + "/cancel", json=cancel_body)
            assert cancel.status_code == 202, cancel.text
            assert cancel.json()["run"]["state"] == "cancel_requested"
            assert cancel.json()["run"]["in_flight"]
            with app.state.database.session() as session:
                first = list(session.scalars(select(EmploymentFact).where(EmploymentFact.file_id == files[0])))
                assert first
                original = [(fact.id, fact.value_json) for fact in first]
                assert not list(session.scalars(select(EmploymentFact).where(EmploymentFact.file_id == files[1])))
            app.state.processing_queue._active.add_done_callback(lambda _: finished.set())
        finally:
            release.set()
        assert finished.wait(5)
        cancelled = client.get(base).json()
        assert cancelled["run"]["state"] == "cancelled"
        assert client.get(f"/api/analyses/{aid}/processing").json()["status"] == "cancelled"
        assert len(provider.calls) == 2
        assert cancelled["resume_preview"]["reusable_file_ids"] == [files[0]]
        assert cancelled["resume_preview"]["extraction_file_ids"] == files[1:]
        receipt = client.get(base + "/requests/" + cancel_body["request_id"]).json()
        assert receipt["outcome"] == "accepted"
        assert receipt["run"]["state"] == "cancelled"
        with app.state.database.session() as session:
            assert len(list(session.scalars(select(AIUsageRecord).where(AIUsageRecord.status == "succeeded")))) == 2
            assert not list(session.scalars(select(EmploymentFact).where(EmploymentFact.file_id == files[1])))
        resumed = client.post(base + "/resume", json=request_body(cancelled["run"]))
        assert resumed.status_code == 202, resumed.text
        app.state.processing_queue.shutdown()
        assert len(provider.calls) == 4
        with app.state.database.session() as session:
            assert [(fact.id, fact.value_json) for fact in session.scalars(select(EmploymentFact).where(
                EmploymentFact.file_id == files[0]))] == original
            assert len(list(session.scalars(select(ProcessingJob).where(
                ProcessingJob.job_type == "extract", ProcessingJob.status == "succeeded")))) == 3


def test_request_lookup_absence_is_unknown_and_scope_checked_first(tmp_path):
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        aid, _ = imported(client, tmp_path, 1)
        url = f"/api/analyses/{aid}/task/requests/{uuid4()}"
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()["outcome"] == "unknown"
        assert response.json()["request"] is None
        assert client.get(url + f"?company_id={uuid4()}").status_code == 404


@contextmanager
def api_case(tmp_path, monkeypatch=None, provider=None, count=1):
    if provider is not None:
        monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        aid, files = imported(client, tmp_path, count)
        yield app, client, aid, files, f"/api/analyses/{aid}/task"


def wait_worker(app):
    future = app.state.processing_queue._active
    if future is not None:
        future.result(5)


def test_cancel_before_adapter_starts_no_usage_or_facts(tmp_path, monkeypatch):
    from qian_labor.jobs.processing import ProcessingPipeline
    entered, release = Event(), Event()
    original = ProcessingPipeline._extraction_inputs
    def gate(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(ProcessingPipeline, "_extraction_inputs", staticmethod(gate))
    with api_case(tmp_path) as (app, client, aid, files, base):
        assert client.post(base + "/start", json=request_body()).status_code == 202
        try:
            assert entered.wait(5)
            run = client.get(base).json()["run"]
            assert client.post(base + "/cancel", json=request_body(run)).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        assert client.get(base).json()["run"]["state"] == "cancelled"
        with app.state.database.session() as session:
            assert not list(session.scalars(select(AIUsageRecord)))
            assert not list(session.scalars(select(EmploymentFact)))


@pytest.mark.parametrize("fail_submit", [False, True])
def test_cancel_prepared_pending_submission_and_failed_submit_receipt(tmp_path, monkeypatch, fail_submit):
    with api_case(tmp_path) as (app, client, aid, files, base):
        entered, release = Event(), Event()
        original = app.state.processing_queue._executor.submit
        def gate(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            if fail_submit:
                raise RuntimeError("SYNTHETIC_SUBMIT_FAILED")
            return original(*args, **kwargs)
        monkeypatch.setattr(app.state.processing_queue._executor, "submit", gate)
        body = request_body()
        with ThreadPoolExecutor(1) as executor:
            submission = executor.submit(client.post, base + "/start", json=body)
            try:
                assert entered.wait(5)
                pending = client.get(base + "/requests/" + body["request_id"]).json()
                assert pending["outcome"] == "pending"
                cancel = client.post(base + "/cancel", json=request_body(pending["run"]))
                assert cancel.status_code == 202
                assert app.state.processing_queue.is_busy
            finally:
                release.set()
            if fail_submit:
                with pytest.raises(RuntimeError, match="SYNTHETIC_SUBMIT_FAILED"):
                    submission.result(5)
            else:
                assert submission.result(5).status_code == 202
        wait_worker(app)
        receipt = client.get(base + "/requests/" + body["request_id"]).json()
        assert receipt["outcome"] == ("rejected" if fail_submit else "accepted")
        assert receipt["run"]["state"] == "cancelled"
        with app.state.database.session() as session:
            assert not list(session.scalars(select(AIUsageRecord)))
            if fail_submit:
                assert session.get(AnalysisBatch, aid).status == "uploading"


def test_publication_failure_releases_enqueued_waiter_and_receipt(tmp_path, monkeypatch):
    with api_case(tmp_path) as (app, client, aid, files, base):
        original = app.state.tasks.write
        @contextmanager
        def fail_publish():
            with original() as session:
                yield session
                if any(isinstance(item, TaskRequest) and item.outcome == "accepted" for item in session.dirty):
                    raise RuntimeError("SYNTHETIC_PUBLICATION_FAILED")
        monkeypatch.setattr(app.state.tasks, "write", fail_publish)
        body = request_body()
        with pytest.raises(RuntimeError, match="SYNTHETIC_PUBLICATION_FAILED"):
            client.post(base + "/start", json=body)
        wait_worker(app)
        receipt = client.get(base + "/requests/" + body["request_id"]).json()
        assert receipt["outcome"] == "rejected"
        assert not app.state.processing_queue.is_busy


def test_prepare_transaction_failure_rolls_back_owner_and_business_state(tmp_path, monkeypatch):
    with api_case(tmp_path) as (app, client, aid, files, base):
        original = app.state.tasks.write
        @contextmanager
        def fail_prepare():
            with original() as session:
                yield session
                if any(isinstance(item, TaskRequest) for item in session.new):
                    raise RuntimeError("SYNTHETIC_PREPARE_FAILED")
        monkeypatch.setattr(app.state.tasks, "write", fail_prepare)
        with pytest.raises(RuntimeError, match="SYNTHETIC_PREPARE_FAILED"):
            client.post(base + "/start", json=request_body())
        assert not app.state.processing_queue.is_busy
        assert app.state.tasks.controls == {}
        with app.state.database.session() as session:
            assert not list(session.scalars(select(TaskRun)))
            assert session.get(AnalysisBatch, aid).status == "uploading"


def test_shutdown_during_reservation_stops_published_worker_and_waits(tmp_path, monkeypatch):
    with api_case(tmp_path) as (app, client, aid, files, base):
        entered, release, closing = Event(), Event(), Event()
        original = app.state.processing_queue._executor.submit
        def gate(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(app.state.processing_queue._executor, "submit", gate)
        original_shutdown = app.state.processing_queue.shutdown
        def shutdown():
            closing.set()
            original_shutdown()
        monkeypatch.setattr(app.state.processing_queue, "shutdown", shutdown)
        with ThreadPoolExecutor(2) as executor:
            submission = executor.submit(client.post, base + "/start", json=request_body())
            assert entered.wait(5)
            stopped = executor.submit(app.state.tasks.shutdown)
            try:
                assert closing.wait(5)
                assert not stopped.done()
            finally:
                release.set()
            assert submission.result(5).status_code == 202
            stopped.result(5)
        assert client.get(base).json()["run"]["state"] == "interrupted"
        with app.state.database.session() as session:
            assert not list(session.scalars(select(AIUsageRecord)))


def test_owned_restart_interrupts_only_live_runs_and_rejects_simultaneous_owner(tmp_path):
    with api_case(tmp_path) as (app, client, aid, files, base):
        with app.state.database.session() as session:
            run = TaskRun(analysis_id=aid, owner_nonce=str(uuid4()), state="running")
            session.add(run)
            idle = AnalysisBatch(name="合成人工归属", status="matching_review")
            session.add(idle)
            session.commit()
            rid, idle_id = run.id, idle.id
        second = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
        with pytest.raises(RuntimeError, match="DESKTOP_TASK_OWNER_BUSY"):
            with TestClient(second):
                pytest.fail("second owner admitted")
        with app.state.database.session() as session:
            assert session.get(TaskRun, rid).state == "running"
    reopened = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(reopened) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        assert client.get(base).json()["run"]["state"] == "interrupted"
        assert client.get(f"/api/analyses/{idle_id}/task").json()["run"] is None
        assert client.get(f"/api/analyses/{idle_id}/processing").json()["status"] == "matching_review"
        assert client.post(base + "/start", json=request_body(client.get(base).json()["run"])).status_code == 409
        with reopened.state.database.session() as session:
            assert not list(session.scalars(select(AIUsageRecord)))
        assert client.post(base + "/resume", json=request_body(client.get(base).json()["run"])).status_code == 202
        wait_worker(reopened)


def test_duplicate_uuid_late_cancel_and_company_scope(tmp_path, monkeypatch):
    from test_company_workspaces import company
    from test_current_company import current
    entered, release = Event(), Event()
    class Provider(FakeAIProvider):
        calls = 0
        def extract(self, *args):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return super().extract(*args)
    provider = Provider()
    with api_case(tmp_path, monkeypatch, provider) as (app, client, aid, files, base):
        body = request_body()
        assert client.post(base + "/start", json=body).status_code == 202
        try:
            assert entered.wait(5)
            assert client.post(base + "/start", json=body).status_code == 202
            run = client.get(base).json()["run"]
            cancel = request_body(run)
            assert client.post(base + "/cancel", json=cancel).status_code == 202
            assert client.post(base + "/cancel", json=cancel).status_code == 202
            assert client.post(base + "/cancel", json=request_body(run)).status_code == 409
            assert client.post(base + "/resume", json=body).status_code == 409
        finally:
            release.set()
        wait_worker(app)
        assert provider.calls == 1
        terminal = client.get(base).json()["run"]
        assert client.post(base + "/cancel", json=request_body(terminal)).status_code == 202
        c = company(client)
        owned = current(client, c["id"])
        owned_base = f'/api/analyses/{owned["analysis_id"]}/task'
        assert client.get(owned_base).status_code == 404
        assert client.get(owned_base, params={"company_id": c["id"]}).status_code == 200
        assert client.get(owned_base + "/requests/" + body["request_id"], params={"company_id": c["id"]}).json()["outcome"] == "unknown"
        assert client.get(base + "/requests/" + body["request_id"], params={"company_id": c["id"]}).status_code == 404


@pytest.mark.parametrize("provider_kind", ["zhipu", "openai"])
@pytest.mark.parametrize("cancel_during_backoff", [False, True])
def test_real_provider_retry_and_backoff_stop_is_invocation_local(tmp_path, monkeypatch, provider_kind, cancel_during_backoff):
    import httpx
    from qian_labor.ai.providers import OpenAIResponsesProvider
    from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
    from qian_labor.jobs import control
    from qian_labor.security.local_redaction import PrivacyBoundary
    from qian_labor.settings import Settings
    entered, release = Event(), Event()
    calls = []
    def transport(request):
        calls.append(request)
        if len(calls) == 1 and not cancel_during_backoff:
            entered.set()
            assert release.wait(5)
        return httpx.Response(503, request=request)
    original_wait = control.retry_wait
    def backoff(seconds):
        entered.set()
        return original_wait(60)
    if cancel_during_backoff:
        monkeypatch.setattr(control, "retry_wait", backoff)
    cls = ZhipuChatCompletionsProvider if provider_kind == "zhipu" else OpenAIResponsesProvider
    if provider_kind == "openai":
        # Current advisory/bbox schema is independently incompatible with this legacy
        # adapter's strict whitelist. Exercise its real retry path with a valid schema;
        # this is not acceptance of the full extraction schema or a real provider call.
        from qian_labor.ai.schemas import ProviderExtractionResult
        monkeypatch.setattr(ProviderExtractionResult, "model_json_schema", classmethod(
            lambda cls: {"type": "object", "properties": {}}))
    pepper = "synthetic-task-private-pepper-at-least-32-characters"
    provider = cls(api_key="synthetic-never-real", base_url="https://synthetic.invalid/v1",
        text_model="synthetic", vision_model="synthetic", max_attempts=3, retry_delay_seconds=0.1,
        privacy_boundary=PrivacyBoundary(pepper), client=httpx.Client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN, settings=Settings(pii_hash_pepper=pepper))
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        aid, _ = imported(client, tmp_path)
        base = f"/api/analyses/{aid}/task"
        assert client.post(base + "/start", json=request_body()).status_code == 202
        try:
            assert entered.wait(5), client.get(f"/api/analyses/{aid}/processing").json()
            assert client.post(base + "/cancel", json=request_body(client.get(base).json()["run"])).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        assert len(calls) == 1
        assert client.get(base).json()["run"]["state"] == "cancelled"
        with app.state.database.session() as session:
            assert [usage.status for usage in session.scalars(select(AIUsageRecord))] == ["unknown"]
        # The shared provider's independent configuration invocation has no worker context.
        if provider_kind == "zhipu":
            from qian_labor.ai.providers import AIProviderError
            with pytest.raises(AIProviderError):
                provider.check_connection()
            assert len(calls) == 2


def test_whole_file_commit_wins_before_cancel_ack_and_next_file_does_not_start(tmp_path, monkeypatch):
    from qian_labor.jobs.processing import ProcessingPipeline
    committing, release_commit, between, release_between, cancel_started = (Event() for _ in range(5))
    original_persist = ProcessingPipeline._persist_result
    def persist(self, *args, **kwargs):
        committing.set()
        assert release_commit.wait(5)
        return original_persist(self, *args, **kwargs)
    monkeypatch.setattr(ProcessingPipeline, "_persist_result", persist)
    original_file = ProcessingPipeline._process_file
    def process_file(self, *args):
        result = original_file(self, *args)
        between.set()
        assert release_between.wait(5)
        return result
    monkeypatch.setattr(ProcessingPipeline, "_process_file", process_file)
    with api_case(tmp_path, count=2) as (app, client, aid, files, base):
        assert client.post(base + "/start", json=request_body()).status_code == 202
        assert committing.wait(5)
        run = client.get(base).json()["run"]
        def cancel():
            cancel_started.set()
            return client.post(base + "/cancel", json=request_body(run))
        with ThreadPoolExecutor(1) as executor:
            response = executor.submit(cancel)
            try:
                assert cancel_started.wait(5)
                assert not response.done()
                release_commit.set()
                assert between.wait(5)
                assert response.result(5).status_code == 202
                with app.state.database.session() as session:
                    assert list(session.scalars(select(EmploymentFact).where(EmploymentFact.file_id == files[0])))
            finally:
                release_commit.set()
                release_between.set()
        wait_worker(app)
        with app.state.database.session() as session:
            assert len(list(session.scalars(select(AIUsageRecord)))) == 1
            assert not list(session.scalars(select(EmploymentFact).where(EmploymentFact.file_id == files[1])))


def test_previous_effective_result_survives_cancel_and_explicit_local_reevaluation(tmp_path, monkeypatch):
    from test_effective_facts import prepared, revision_body, local_evaluate
    from qian_labor.models.core import SourceLocator, EffectiveFactRevision
    entered, release = Event(), Event()
    class Provider(FakeAIProvider):
        calls = 0
        def extract(self, *args):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return super().extract(*args)
    provider = Provider()
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path, launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        client, db, company, rec, aid, business = prepared((client, app.state.database), tmp_path)
        row = client.get(business + "/effective-facts").json()["items"][0]
        body = revision_body(row)
        assert client.post(business + f'/effective-facts/{row["id"]}/revisions', json=body).status_code == 200
        evaluated = local_evaluate(client, business)
        assert evaluated["fresh"]
        with db.session() as session:
            original = session.get(EmploymentFact, row["id"]).value_json
            source_ids = list(session.scalars(select(SourceLocator.id)))
        extra = tmp_path / "synthetic-supplement.csv"
        extra.write_text("员工编号,姓名\nSYN-001,完全虚构员工\n")
        assert client.post(f"/api/analyses/{aid}/import-paths", json={"paths": [str(extra)]}).status_code == 200
        base = f"/api/analyses/{aid}/task"
        params = {"company_id": company["id"]}
        assert client.post(base + "/start", json=request_body(company_id=company["id"])).status_code == 202
        try:
            assert entered.wait(5)
            run = client.get(base, params=params).json()["run"]
            assert client.post(base + "/cancel", json=request_body(run, company["id"])).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        state = client.get(business + "/assessment-results").json()["assessment_revision"]
        assert state["result_revision"] == evaluated["result_revision"]
        assert not state["fresh"] and state["completeness"] == "partial"
        assert state["availability"] == "available"
        assert client.get(f'/api/company-workspaces/{company["id"]}/current').json()["current_analysis"]["stale"]
        assert client.get(f"/api/analyses/{aid}/dashboard").json()["overview"]["assessment_revision"] == state
        with db.session() as session:
            assert session.get(EmploymentFact, row["id"]).value_json == original
            assert list(session.scalars(select(SourceLocator.id))) == source_ids
            assert session.get(EffectiveFactRevision, body["id"]) is not None
        after = local_evaluate(client, business)
        assert after["fresh"] and after["completeness"] == "partial"
        assert after["result_revision"] != state["result_revision"]
        assert provider.calls == 1
        assert client.get(base, params=params).json()["run"]["state"] == "cancelled"


def test_multi_page_file_discards_all_pages_after_inflight_cancel(tmp_path, monkeypatch):
    import pymupdf as fitz
    from qian_labor.ai.schemas import ExtractionResult, EmploymentFact as ExtractedFact, SourceLocator
    entered, release = Event(), Event()
    class Provider(FakeAIProvider):
        calls = 0
        def extract(self, filename, content):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(5)
            return ExtractionResult(document_type="contract", employee_number="SYN-001", facts=[ExtractedFact(
                fact_type="employment.contract.exists", value=True, confidence=1,
                source=SourceLocator(file_name=filename, excerpt="SYN-001 synthetic contract", page=self.calls))])
    provider = Provider()
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    path = tmp_path / "synthetic-pages.pdf"
    doc = fitz.open()
    for _ in range(3):
        doc.new_page().insert_text((72, 72), "SYN-001 synthetic contract")
    doc.save(path)
    doc.close()
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        aid = client.post("/api/analyses", json={"name": "合成多页"}).json()["id"]
        assert client.post(f"/api/analyses/{aid}/import-paths", json={"paths": [str(path)]}).status_code == 200
        base = f"/api/analyses/{aid}/task"
        assert client.post(base + "/start", json=request_body()).status_code == 202
        try:
            assert entered.wait(5)
            assert client.post(base + "/cancel", json=request_body(client.get(base).json()["run"])).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        assert provider.calls == 2
        with app.state.database.session() as session:
            assert not list(session.scalars(select(EmploymentFact)))
            assert not list(session.scalars(select(ProcessingJob).where(
                ProcessingJob.job_type == "extract", ProcessingJob.status == "succeeded")))
            assert len(list(session.scalars(select(AIUsageRecord).where(AIUsageRecord.status == "succeeded")))) == 2


def test_actual_lifespan_shutdown_waits_inflight_and_restart_requires_explicit_resume(tmp_path, monkeypatch):
    entered, release, closing = Event(), Event(), Event()
    class Provider(FakeAIProvider):
        calls = 0
        def extract(self, *args):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(10)
            return super().extract(*args)
    provider = Provider()
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    client = TestClient(app).__enter__()
    client.headers["X-Qian-Desktop-Token"] = TOKEN
    aid, files = imported(client, tmp_path)
    base = f"/api/analyses/{aid}/task"
    original_shutdown = app.state.processing_queue.shutdown
    def shutdown():
        closing.set()
        original_shutdown()
    monkeypatch.setattr(app.state.processing_queue, "shutdown", shutdown)
    assert client.post(base + "/start", json=request_body()).status_code == 202
    assert entered.wait(5)
    with ThreadPoolExecutor(1) as executor:
        closed = executor.submit(client.__exit__, None, None, None)
        try:
            assert closing.wait(5)
            assert not closed.done()
            second = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
            with pytest.raises(RuntimeError, match="DESKTOP_TASK_OWNER_BUSY"):
                with TestClient(second):
                    pass
        finally:
            release.set()
        closed.result(5)
    reopened = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(reopened) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        state = client.get(base).json()
        assert state["run"]["state"] == "interrupted"
        assert state["business_status"] == "interrupted"
        assert state["resume_preview"]["reusable_file_ids"] == [files[0]]
        assert provider.calls == 2
        assert client.post(base + "/resume", json=request_body(state["run"])).status_code == 202
        wait_worker(reopened)
        assert provider.calls == 4


def test_resume_preview_excludes_outdated_extraction_version(tmp_path, monkeypatch):
    from qian_labor.jobs import processing
    with api_case(tmp_path) as (app, client, aid, files, base):
        assert client.post(base + "/start", json=request_body()).status_code == 202
        wait_worker(app)
        assert client.get(base).json()["resume_preview"]["reusable_file_ids"] == files
        monkeypatch.setattr(processing, "EXTRACTION_VERSION", "synthetic-next-version")
        state = client.get(base).json()
        assert state["resume_preview"]["reusable_file_ids"] == []
        assert state["resume_preview"]["extraction_file_ids"] == files


def test_pending_identity_retains_matching_review_after_cancel_and_history_readonly(tmp_path, monkeypatch):
    from test_company_workspaces import company, record, snapshot
    from test_current_company import current
    entered, release = Event(), Event()
    class Provider(FakeAIProvider):
        calls = 0
        def extract(self, *args):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(5)
            return super().extract(*args)
    provider = Provider()
    monkeypatch.setattr("qian_labor.desktop.app.provider_from_settings", lambda _: provider)
    app = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(app) as client:
        client.headers["X-Qian-Desktop-Token"] = TOKEN
        c = company(client)
        rec = record(client, c["id"], "SYN-000")
        aid = current(client, c["id"], 1)["analysis_id"]
        paths = []
        for i in range(2):
            path = tmp_path / f"synthetic-owned-{i}.csv"
            path.write_text(f"员工编号,姓名\nSYN-{i:03d},完全虚构员工\n")
            paths.append(str(path))
        assert client.post(f"/api/analyses/{aid}/import-paths", json={"paths": paths}).status_code == 200
        base = f"/api/analyses/{aid}/task"
        params = {"company_id": c["id"]}
        assert client.post(base + "/start", json=request_body(company_id=c["id"])).status_code == 202
        try:
            assert entered.wait(5)
            run = client.get(base, params=params).json()["run"]
            assert client.post(base + "/cancel", json=request_body(run, c["id"])).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        state = client.get(base, params=params).json()
        assert state["run"]["state"] == "cancelled" and state["business_status"] == "matching_review"
        assert client.get(f"/api/analyses/{aid}/matching-candidates").json()["candidates"]
        # Actual historical adoption cannot turn the linked snapshot into a writable task.
        historical, snapshot_id = snapshot(app.state.database)
        assert client.put(f'/api/company-workspaces/{c["id"]}/analyses/{historical}/binding', json={
            "expected_company_version": 2, "expected_analysis_version": 0,
            "decisions": [{"snapshot_id": snapshot_id, "action": "link", "employee_record_id": rec["id"],
                           "expected_record_version": rec["version"]}]}).status_code == 200
        hist = f"/api/analyses/{historical}/task"
        assert client.get(hist, params=params).json()["read_only"]
        assert client.post(hist + "/start", json=request_body(company_id=c["id"])).status_code == 409


def _separate_task_owner(data_dir, ready, release):
    app = create_desktop_app(data_dir=data_dir, launch_token=TOKEN)
    with TestClient(app):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("SYNTHETIC_OWNER_RELEASE_TIMEOUT")


def test_cross_process_owner_exclusion_and_release(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_separate_task_owner, args=(tmp_path / "data", ready, release))
    process.start()
    try:
        assert ready.wait(5)
        second = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
        with pytest.raises(RuntimeError, match="DESKTOP_TASK_OWNER_BUSY"):
            with TestClient(second):
                pass
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0
    reopened = create_desktop_app(data_dir=tmp_path / "data", launch_token=TOKEN)
    with TestClient(reopened) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("overlap", ["before_queue", "reserved_before_prepare"])
@pytest.mark.parametrize("different_body", [False, True])
def test_fix1_same_uuid_concurrent_admission_reconciles_one_worker(tmp_path, monkeypatch, overlap, different_body):
    from threading import current_thread
    from qian_labor.desktop import tasks
    entered, release, contender_observed = Event(), Event(), Event()
    # Observe an actual request waiter without replacing Event synchronization.
    # On the old implementation the competing HTTP response signals this instead.
    class ObservedEvent(Event):
        def wait(self, timeout=None):
            if not current_thread().name.startswith("qian-desktop-analysis"):
                contender_observed.set()
            return super().wait(timeout)
    monkeypatch.setattr(tasks, "Event", ObservedEvent)
    with api_case(tmp_path) as (app, client, aid, files, base):
        queue_submit = app.state.processing_queue.submit
        first = True
        def gate(analysis_id, **kwargs):
            nonlocal first
            if first:
                first = False
                if overlap == "before_queue":
                    entered.set()
                    assert release.wait(5)
                else:
                    prepare = kwargs["prepare"]
                    def gated_prepare():
                        entered.set()
                        assert release.wait(5)
                        return prepare()
                    kwargs["prepare"] = gated_prepare
            return queue_submit(analysis_id, **kwargs)
        monkeypatch.setattr(app.state.processing_queue, "submit", gate)
        body = request_body()
        with ThreadPoolExecutor(2) as executor:
            original = executor.submit(client.post, base + "/start", json=body)
            assert entered.wait(5)
            competing_body = {**body, "expected_version": 0} if different_body else body
            competing = executor.submit(client.post, base + "/start", json=competing_body)
            competing.add_done_callback(lambda _: contender_observed.set())
            try:
                assert contender_observed.wait(5)
                if different_body:
                    rejected = competing.result(5)
                    assert rejected.status_code == 409
                    assert rejected.json()["detail"]["code"] == "TASK_REQUEST_CONFLICT"
            finally:
                release.set()
            accepted = original.result(5)
            assert accepted.status_code == 202, accepted.text
            if not different_body:
                duplicate = competing.result(5)
                assert duplicate.status_code == 202, duplicate.text
                assert duplicate.json()["outcome"] == "accepted"
                assert duplicate.json()["request"]["id"] == body["request_id"]
                assert duplicate.json()["run"]["id"] == accepted.json()["run"]["id"]
        wait_worker(app)
        with app.state.database.session() as session:
            assert len(list(session.scalars(select(TaskRequest)))) == 1
            assert len(list(session.scalars(select(TaskRun)))) == 1
            assert len(list(session.scalars(select(AIUsageRecord)))) == 1


@pytest.mark.parametrize("cancel_restore", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_fix1_failed_file_cache_resume_restores_state_without_extraction(tmp_path, monkeypatch, cancel_restore, partial):
    from qian_labor.jobs.processing import ProcessingPipeline
    from qian_labor.models.core import UploadedFile, SourceLocator, ContractAdvisoryRun
    with api_case(tmp_path) as (app, client, aid, files, base):
        assert client.post(base + "/start", json=request_body()).status_code == 202
        wait_worker(app)
        with app.state.database.session() as session:
            originals = [(fact.id, fact.value_json) for fact in session.scalars(select(EmploymentFact))]
            sources = [(source.id, source.location) for source in session.scalars(select(SourceLocator))]
            advisory_ids = list(session.scalars(select(ContractAdvisoryRun.id)))
            job_ids = list(session.scalars(select(ProcessingJob.id)))
        parse = ProcessingPipeline._parse
        def fail_parse(*args):
            raise RuntimeError("SYNTHETIC_TRANSIENT_PARSE_FAILURE")
        monkeypatch.setattr(ProcessingPipeline, "_parse", fail_parse)
        assert client.post(base + "/start", json=request_body(client.get(base).json()["run"])).status_code == 202
        wait_worker(app)
        failed = client.get(base).json()
        assert failed["run"]["state"] == "failed"
        assert failed["resume_preview"]["reusable_file_ids"] == files
        assert failed["resume_preview"]["extraction_file_ids"] == []
        def restored_parse(*args):
            parsed = parse(*args)
            if partial:
                parsed.warnings.append("SYNTHETIC_PARSE_WARNING")
            return parsed
        monkeypatch.setattr(ProcessingPipeline, "_parse", restored_parse)
        cache_selected, release = Event(), Event()
        if cancel_restore:
            extract = ProcessingPipeline._extract
            def gated_cache(*args):
                result = extract(*args)
                cache_selected.set()
                assert release.wait(5)
                return result
            monkeypatch.setattr(ProcessingPipeline, "_extract", gated_cache)
        assert client.post(base + "/resume", json=request_body(failed["run"])).status_code == 202
        try:
            if cancel_restore:
                assert cache_selected.wait(5)
                assert client.post(base + "/cancel", json=request_body(client.get(base).json()["run"])).status_code == 202
        finally:
            release.set()
        wait_worker(app)
        state = client.get(base).json()
        assert state["run"]["state"] == ("cancelled" if cancel_restore else "partial" if partial else "completed")
        assert state["business_status"] == ("cancelled" if cancel_restore else "partial" if partial else "completed")
        with app.state.database.session() as session:
            file = session.get(UploadedFile, files[0])
            assert file.status == ("failed" if cancel_restore else "partial" if partial else "processed")
            assert file.error_code == ("PROCESSING_FILE_FAILED" if cancel_restore else "PROCESSING_INCOMPLETE" if partial else None)
            if not cancel_restore:
                assert file.progress == 100
            assert [(fact.id, fact.value_json) for fact in session.scalars(select(EmploymentFact))] == originals
            assert [(source.id, source.location) for source in session.scalars(select(SourceLocator))] == sources
            assert list(session.scalars(select(ContractAdvisoryRun.id))) == advisory_ids
            assert list(session.scalars(select(ProcessingJob.id))) == job_ids
            assert len(list(session.scalars(select(AIUsageRecord)))) == 1


@pytest.mark.parametrize("failure", ["before_queue", "prepare", "executor", "publish", "shutdown"])
def test_fix1_admission_waiter_released_on_failure_and_shutdown(tmp_path, monkeypatch, failure):
    from threading import current_thread
    from qian_labor.desktop import tasks
    entered, release, waiting, closing = (Event() for _ in range(4))
    class ObservedEvent(Event):
        def wait(self, timeout=None):
            if not current_thread().name.startswith("qian-desktop-analysis"):
                waiting.set()
            return super().wait(timeout)
    monkeypatch.setattr(tasks, "Event", ObservedEvent)
    with api_case(tmp_path) as (app, client, aid, files, base):
        queue = app.state.processing_queue
        submit = queue.submit
        def gated_submit(analysis_id, **kwargs):
            if failure == "before_queue":
                entered.set()
                assert release.wait(5)
                raise RuntimeError("SYNTHETIC_ADMISSION_FAILED")
            if failure in {"prepare", "publish"}:
                def fail():
                    entered.set()
                    assert release.wait(5)
                    raise RuntimeError("SYNTHETIC_ADMISSION_FAILED")
                kwargs[failure] = fail
            return submit(analysis_id, **kwargs)
        monkeypatch.setattr(queue, "submit", gated_submit)
        executor_submit = queue._executor.submit
        if failure in {"executor", "shutdown"}:
            def gated_executor(*args, **kwargs):
                entered.set()
                assert release.wait(5)
                if failure == "executor":
                    raise RuntimeError("SYNTHETIC_ADMISSION_FAILED")
                return executor_submit(*args, **kwargs)
            monkeypatch.setattr(queue._executor, "submit", gated_executor)
        shutdown = queue.shutdown
        def observed_shutdown():
            closing.set()
            shutdown()
        monkeypatch.setattr(queue, "shutdown", observed_shutdown)
        body = request_body()
        with ThreadPoolExecutor(3) as executor:
            original = executor.submit(client.post, base + "/start", json=body)
            assert entered.wait(5)
            duplicate = executor.submit(client.post, base + "/start", json=body)
            try:
                assert waiting.wait(5)
                assert not duplicate.done()
                pending = client.get(base + "/requests/" + body["request_id"]).json()
                assert pending["outcome"] == ("unknown" if failure in {"before_queue", "prepare"} else "pending")
                if failure == "shutdown":
                    stopped = executor.submit(app.state.tasks.shutdown)
                    assert closing.wait(5)
                    assert not stopped.done()
                elif pending["outcome"] == "pending":
                    assert client.post(base + "/cancel", json=request_body(pending["run"])).status_code == 202
            finally:
                release.set()
            if failure == "shutdown":
                assert original.result(5).status_code == 202
                assert duplicate.result(5).json()["outcome"] == "accepted"
                stopped.result(5)
            else:
                with pytest.raises(RuntimeError, match="SYNTHETIC_ADMISSION_FAILED"):
                    original.result(5)
                if failure in {"before_queue", "prepare"}:
                    with pytest.raises(RuntimeError, match="SYNTHETIC_ADMISSION_FAILED"):
                        duplicate.result(5)
                else:
                    assert duplicate.result(5).json()["outcome"] == "rejected"
        wait_worker(app)
        receipt = client.get(base + "/requests/" + body["request_id"]).json()
        assert receipt["outcome"] == ("accepted" if failure == "shutdown" else
                                      "unknown" if failure in {"before_queue", "prepare"} else "rejected")
        assert app.state.tasks.admissions == {}
        assert app.state.tasks.controls == {}
        with app.state.database.session() as session:
            assert not list(session.scalars(select(AIUsageRecord)))
            assert len(list(session.scalars(select(TaskRun)))) == (0 if failure in {"before_queue", "prepare"} else 1)


@pytest.mark.parametrize("changed_body", [False, True])
def test_fix2_waiter_validates_receipt_after_failed_admission_uuid_reuse(tmp_path, monkeypatch, changed_body):
    from threading import current_thread
    from qian_labor.desktop import tasks
    entered, release_leader, waiting, awakened, release_waiter = (Event() for _ in range(5))
    class PausedWaitEvent(Event):
        def wait(self, timeout=None):
            if current_thread().name.startswith("qian-desktop-analysis"):
                return super().wait(timeout)
            waiting.set()
            result = super().wait(timeout)
            awakened.set()
            assert release_waiter.wait(5)
            return result
    monkeypatch.setattr(tasks, "Event", PausedWaitEvent)
    with api_case(tmp_path) as (app, client, aid, files, base):
        queue_submit = app.state.processing_queue.submit
        first = True
        def fail_first(analysis_id, **kwargs):
            nonlocal first
            if first:
                first = False
                entered.set()
                assert release_leader.wait(5)
                raise RuntimeError("SYNTHETIC_ADMISSION_FAILED")
            return queue_submit(analysis_id, **kwargs)
        monkeypatch.setattr(app.state.processing_queue, "submit", fail_first)
        body = request_body()
        if changed_body:
            body["expected_version"] = 0
        with ThreadPoolExecutor(2) as executor:
            leader = executor.submit(client.post, base + "/start", json=body)
            assert entered.wait(5)
            waiter = executor.submit(client.post, base + "/start", json=body)
            try:
                assert waiting.wait(5)
                release_leader.set()
                with pytest.raises(RuntimeError, match="SYNTHETIC_ADMISSION_FAILED"):
                    leader.result(5)
                assert awakened.wait(5)
                assert app.state.tasks.admissions == {}
                assert client.get(base + "/requests/" + body["request_id"]).json()["outcome"] == "unknown"
                later = client.post(base + "/start", json={**body, "expected_version": None})
                assert later.status_code == 202, later.text
                assert later.json()["outcome"] == "accepted"
            finally:
                release_leader.set()
                release_waiter.set()
            result = waiter.result(5)
            if changed_body:
                assert result.status_code == 409, result.text
                assert result.json()["detail"]["code"] == "TASK_REQUEST_CONFLICT"
            else:
                assert result.status_code == 202, result.text
                assert result.json()["outcome"] == "accepted"
                assert result.json()["run"]["id"] == later.json()["run"]["id"]
        wait_worker(app)
        with app.state.database.session() as session:
            assert len(list(session.scalars(select(TaskRun)))) == 1
            assert len(list(session.scalars(select(TaskRequest)))) == 1
            assert len(list(session.scalars(select(AIUsageRecord)))) == 1
