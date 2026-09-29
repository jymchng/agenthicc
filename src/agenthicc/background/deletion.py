"""Typed deletion requests and outcomes for background sessions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeleteFailure:
    """One target that could not be moved to recoverable trash."""

    session_id: str
    code: str
    message: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Structured result for one idempotent deletion operation."""

    operation_id: str
    deleted: tuple[str, ...] = ()
    failures: tuple[DeleteFailure, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every requested target completed successfully."""

        return not self.failures

    @property
    def phase(self) -> str:
        """Return a stable terminal phase for UI and diagnostics."""

        return "completed" if self.ok else "failed"


__all__ = ["DeleteFailure", "DeleteResult"]
