"""Persisted desktop execution owner, separate from business review state."""
from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import stat
from threading import Event, RLock
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from qian_labor.jobs.control import ProcessingStopped, owned_invocation
from qian_labor.jobs.processing import ProcessingPipeline
from qian_labor.models.core import (
    AIUsageRecord, AnalysisBatch, CompanyAnalysisBinding, EmployeeMatchCandidate, ProcessingJob, TaskRequest,
    TaskRun, UploadedFile,
)
from qian_labor.services.company_workspaces import WorkspaceError, require_material_mutation
from qian_labor.sqlite_migrations import assert_no_pending_recovery

ACTIVE = {"queued", "running", "cancel_requested"}


class TaskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    company_id: UUID | None = None
    expected_run_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0, strict=True)


class CommandAdmission:
    def __init__(self, signature):
        self.signature = signature
        self.done = Event()
        self.error = None


@contextmanager
def task_owner_lock(data_dir: Path):
    """OS lock, not a PID/nonce inference. Keep the inode for future owners."""
    path = data_dir / ".qian-task-owner.lock"
    if path.is_symlink():
        raise RuntimeError("DESKTOP_TASK_OWNER_INVALID")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("DESKTOP_TASK_OWNER_INVALID")
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("DESKTOP_TASK_OWNER_BUSY") from None
        else:
            import fcntl
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("DESKTOP_TASK_OWNER_BUSY") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_payload(run):
    if run is None:
        return None
    return {key: getattr(run, key) for key in (
        "id", "analysis_id", "company_id", "parent_run_id", "state", "version",
        "in_flight", "created_at", "completed_at",
    )}


def receipt_payload(receipt):
    return {key: getattr(receipt, key) for key in (
        "id", "analysis_id", "company_id", "run_id", "operation", "expected_run_id",
        "expected_version", "outcome", "error_code", "created_at",
    )} if receipt is not None else None


class RunControl:
    def __init__(self, owner, run_id):
        self.owner, self.run_id = owner, run_id
        self.stop = Event()
        self.interrupted = False

    def checkpoint(self):
        with self.owner.guard:
            if self.stop.is_set():
                raise ProcessingStopped()

    @contextmanager
    def commit_boundary(self):
        # Every caller must enter BEFORE creating a database session/transaction.
        with self.owner.guard:
            self.checkpoint()
            yield

    @contextmanager
    def adapter_call(self, begin_usage):
        with self.owner.guard:
            self.checkpoint()
            begin_usage()
            with self.owner.write() as session:
                session.get(TaskRun, self.run_id).in_flight = True
        try:
            yield
        finally:
            with self.owner.guard:
                with self.owner.write() as session:
                    session.get(TaskRun, self.run_id).in_flight = False


