from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from qian_labor.desktop.queue import DesktopProcessingQueue


class SlowPipeline:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def process(self, analysis_id: str) -> dict[str, object]:
        self.started.set()
        self.release.wait(timeout=5)
        return {"analysis_id": analysis_id, "status": "completed"}


class ImmediateExecutor:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def submit(self, fn, *args, **kwargs):
        future: Future[dict[str, object]] = Future()
        future.set_result(fn(*args, **kwargs))
        return future

    def shutdown(self, *args, **kwargs) -> None:
        pass


class ImmediatePipeline:
    def process(self, analysis_id: str) -> dict[str, object]:
        return {"analysis_id": analysis_id, "status": "completed"}


def test_submit_returns_immediately_and_only_one_analysis_can_run() -> None:
    started = threading.Event()
    release = threading.Event()
    queue = DesktopProcessingQueue(lambda: SlowPipeline(started, release))
    try:
        before = time.monotonic()
        result = queue.submit("analysis-one")
        elapsed = time.monotonic() - before

        assert result == {
            "analysis_id": "analysis-one",
            "status": "queued",
            "queue_mode": "desktop",
        }
        assert elapsed < 0.5
        assert started.wait(timeout=2)
        assert queue.max_workers == 1
        assert queue.is_busy is True

        with pytest.raises(RuntimeError, match="DESKTOP_ANALYSIS_BUSY"):
            queue.submit("analysis-two")

        release.set()
        deadline = time.monotonic() + 3
        while queue.is_busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queue.is_busy is False
    finally:
        release.set()
        queue.shutdown()


def test_queue_accepts_next_analysis_after_previous_job_finishes() -> None:
    started = threading.Event()
    release = threading.Event()
    queue = DesktopProcessingQueue(lambda: SlowPipeline(started, release))
    try:
        queue.submit("analysis-one")
        assert started.wait(timeout=2)
        release.set()
        deadline = time.monotonic() + 3
        while queue.is_busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queue.is_busy is False

        started.clear()
        release.clear()
        queue.submit("analysis-two")
        assert started.wait(timeout=2)
    finally:
        release.set()
        queue.shutdown()


def test_already_completed_future_cannot_deadlock_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("qian_labor.desktop.queue.ThreadPoolExecutor", ImmediateExecutor)
    queue = DesktopProcessingQueue(ImmediatePipeline)
    result: dict[str, object] = {}

    def submit() -> None:
        result.update(queue.submit("instant-analysis"))

    thread = threading.Thread(target=submit, daemon=True)
    thread.start()
    thread.join(timeout=0.5)

    assert thread.is_alive() is False
    assert result["analysis_id"] == "instant-analysis"
