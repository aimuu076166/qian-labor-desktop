"""Invocation-local cooperative controls; generic callers have no owned task."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import time


class ProcessingStopped(Exception):
    pass


_current = ContextVar("qian_processing_control", default=None)


@contextmanager
def owned_invocation(control):
    token = _current.set(control)
    try:
        yield
    finally:
        _current.reset(token)


def checkpoint():
    control = _current.get()
    if control is not None:
        control.checkpoint()


def commit_boundary():
    control = _current.get()
    return control.commit_boundary() if control is not None else nullcontext()


def adapter_call(begin_usage):
    control = _current.get()
    if control is not None:
        return control.adapter_call(begin_usage)
    @contextmanager
    def unowned():
        begin_usage()
        yield
    return unowned()


def retry_wait(seconds):
    control = _current.get()
    if control is None:
        time.sleep(seconds)
    else:
        control.stop.wait(seconds)
        control.checkpoint()
