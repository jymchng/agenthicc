"""Reactive primitives: Signal and Computed.

Signal  — a mutable cell that notifies subscribers on change.
Computed — a read-only derived value that recomputes when its signals change.

These are the foundation of PRD-59 (Reactive State Graph).  Every piece of
state that drives the TUI lives in Signals; every derived view is a Computed.

A subscriber that raises does not stop the others: the failure is logged with
the cell's name — with a traceback the first time a given subscriber fails,
and at debug level after that, so a broken per-frame subscriber cannot flood
the log.

Thread-safety
-------------
Signal.set() holds a lock only for the value swap; subscriber calls happen
outside the lock so callbacks may call Signal.set() without deadlocking.
All mutations in normal operation come from the asyncio event-loop thread.
The lock exists as a safety net for the atexit / signal-handler edge cases
in cbreak_reader.raw_mode.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Generic, Protocol, TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)

Subscriber = Callable[[], None]


class _Subscribable(Protocol):
    def subscribe(self, fn: Subscriber) -> Callable[[], None]: ...


def _notify(
    kind: str,
    name: str,
    subscribers: list[Subscriber],
    failed: set[int],
) -> None:
    """Call every subscriber; log a failure without skipping the rest.

    *failed* holds the ids of subscribers whose failure was already logged
    with a traceback; subscribers need not be hashable.
    """
    for sub in list(subscribers):
        try:
            sub()
        except Exception:
            if id(sub) in failed:
                log.debug("%s %r subscriber %r failed again", kind, name, sub, exc_info=True)
            else:
                failed.add(id(sub))
                log.exception("%s %r subscriber %r failed", kind, name, sub)


class Signal(Generic[T]):
    """A reactive cell.  Writes notify all subscribers synchronously."""

    __slots__ = ("_value", "_subscribers", "_lock", "_name", "_failed")

    def __init__(self, initial: T, *, name: str = "") -> None:
        self._value: T = initial
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._name = name or "<unnamed>"
        self._failed: set[int] = set()

    @property
    def name(self) -> str:
        """The name used when a subscriber failure is logged."""
        return self._name

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self) -> T:
        return self._value

    def __call__(self) -> T:  # sugar: signal() instead of signal.get()
        return self._value

    # ── write ─────────────────────────────────────────────────────────────────

    def set(self, value: T) -> None:
        with self._lock:
            if value == self._value:
                return
            self._value = value
        # Notify outside the lock so callbacks can call set() safely.
        _notify("signal", self._name, self._subscribers, self._failed)

    # ── subscription ──────────────────────────────────────────────────────────

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register *fn* to be called on every value change.

        Returns an unsubscribe callable.
        """
        self._subscribers.append(fn)
        return lambda: self._safely_remove(fn)

    def _safely_remove(self, fn: Subscriber) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass
        self._failed.discard(id(fn))


class Computed(Generic[T]):
    """A read-only value derived from one or more Signals.

    The value is recomputed eagerly whenever any dependency changes, until
    :meth:`dispose` releases the dependency subscriptions.
    """

    __slots__ = ("_fn", "_value", "_subscribers", "_name", "_failed", "_dependency_unsubscribers")

    def __init__(self, fn: Callable[[], T], *deps: _Subscribable, name: str = "") -> None:
        self._fn = fn
        self._value: T = fn()
        self._subscribers: list[Subscriber] = []
        self._name = name or "<unnamed>"
        self._failed: set[int] = set()
        self._dependency_unsubscribers = [dep.subscribe(self._recompute) for dep in deps]

    @property
    def name(self) -> str:
        """The name used when a subscriber failure is logged."""
        return self._name

    def _recompute(self) -> None:
        new = self._fn()
        if new != self._value:
            self._value = new
            _notify("computed", self._name, self._subscribers, self._failed)

    def get(self) -> T:
        return self._value

    def __call__(self) -> T:
        return self._value

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._safely_remove(fn)

    def _safely_remove(self, fn: Subscriber) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass
        self._failed.discard(id(fn))

    def dispose(self) -> None:
        """Stop following the dependencies and drop every subscriber.

        The last computed value stays readable. Disposing twice is harmless.
        """
        for unsubscribe in self._dependency_unsubscribers:
            unsubscribe()
        self._dependency_unsubscribers = []
        self._subscribers.clear()
        self._failed.clear()
