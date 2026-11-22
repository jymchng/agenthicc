"""Carry a durable workflow return key to the first parent model turn."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


_return_key: ContextVar[str | None] = ContextVar("workflow_return_key", default=None)


def workflow_digest(text: str) -> str:
    """Return the stable digest written to the kernel log."""
    return sha256(text.encode("utf-8")).hexdigest()[:16]


@contextmanager
def bind_workflow_return(key: str) -> Iterator[None]:
    token = _return_key.set(key)
    try:
        yield
    finally:
        _return_key.reset(token)


def consume_workflow_return_key() -> str | None:
    key = _return_key.get()
    if key is not None:
        _return_key.set(None)
    return key
