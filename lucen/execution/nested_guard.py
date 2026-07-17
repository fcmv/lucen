from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_ACTIVE: ContextVar[bool] = ContextVar("lucen_dispatch_active", default=False)


def active() -> bool:
    return _ACTIVE.get()


@contextmanager
def dispatch_scope() -> Iterator[None]:
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
