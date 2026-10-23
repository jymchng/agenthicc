"""Signal and Computed notify reliably and report subscriber failures (PRD-250 D-06)."""

from __future__ import annotations

import logging

import pytest

from agenthicc.reactive import Computed, Signal

pytestmark = pytest.mark.unit


def test_signal_notifies_subscribers_only_on_change() -> None:
    signal: Signal[int] = Signal(0, name="count")
    calls: list[int] = []
    signal.subscribe(lambda: calls.append(signal()))

    signal.set(0)
    signal.set(1)
    signal.set(1)
    signal.set(2)

    assert calls == [1, 2]


def test_a_failing_subscriber_is_logged_and_the_rest_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    signal: Signal[str] = Signal("", name="notification")
    after: list[str] = []

    def broken() -> None:
        raise RuntimeError("render exploded")

    signal.subscribe(broken)
    signal.subscribe(lambda: after.append(signal()))

    with caplog.at_level(logging.DEBUG, logger="agenthicc.reactive"):
        signal.set("one")
        signal.set("two")

    assert after == ["one", "two"]
    first, second = caplog.records
    assert first.levelno == logging.ERROR
    assert "signal 'notification' subscriber" in first.getMessage()
    assert first.exc_info is not None and "render exploded" in str(first.exc_info[1])
    # A repeat failure of the same subscriber is kept out of the error log.
    assert second.levelno == logging.DEBUG
    assert "failed again" in second.getMessage()


def test_an_unhashable_subscriber_failure_is_still_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __call__(self) -> None:
            raise ValueError("no hash, no luck")

    signal: Signal[int] = Signal(0, name="frame")
    signal.subscribe(Unhashable())

    signal.set(1)

    assert "signal 'frame' subscriber" in caplog.text


def test_unsubscribe_stops_notifications() -> None:
    signal: Signal[int] = Signal(0)
    calls: list[int] = []
    unsubscribe = signal.subscribe(lambda: calls.append(signal()))

    signal.set(1)
    unsubscribe()
    unsubscribe()  # removing twice is harmless
    signal.set(2)

    assert calls == [1]
    assert signal.name == "<unnamed>"


def test_computed_follows_its_dependencies_and_reports_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    left: Signal[int] = Signal(1)
    right: Signal[int] = Signal(2)
    total: Computed[int] = Computed(lambda: left() + right(), left, right, name="total")
    seen: list[int] = []

    def broken() -> None:
        raise RuntimeError("footer exploded")

    total.subscribe(broken)
    total.subscribe(lambda: seen.append(total()))

    left.set(10)
    right.set(20)

    assert total() == 30
    assert seen == [12, 30]
    assert "computed 'total' subscriber" in caplog.text


def test_dispose_releases_dependency_subscriptions() -> None:
    source: Signal[int] = Signal(1)
    doubled: Computed[int] = Computed(lambda: source() * 2, source, name="doubled")
    seen: list[int] = []
    doubled.subscribe(lambda: seen.append(doubled()))

    doubled.dispose()
    doubled.dispose()  # disposing twice is harmless
    source.set(5)

    assert doubled() == 2  # the last value stays readable
    assert seen == []
    assert source._subscribers == []