class DesktopTasks:
    def __init__(self, database, queue, provider):
        self.database, self.queue, self.provider = database, queue, provider
        self.guard = RLock()
        self.owner_nonce = str(uuid4())
        self.controls = {}
        self.admissions = {}
        self.ready = False
        self.closing = False

    @contextmanager
    def write(self):
        assert_no_pending_recovery(self.database.path.parent)
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()

    @staticmethod
    def scope(session, analysis_id, company_id, *, mutate=False):
        analysis = session.get(AnalysisBatch, analysis_id)
        binding = session.get(CompanyAnalysisBinding, analysis_id)
        actual_company = binding.company_id if binding else None
        if analysis is None or actual_company != company_id:
            raise WorkspaceError("TASK_SCOPE_INVALID", 404)
        if mutate:
            require_material_mutation(session, analysis_id)
        return analysis, binding

    @staticmethod
    def latest(session, analysis_id):
        return session.scalar(select(TaskRun).where(TaskRun.analysis_id == analysis_id)
                              .order_by(TaskRun.created_at.desc(), TaskRun.id.desc()).limit(1))

    @staticmethod
    def transition(run, state):
        run.state = state
        run.version += 1
        if state not in ACTIVE:
            run.completed_at = datetime.now(UTC)
            run.in_flight = False

    @staticmethod
    def interrupted_files(session, analysis_id):
        for job in session.scalars(select(ProcessingJob).where(
                ProcessingJob.analysis_id == analysis_id, ProcessingJob.status == "running")):
            job.status, job.error_code = "interrupted", "PROCESSING_INTERRUPTED"
            job.completed_at = datetime.now(UTC)
        for item in session.scalars(select(UploadedFile).where(
                UploadedFile.analysis_id == analysis_id, UploadedFile.status.in_({"parsing", "extracting"}))):
            item.status, item.error_code = "interrupted", "PROCESSING_INTERRUPTED"
        for usage in session.scalars(select(AIUsageRecord).where(
                AIUsageRecord.analysis_id == analysis_id, AIUsageRecord.status == "running")):
            usage.status = "unknown"

    @staticmethod
    def stopped_business(session, analysis_id, state):
        analysis = session.get(AnalysisBatch, analysis_id)
        pending = session.scalar(select(EmployeeMatchCandidate.id).where(
            EmployeeMatchCandidate.analysis_id == analysis_id,
            EmployeeMatchCandidate.status == "pending").limit(1))
        analysis.status = "matching_review" if pending else state
        analysis.current_stage = analysis.status
        analysis.failure_reason = "PROCESSING_CANCELLED" if state == "cancelled" else "PROCESSING_INTERRUPTED"

    def startup(self):
        # Called only inside the exclusive data-dir lock, never during app construction.
        with self.guard, self.write() as session:
            for run in session.scalars(select(TaskRun).where(TaskRun.state.in_(ACTIVE))):
                self.transition(run, "interrupted")
                self.interrupted_files(session, run.analysis_id)
                self.stopped_business(session, run.analysis_id, "interrupted")
            for receipt in session.scalars(select(TaskRequest).where(TaskRequest.outcome == "pending")):
                receipt.outcome, receipt.error_code = "rejected", "TASK_SUBMISSION_INTERRUPTED"
            self.ready = True

    def shutdown(self):
        with self.guard:
            self.closing = True
            for control in self.controls.values():
                control.interrupted = True
                control.stop.set()
        self.queue.shutdown()
        self.ready = False

    def metadata(self, analysis_id, company_id, limit=20, offset=0):
        with self.database.session() as session:
            analysis, binding = self.scope(session, analysis_id, company_id)
            run = self.latest(session, analysis_id)
            files = list(session.scalars(select(UploadedFile).where(UploadedFile.analysis_id == analysis_id)
                                        .order_by(UploadedFile.created_at, UploadedFile.id)))
            reusable = [item.id for item in files if ProcessingPipeline.cached_extraction(
                session, analysis_id, item, self.provider)]
            runs = list(session.scalars(select(TaskRun).where(TaskRun.analysis_id == analysis_id)
                        .order_by(TaskRun.created_at.desc(), TaskRun.id.desc()).offset(offset).limit(limit)))
            return {"analysis_id": analysis_id, "company_id": company_id,
                    "business_status": analysis.status, "run": run_payload(run),
                    "read_only": binding is not None and binding.role == "historical",
                    "history": [run_payload(item) for item in runs], "limit": limit, "offset": offset,
                    "resume_preview": {"reusable_file_ids": reusable,
                        "extraction_file_ids": [item.id for item in files if item.id not in reusable]},
                    "usage_notice": "admitted_requests_may_consume_quota_unknown_is_not_zero"}

    def lookup(self, analysis_id, company_id, request_id):
        with self.database.session() as session:
            self.scope(session, analysis_id, company_id)
            receipt = session.get(TaskRequest, request_id)
            if receipt is not None and (receipt.analysis_id != analysis_id or receipt.company_id != company_id):
                receipt = None
            return {"analysis_id": analysis_id, "company_id": company_id,
                    "outcome": receipt.outcome if receipt else "unknown",
                    "request": receipt_payload(receipt),
                    "run": run_payload(session.get(TaskRun, receipt.run_id)) if receipt else None}

    @staticmethod
    def duplicate(session, analysis_id, company_id, operation, body):
        receipt = session.get(TaskRequest, str(body.request_id))
        if receipt is None:
            return False
        expected = str(body.expected_run_id) if body.expected_run_id else None
        if (receipt.analysis_id, receipt.company_id, receipt.operation, receipt.expected_run_id, receipt.expected_version) != (
                analysis_id, company_id, operation, expected, body.expected_version):
            raise WorkspaceError("TASK_REQUEST_CONFLICT")
        return True

    @staticmethod
    def cas(run, body):
        expected = str(body.expected_run_id) if body.expected_run_id else None
        if (run.id if run else None, run.version if run else None) != (expected, body.expected_version):
            raise WorkspaceError("TASK_VERSION_CONFLICT")

    def command(self, analysis_id, operation, body):
        company_id = str(body.company_id) if body.company_id else None
        request_id = str(body.request_id)
        signature = (analysis_id, company_id, operation,
                     str(body.expected_run_id) if body.expected_run_id else None, body.expected_version)
        with self.guard:
            with self.database.session() as session:
                self.scope(session, analysis_id, company_id, mutate=True)
                admission = self.admissions.get(request_id)
                leader = admission is None
                if leader and self.duplicate(session, analysis_id, company_id, operation, body):
                    return self.lookup(analysis_id, company_id, request_id)
            if leader:
                admission = CommandAdmission(signature)
                self.admissions[request_id] = admission
            elif admission.signature != signature:
                raise WorkspaceError("TASK_REQUEST_CONFLICT")
        if not leader:
            # No lifecycle guard, database session or queue mutex while waiting.
            # Only the original caller may reserve/prepare/publish this UUID.
            admission.done.wait()
            result = self.lookup(analysis_id, company_id, request_id)
            # A failed admission may have been replaced while this caller waited.
            receipt = result["request"]
            if receipt is not None and tuple(receipt[key] for key in (
                    "analysis_id", "company_id", "operation", "expected_run_id", "expected_version")) != signature:
                raise WorkspaceError("TASK_REQUEST_CONFLICT")
            if result["outcome"] == "unknown" and admission.error is not None:
                raise admission.error
            return {**result, "queue_mode": "desktop"}
        try:
            if operation == "cancel":
                self.cancel(analysis_id, company_id, body)
            else:
                self.submit(analysis_id, company_id, operation, body)
            return {**self.lookup(analysis_id, company_id, request_id), "queue_mode": "desktop"}
        except BaseException as error:
            admission.error = error
            raise
        finally:
            with self.guard:
                self.admissions.pop(request_id, None)
                admission.done.set()

    def cancel(self, analysis_id, company_id, body):
        with self.guard, self.write() as session:
            self.scope(session, analysis_id, company_id, mutate=True)
            if self.duplicate(session, analysis_id, company_id, "cancel", body):
                return
            run = self.latest(session, analysis_id)
            self.cas(run, body)
            if run is None or run.state not in {"queued", "running", "cancel_requested", "cancelled"}:
                raise WorkspaceError("TASK_NOT_CANCELLABLE")
            if run.state in {"queued", "running"}:
                self.transition(run, "cancel_requested")
            session.add(TaskRequest(id=str(body.request_id), analysis_id=analysis_id, company_id=company_id,
                run_id=run.id, operation="cancel", expected_run_id=str(body.expected_run_id),
                expected_version=body.expected_version, outcome="accepted"))
            control = self.controls.get(run.id)
            # Set only after durable acknowledgement; guard excludes adapter/commit admission.
            session.commit()
            if control is not None:
                control.stop.set()

    def submit(self, analysis_id, company_id, operation, body):
        published = Event()
        execution = {"accepted": False, "run_id": None}
        def prepare():
            with self.guard, self.write() as session:
                if not self.ready or self.closing:
                    raise WorkspaceError("TASK_OWNER_UNAVAILABLE")
                analysis, _ = self.scope(session, analysis_id, company_id, mutate=True)
                if self.duplicate(session, analysis_id, company_id, operation, body):
                    raise WorkspaceError("TASK_REQUEST_CONFLICT")
                prior = self.latest(session, analysis_id)
                self.cas(prior, body)
                allowed = {"cancelled", "interrupted", "partial", "failed"} if operation == "resume" else {"completed", "partial", "failed"}
                if (prior is None and operation == "resume") or (prior is not None and prior.state not in allowed):
                    raise WorkspaceError("TASK_RESUME_REQUIRED" if prior and prior.state in {"cancelled", "interrupted"} else "TASK_STATE_CONFLICT")
                if session.scalar(select(UploadedFile.id).where(UploadedFile.analysis_id == analysis_id).limit(1)) is None:
                    raise WorkspaceError("ANALYSIS_HAS_NO_FILES")
                previous = (analysis.status, analysis.current_stage, analysis.progress)
                run = TaskRun(analysis_id=analysis_id, company_id=company_id,
                    parent_run_id=prior.id if operation == "resume" and prior else None, owner_nonce=self.owner_nonce)
                session.add(run)
                session.flush()
                session.add(TaskRequest(id=str(body.request_id), analysis_id=analysis_id, company_id=company_id,
                    run_id=run.id, operation=operation,
                    expected_run_id=str(body.expected_run_id) if body.expected_run_id else None,
                    expected_version=body.expected_version))
                analysis.status, analysis.current_stage, analysis.progress = "queued", "queued", max(1, analysis.progress)
                execution["run_id"] = run.id
                control = RunControl(self, run.id)
                self.controls[run.id] = control

            def rollback():
                try:
                    with self.guard, self.write() as session:
                        run = session.get(TaskRun, execution["run_id"])
                        receipt = session.get(TaskRequest, str(body.request_id))
                        if receipt.outcome == "pending":
                            receipt.outcome, receipt.error_code = "rejected", "TASK_SUBMISSION_FAILED"
                            self.transition(run, "cancelled" if run.state == "cancel_requested" else "failed")
                            analysis = session.get(AnalysisBatch, analysis_id)
                            analysis.status, analysis.current_stage, analysis.progress = previous
                        self.controls.pop(run.id, None)
                finally:
                    published.set()
            return rollback

        def publish():
            with self.guard, self.write() as session:
                receipt = session.get(TaskRequest, str(body.request_id))
                receipt.outcome = "accepted"
            execution["accepted"] = True
            published.set()

        def execute():
            published.wait()
            if not execution["accepted"]:
                return {"status": "not_submitted"}
            return self.run(analysis_id, execution["run_id"])

        try:
            self.queue.submit(analysis_id, prepare=prepare, execute=execute, publish=publish)
        except Exception as error:
            # prepare can fail before it returns the queue's rollback callback.
            # No enqueued publication waiter or invocation control may be stranded.
            if not execution["accepted"]:
                with self.guard:
                    self.controls.pop(execution["run_id"], None)
                published.set()
            if str(error) == "DESKTOP_ANALYSIS_BUSY":
                raise WorkspaceError("DESKTOP_ANALYSIS_BUSY") from None
            raise

    def run(self, analysis_id, run_id):
        control = self.controls[run_id]
        result = {"status": "failed"}
        try:
            with self.guard, self.write() as session:
                control.checkpoint()
                self.transition(session.get(TaskRun, run_id), "running")
            with owned_invocation(control):
                result = dict(self.queue.pipeline_factory().process(analysis_id))
        except ProcessingStopped:
            pass
        except Exception:
            result = {"status": "failed"}
        finally:
            with self.guard, self.write() as session:
                run = session.get(TaskRun, run_id)
                if run.state in ACTIVE:
                    terminal = "cancelled" if run.state == "cancel_requested" else "interrupted" if control.interrupted else (
                        result["status"] if result["status"] in {"completed", "partial", "failed"} else "completed")
                    self.transition(run, terminal)
                    if terminal in {"cancelled", "interrupted", "failed"}:
                        self.interrupted_files(session, analysis_id)
                    if terminal in {"cancelled", "interrupted"}:
                        self.stopped_business(session, analysis_id, terminal)
                self.controls.pop(run_id, None)
        return result


def task_router(tasks):
    router = APIRouter()
    @router.get("/api/analyses/{analysis_id}/task")
    def metadata(analysis_id: str, company_id: UUID | None = None,
                 limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
        return tasks.metadata(analysis_id, str(company_id) if company_id else None, limit, offset)

    @router.get("/api/analyses/{analysis_id}/task/requests/{request_id}")
    def lookup(analysis_id: str, request_id: UUID, company_id: UUID | None = None):
        return tasks.lookup(analysis_id, str(company_id) if company_id else None, str(request_id))

    @router.post("/api/analyses/{analysis_id}/task/{operation}", status_code=202)
    def command(analysis_id: str, operation: str, body: TaskCommand):
        if operation not in {"start", "resume", "cancel"}:
            raise WorkspaceError("TASK_OPERATION_INVALID", 422)
        return tasks.command(analysis_id, operation, body)
    return router
