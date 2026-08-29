from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any


class DesktopProcessingQueue:
    def __init__(self, pipeline_factory: Callable[[], Any]) -> None:
        self.pipeline_factory = pipeline_factory
        self.max_workers = 1
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="qian-desktop-analysis",
        )
        self._lock = Lock()
        self._active: Future[dict[str, object]] | None = None
        self._active_analysis_id: str | None = None

    @property
    def is_busy(self) -> bool:
        with self._lock:
            if self._active is not None and self._active.done():
                self._active = None
                self._active_analysis_id = None
            return self._active is not None

    @property
    def active_analysis_id(self) -> str | None:
        with self._lock:
            if self._active is not None and self._active.done():
                self._active = None
                self._active_analysis_id = None
            return self._active_analysis_id

    def submit(self, analysis_id: str) -> dict[str, object]:
        with self._lock:
            if self._active is not None and not self._active.done():
                raise RuntimeError("DESKTOP_ANALYSIS_BUSY")
            future = self._executor.submit(self._run, analysis_id)
            self._active = future
            self._active_analysis_id = analysis_id
        # Future.add_done_callback invokes immediately when the future is already done.
        # Register only after releasing the mutex so an instant task cannot self-deadlock.
        future.add_done_callback(self._clear_completed)
        return {
            "analysis_id": analysis_id,
            "status": "queued",
            "queue_mode": "desktop",
        }

    def _run(self, analysis_id: str) -> dict[str, object]:
        result = self.pipeline_factory().process(analysis_id)
        return dict(result)

    def _clear_completed(self, future: Future[dict[str, object]]) -> None:
        with self._lock:
            if self._active is future:
                self._active = None
                self._active_analysis_id = None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
