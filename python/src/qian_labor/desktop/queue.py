from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
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
        self._submitting_analysis_id: str | None = None
        self._mutating: set[str] = set()
        self._closed = False
        self._submission_done = Event()
        self._submission_done.set()

    @property
    def is_busy(self) -> bool:
        with self._lock:
            if self._active is not None and self._active.done():
                self._active = None
                self._active_analysis_id = None
            return self._active is not None or self._submitting_analysis_id is not None

    @property
    def active_analysis_id(self) -> str | None:
        with self._lock:
            if self._active is not None and self._active.done():
                self._active = None
                self._active_analysis_id = None
            return self._submitting_analysis_id or self._active_analysis_id

    @contextmanager
    def mutation(self, analysis_id: str) -> Iterator[None]:
        """Reserve one analysis without holding the mutex during file/DB work."""
        with self._lock:
            active = self._active is not None and not self._active.done()
            if (analysis_id in self._mutating
                    or self._submitting_analysis_id == analysis_id
                    or (active and self._active_analysis_id == analysis_id)):
                raise RuntimeError("DESKTOP_ANALYSIS_BUSY")
            self._mutating.add(analysis_id)
        try:
            yield
        finally:
            with self._lock:
                self._mutating.remove(analysis_id)

    def submit(
        self, analysis_id: str, *, prepare: Callable[[], Callable[[], None] | None] | None = None,
        execute: Callable[[], dict[str, object]] | None = None,
        publish: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if (self._closed or self._submitting_analysis_id is not None or analysis_id in self._mutating
                    or (self._active is not None and not self._active.done())):
                raise RuntimeError("DESKTOP_ANALYSIS_BUSY")
            self._submitting_analysis_id = analysis_id
            self._submission_done.clear()
        rollback = None
        try:
            if prepare is not None:
                rollback = prepare()
            future = self._executor.submit(execute or self._run, *(() if execute else (analysis_id,)))
            with self._lock:
                self._active = future
                self._active_analysis_id = analysis_id
            if publish is not None:
                publish()
        except Exception:
            if rollback is not None:
                rollback()
            raise
        finally:
            with self._lock:
                self._submitting_analysis_id = None
                self._submission_done.set()
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
        with self._lock:
            self._closed = True
        self._submission_done.wait()
        self._executor.shutdown(wait=True, cancel_futures=False)
