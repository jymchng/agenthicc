"""Reactive primitives: Signal and Computed.

Signal  — a mutable cell that notifies subscribers on change.
Computed — a read-only derived value that recomputes when its signals change.

These are the foundation of PRD-59 (Reactive State Graph).  Every piece of
state that drives the TUI lives in Signals; every derived view is a Computed.

Thread-safety
-------------
Signal.set() holds a lock only for the value swap; subscriber calls happen
outside the lock so callbacks may call Signal.set() without deadlocking.
All mutations in normal operation come from the asyncio event-loop thread.
The lock exists as a safety net for the atexit / signal-handler edge cases
in cbreak_reader.raw_mode.
"""
from __future__ import annotations

import threading
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Signal(Generic[T]):
    """A reactive cell.  Writes notify all subscribers synchronously."""

    __slots__ = ("_value", "_subscribers", "_lock")

    def __init__(self, initial: T) -> None:
        self._value: T = initial
        self._subscribers: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self) -> T:
        return self._value

    def __call__(self) -> T:          # sugar: signal() instead of signal.get()
        return self._value

    # ── write ─────────────────────────────────────────────────────────────────

    def set(self, value: T) -> None:
        with self._lock:
            if value == self._value:
                return
            self._value = value
        # Notify outside the lock so callbacks can call set() safely.
        for sub in list(self._subscribers):
            try:
                sub()
            except Exception:       # noqa: BLE001
                pass

    # ── subscription ──────────────────────────────────────────────────────────

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Register *fn* to be called on every value change.

        Returns an unsubscribe callable.
        """
        self._subscribers.append(fn)
        return lambda: self._safely_remove(fn)

    def _safely_remove(self, fn: Callable[[], None]) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass


class Computed(Generic[T]):
    """A read-only value derived from one or more Signals.

    The value is recomputed eagerly whenever any dependency changes.
    """

    __slots__ = ("_fn", "_value", "_subscribers")

    def __init__(self, fn: Callable[[], T], *deps: Signal) -> None:
        self._fn = fn
        self._value: T = fn()
        self._subscribers: list[Callable[[], None]] = []

        def _recompute() -> None:
            new = fn()
            if new != self._value:
                self._value = new
                for sub in list(self._subscribers):
                    try:
                        sub()
                    except Exception:       # noqa: BLE001
                        pass

        for dep in deps:
            dep.subscribe(_recompute)

    def get(self) -> T:
        return self._value

    def __call__(self) -> T:
        return self._value

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._safely_remove(fn)

    def _safely_remove(self, fn: Callable[[], None]) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass
