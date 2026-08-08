"""Execution identity shared across runtime and presentation layers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True)
class ExecutionIdentity:
    thread_id: str = ""
    session_id: str = ""


_CURRENT_EXECUTION_IDENTITY: ContextVar[ExecutionIdentity] = ContextVar(
    "voidx_execution_identity",
    default=ExecutionIdentity(),
)


def current_execution_identity() -> ExecutionIdentity:
    return _CURRENT_EXECUTION_IDENTITY.get()


@contextmanager
def bind_execution_identity(identity: ExecutionIdentity) -> Iterator[ExecutionIdentity]:
    token = _CURRENT_EXECUTION_IDENTITY.set(identity)
    try:
        yield identity
    finally:
        _CURRENT_EXECUTION_IDENTITY.reset(token)
