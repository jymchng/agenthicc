"""Session startup phases and readiness gates (PRD-176).

Startup is split into a small synchronous core and independently observable
readiness phases.  This module intentionally contains no TUI or provider
imports so it can be used by the headless runner and by tests without loading
the full application.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeVar

__all__ = [
    "ReadinessState",
    "StartupDependencyError",
    "StartupPhaseReport",
    "StartupCoordinator",
]

T = TypeVar("T")

_SECRET_ERROR_RE = re.compile(
    r"(?i)(authorization|api[_ -]?key|token|secret|password|passwd|cookie|private[_ -]?key)"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _redact_error(error: str) -> str:
    """Bound and redact exception text before it enters diagnostics."""
    redacted = _BEARER_RE.sub("Bearer <redacted>", error)
    redacted = _SECRET_ERROR_RE.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return redacted[:500]


class ReadinessState(StrEnum):
    """Lifecycle state for a startup subsystem."""

    NOT_STARTED = "not_started"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class StartupPhaseReport:
    """Redacted timing/status information for one startup phase."""

    name: str
    state: ReadinessState
    deferred: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # Set only on a read of an in-flight report. Keeping this observation
    # separate from ``finished_at`` avoids making diagnostics look complete.
    observed_at: float | None = None

    @property
    def elapsed_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = (
            self.finished_at
            if self.finished_at is not None
            else self.observed_at
            if self.observed_at is not None
            else time.monotonic()
        )
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict[str, object]:
        """Return a safe JSON-compatible report."""
        return {
            "name": self.name,
            "state": self.state.value,
            "deferred": self.deferred,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
        }


class StartupDependencyError(RuntimeError):
    """Raised when an operation requires a phase that failed or was cancelled."""

    def __init__(self, phase: str, report: StartupPhaseReport) -> None:
        self.phase = phase
        self.report = report
        detail = report.error or report.state.value
        super().__init__(f"startup phase {phase!r} is unavailable: {detail}")


class StartupCoordinator:
    """Own phase reports and cancellable background readiness tasks."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._reports: dict[str, StartupPhaseReport] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._subscribers: list[Callable[[], None]] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the session has begun shutting down."""
        return self._closed

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to readiness changes and return an idempotent remover."""
        self._subscribers.append(callback)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _notify(self) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback()
            except Exception:  # noqa: BLE001
                # Observers are diagnostic/rendering hooks. A broken observer
                # must never change the startup outcome.
                continue

    def begin(self, name: str, *, deferred: bool = False) -> None:
        """Mark a phase as loading, replacing any previous report."""
        if not name:
            raise ValueError("startup phase name must not be empty")
        self._reports[name] = StartupPhaseReport(
            name=name,
            state=ReadinessState.LOADING,
            deferred=deferred,
            started_at=self._clock(),
        )
        self._events.setdefault(name, asyncio.Event()).clear()
        self._notify()

    def finish(
        self,
        name: str,
        state: ReadinessState = ReadinessState.READY,
        *,
        error: str | None = None,
    ) -> StartupPhaseReport:
        """Finish a phase with a bounded, non-sensitive error summary."""
        prior = self._reports.get(name)
        if prior is None:
            self.begin(name)
            prior = self._reports[name]
        report = StartupPhaseReport(
            name=name,
            state=state,
            deferred=prior.deferred,
            started_at=prior.started_at,
            finished_at=self._clock(),
            error=(_redact_error(error) if error else None),
        )
        self._reports[name] = report
        self._events.setdefault(name, asyncio.Event()).set()
        self._notify()
        return report

    async def run(
        self,
        name: str,
        operation: Callable[[], Awaitable[T]],
        *,
        deferred: bool = False,
        degrade_on_error: bool = False,
    ) -> T | None:
        """Run and record an async phase, optionally converting errors to degraded state."""
        self.begin(name, deferred=deferred)
        return await self._execute(name, operation, degrade_on_error=degrade_on_error)

    async def _execute(
        self,
        name: str,
        operation: Callable[[], Awaitable[T]],
        *,
        degrade_on_error: bool,
    ) -> T | None:
        """Execute an already-announced phase (used by background tasks)."""
        try:
            result = await operation()
        except asyncio.CancelledError:
            self.finish(name, ReadinessState.CANCELLED, error="cancelled")
            raise
        except Exception as exc:
            state = ReadinessState.DEGRADED if degrade_on_error else ReadinessState.FAILED
            self.finish(name, state, error=f"{type(exc).__name__}: {exc}")
            if not degrade_on_error:
                raise
            return None
        self.finish(name)
        return result

    def start_background(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
        *,
        degrade_on_error: bool = True,
    ) -> asyncio.Task[object]:
        """Start one tracked readiness operation on the current event loop."""
        if self._closed:
            raise RuntimeError("startup coordinator is closed")
        # Publish LOADING before scheduling so a caller that submits work in
        # the same event-loop turn cannot mistake the dependency for absent.
        self.begin(name, deferred=True)
        task = asyncio.create_task(
            self._execute(name, operation, degrade_on_error=degrade_on_error),
            name=f"startup-{name}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_for(self, *names: str) -> None:
        """Wait for phases and raise if any required phase is unavailable."""
        for name in names:
            report = self._reports.get(name)
            if report is None:
                raise StartupDependencyError(
                    name, StartupPhaseReport(name, ReadinessState.NOT_STARTED)
                )
            if report.state == ReadinessState.LOADING:
                await self._events.setdefault(name, asyncio.Event()).wait()
                report = self._reports[name]
            if report.state is not ReadinessState.READY:
                raise StartupDependencyError(name, report)

    def report(self, name: str) -> StartupPhaseReport:
        """Return one report, or a not-started report for diagnostics."""
        report = self._reports.get(name, StartupPhaseReport(name, ReadinessState.NOT_STARTED))
        if report.state is ReadinessState.LOADING:
            return replace(report, observed_at=self._clock())
        return report

    def snapshot(self) -> tuple[StartupPhaseReport, ...]:
        """Return reports in deterministic phase-name order."""
        return tuple(self.report(name) for name in sorted(self._reports))

    def to_dict(self) -> list[dict[str, object]]:
        """Return safe reports for TUI/headless diagnostics."""
        return [report.to_dict() for report in self.snapshot()]

    async def close(self) -> None:
        """Cancel and await outstanding readiness operations exactly once."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name, report in tuple(self._reports.items()):
            if report.state == ReadinessState.LOADING:
                self.finish(name, ReadinessState.CANCELLED, error="session closed")
