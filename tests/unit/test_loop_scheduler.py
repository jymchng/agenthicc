"""Deterministic tests for PRD-193's session-scoped loop scheduler."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.config import LoopSettings, load_config
from agenthicc.runners.loop_scheduler import (
    LoopLifecycle,
    LoopManager,
    LoopPayloadKind,
    LoopRecord,
    LoopStorageError,
    LoopStore,
    format_loop_duration,
    parse_loop_duration,
)


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _settings(**overrides: object) -> LoopSettings:
    values: dict[str, object] = {
        "default_interval_s": 60,
        "min_interval_s": 1,
        "max_interval_s": 86_400,
        "max_age_s": 3_600,
        "busy_poll_s": 1.0,
    }
    values.update(overrides)
    return LoopSettings(**values)  # type: ignore[arg-type]


def test_duration_parser_is_strict_and_format_is_compact() -> None:
    assert parse_loop_duration("5m") == 300
    assert parse_loop_duration("2H") == 7_200
    assert parse_loop_duration("1d") == 86_400
    assert format_loop_duration(7_200) == "2h"
    assert format_loop_duration(61) == "61s"
    for value in ("", "5", "-1m", "1w", "1.5h", "0s"):
        with pytest.raises(ValueError):
            parse_loop_duration(value)


def test_loop_settings_validate_finite_bounds() -> None:
    LoopSettings().validate()
    with pytest.raises(ValueError, match="max_interval_s"):
        LoopSettings(min_interval_s=100, max_interval_s=10).validate()
    with pytest.raises(ValueError, match="default_interval_s"):
        LoopSettings(default_interval_s=1, min_interval_s=60).validate()


def test_loop_settings_are_loaded_from_toml(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    path.write_text(
        "[loops]\n"
        "default_interval_s = 120\n"
        "min_interval_s = 30\n"
        "max_interval_s = 900\n"
        "max_age_s = 1800\n"
        "allow_slash_commands = false\n",
        encoding="utf-8",
    )
    config = load_config(config_path=path)
    assert config.loops.default_interval_s == 120
    assert config.loops.min_interval_s == 30
    assert config.loops.allow_slash_commands is False


def test_record_store_round_trip_and_identity_check(tmp_path) -> None:
    store = LoopStore(tmp_path, "session-1")
    record = LoopRecord(
        loop_id="loop-1",
        session_id="session-1",
        conversation_id="session-1",
        payload_kind=LoopPayloadKind.PROMPT,
        payload="inspect the fixture",
        interval_s=60,
        created_at=1.0,
        updated_at=1.0,
        next_due_at=1.0,
        expires_at=100.0,
    )
    store.save(record)
    assert store.load() == record

    store.path.write_text('{"session_id": "other"}', encoding="utf-8")
    with pytest.raises(LoopStorageError):
        store.load()


def test_store_lists_jobs_and_manager_can_run_or_delete_selected_job(tmp_path) -> None:
    first_store = LoopStore(tmp_path, "session-1")
    second_store = LoopStore(tmp_path, "session-2")
    first = LoopRecord(
        loop_id="loop-1",
        session_id="session-1",
        conversation_id="session-1",
        payload_kind=LoopPayloadKind.PROMPT,
        payload="inspect one",
        interval_s=60,
        created_at=1.0,
        updated_at=2.0,
        next_due_at=100.0,
        expires_at=1_000.0,
    )
    second = LoopRecord(
        loop_id="loop-2",
        session_id="session-2",
        conversation_id="session-2",
        payload_kind=LoopPayloadKind.COMMAND,
        payload="/status",
        interval_s=120,
        created_at=1.0,
        updated_at=3.0,
        next_due_at=100.0,
        expires_at=1_000.0,
    )
    first_store.save(first)
    second_store.save(second)

    assert [record.loop_id for record in LoopStore.list_all(tmp_path)] == ["loop-2", "loop-1"]

    manager = LoopManager(
        session_id="session-1",
        settings=_settings(),
        store=first_store,
        is_idle=lambda: True,
        dispatch=lambda record: asyncio.sleep(0),
        event_sink=lambda kind, payload: None,
    )
    assert "due now" in manager.run_job_now(first)
    assert first_store.load() is not None and first_store.load().pending is True
    assert "deleted" in manager.delete_job(second)
    assert second_store.load() is None


@pytest.mark.asyncio
async def test_loop_dispatches_immediately_then_uses_actual_completion_cadence(tmp_path) -> None:
    clock = FakeClock()
    dispatched: list[str] = []
    events: list[str] = []

    async def dispatch(record: LoopRecord) -> None:
        dispatched.append(record.payload)

    manager = LoopManager(
        session_id="session-1",
        settings=_settings(),
        store=LoopStore(tmp_path, "session-1"),
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: events.append(kind),
        clock=clock,
    )
    assert "scheduled" in manager.handle_command("1m inspect the fixture")
    assert await manager.tick_once() is True
    assert dispatched == ["inspect the fixture"]
    assert manager.record is not None
    assert manager.record.state is LoopLifecycle.SCHEDULED
    assert manager.record.next_due_at == 1_060.0
    assert "loop_dispatch_started" in events
    assert "loop_dispatch_completed" in events

    clock.advance(60)
    assert await manager.tick_once() is True
    assert dispatched == ["inspect the fixture", "inspect the fixture"]


@pytest.mark.asyncio
async def test_busy_ticks_are_coalesced_and_never_overlap(tmp_path) -> None:
    clock = FakeClock()
    idle = False
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def dispatch(record: LoopRecord) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    manager = LoopManager(
        session_id="session-1",
        settings=_settings(),
        store=LoopStore(tmp_path, "session-1"),
        is_idle=lambda: idle,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    manager.handle_command("1m inspect")
    assert await manager.tick_once() is False
    assert manager.record is not None
    assert manager.record.state is LoopLifecycle.WAITING_FOR_IDLE

    idle = True
    clock.advance(60)
    running = asyncio.create_task(manager.tick_once())
    await entered.wait()
    assert await manager.tick_once() is False
    assert calls == 1
    release.set()
    assert await running is True


@pytest.mark.asyncio
async def test_failure_backoff_and_terminal_failure(tmp_path) -> None:
    clock = FakeClock()
    failures = 0

    async def dispatch(record: LoopRecord) -> None:
        nonlocal failures
        failures += 1
        raise RuntimeError("provider unavailable")

    manager = LoopManager(
        session_id="session-1",
        settings=_settings(max_consecutive_failures=2),
        store=LoopStore(tmp_path, "session-1"),
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    manager.handle_command("1m inspect")
    assert await manager.tick_once() is True
    assert manager.record is not None
    assert manager.record.state is LoopLifecycle.SCHEDULED
    assert manager.record.last_error == "RuntimeError: provider unavailable"
    clock.advance(2)
    assert await manager.tick_once() is True
    assert manager.record.state is LoopLifecycle.FAILED
    assert failures == 2
    assert manager.status_text().startswith("Loop failed")


@pytest.mark.asyncio
async def test_pause_during_running_iteration_wins_after_completion(tmp_path) -> None:
    clock = FakeClock()
    manager: LoopManager | None = None

    async def dispatch(record: LoopRecord) -> None:
        assert manager is not None
        assert manager.handle_command("pause") == "Loop paused."

    manager = LoopManager(
        session_id="session-1",
        settings=_settings(),
        store=LoopStore(tmp_path, "session-1"),
        is_idle=lambda: True,
        dispatch=dispatch,
        event_sink=lambda kind, payload: None,
        clock=clock,
    )
    manager.handle_command("1m inspect")
    assert await manager.tick_once() is True
    assert manager.record is not None
    assert manager.record.state is LoopLifecycle.PAUSED


def test_command_validation_and_replacement(tmp_path) -> None:
    manager = LoopManager(
        session_id="session-1",
        settings=_settings(),
        store=LoopStore(tmp_path, "session-1"),
        is_idle=lambda: True,
        dispatch=lambda record: asyncio.sleep(0),
        event_sink=lambda kind, payload: None,
        payload_validator=lambda payload, kind: (
            "unknown command" if kind is LoopPayloadKind.COMMAND and payload != "/status" else None
        ),
    )
    assert "unknown" in manager.handle_command("/missing")
    assert "interval must look" in manager.handle_command("5x inspect")
    assert "scheduled" in manager.handle_command("1m first")
    first_id = manager.record.loop_id if manager.record else ""
    assert "scheduled" in manager.handle_command("1m second")
    assert manager.record is not None and manager.record.loop_id != first_id
    assert manager.handle_command("pause") == "Loop paused."
    assert manager.handle_command("resume") == "Loop resumed; one iteration is due now."
    assert manager.handle_command("stop") == "Loop stopped."
    assert manager.handle_command("stop") == "Loop is already stopped."


def test_disabled_scheduler_can_stop_persisted_work_without_restarting_it(tmp_path) -> None:
    settings = _settings(enabled=True)
    store = LoopStore(tmp_path, "session-1")
    manager = LoopManager(
        session_id="session-1",
        settings=settings,
        store=store,
        is_idle=lambda: True,
        dispatch=lambda record: asyncio.sleep(0),
        event_sink=lambda kind, payload: None,
    )
    assert "scheduled" in manager.handle_command("1m inspect")

    disabled = _settings(enabled=False)
    recovered = LoopManager(
        session_id="session-1",
        settings=disabled,
        store=store,
        is_idle=lambda: True,
        dispatch=lambda record: asyncio.sleep(0),
        event_sink=lambda kind, payload: None,
    )
    assert (
        recovered.handle_command("1m new prompt") == "Loop scheduling is disabled by configuration."
    )
    assert recovered.handle_command("stop") == "Loop stopped."
