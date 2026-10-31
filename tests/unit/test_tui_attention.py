from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, time
from pathlib import Path

import pytest

from agenthicc.background.agents_service import AgentsService
from agenthicc.background.attention import (
    _BackgroundAttentionSnapshot,
    _load_attention_snapshot,
)
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.settings import (
    BackgroundNotificationSettings,
    BackgroundSettings,
    QuietHoursSettings,
)
from agenthicc.background.store import BackgroundStore
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.tui.attention import AgentAttentionRouter
from agenthicc.tui.conversation_store import AppState

pytestmark = pytest.mark.unit


def _session(session_id: str, status: SessionStatus) -> BackgroundSession:
    return BackgroundSession.create(
        session_id,
        title="private title",
        cwd=f"/workspace/{session_id}",
        workflow_name="goal_flow",
        intent="private prompt",
        now=1.0,
    ).evolve(status=status, state_changed_at=2.0)


def _snapshot(
    sessions: tuple[BackgroundSession, ...] = (),
    *,
    needs_you: int = 0,
    running: int = 0,
    pending_item_ids: frozenset[str] = frozenset(),
    pending_session_ids: frozenset[str] = frozenset(),
) -> _BackgroundAttentionSnapshot:
    return _BackgroundAttentionSnapshot(
        sessions=sessions,
        needs_you_count=needs_you,
        running_count=running,
        pending_item_ids=pending_item_ids,
        pending_session_ids=pending_session_ids,
        oldest_item=None,
    )


def _router(
    *,
    settings: BackgroundSettings | None = None,
    app_state: AppState | None = None,
    monotonic: float = 100.0,
    now: datetime | None = None,
    set_terminal_title: list[str] | None = None,
    send_terminal_notification: list[str] | None = None,
) -> AgentAttentionRouter:
    return AgentAttentionRouter(
        object(),
        settings or BackgroundSettings(),
        app_state or AppState.create(),
        monotonic=lambda: monotonic,
        now=lambda: now or datetime.now().astimezone(),
        set_terminal_title=(set_terminal_title.append if set_terminal_title is not None else None),
        send_terminal_notification=(
            send_terminal_notification.append if send_terminal_notification is not None else None
        ),
    )


@pytest.mark.asyncio
async def test_attention_snapshot_counts_waiting_and_running_sessions(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path)
    store.create(_session("running", SessionStatus.RUNNING))
    store.create(_session("waiting", SessionStatus.RUNNING))
    store.transition("waiting", SessionStatus.WAITING_INPUT)
    service = AgentsService(store, BackgroundSupervisor(store))

    try:
        snapshot = await _load_attention_snapshot(service)
    finally:
        await service.close()

    assert snapshot.needs_you_count == 1
    assert snapshot.running_count == 1
    assert {session.session_id for session in snapshot.sessions} == {"running", "waiting"}


def test_router_publishes_counts_target_and_terminal_title() -> None:
    app_state = AppState.create()
    titles: list[str] = []
    router = _router(app_state=app_state, set_terminal_title=titles)
    waiting = _session("session-a", SessionStatus.WAITING_INPUT)

    router._publish_counts(_snapshot((waiting,), needs_you=1, running=2))

    assert app_state.conversation.background_needs_you() == 1
    assert app_state.conversation.background_running() == 2
    assert app_state.conversation.agents_target() == "session-a"
    assert titles[-1] == "Agenthicc · 1 need you · 2 running"


def test_router_coalesces_waiting_transition_and_defers_for_modal() -> None:
    app_state = AppState.create()
    router = _router(app_state=app_state)
    before = _snapshot(
        (
            _session("session-a", SessionStatus.RUNNING),
            _session("session-b", SessionStatus.RUNNING),
        ),
        running=2,
    )
    after = _snapshot(
        (
            _session("session-a", SessionStatus.WAITING_INPUT),
            _session("session-b", SessionStatus.COMPLETED),
        ),
        needs_you=1,
    )

    router._observe(before, after)
    app_state.modal_open.set(True)
    router._flush_if_ready(102.0)

    assert app_state.conversation.background_toast() is None
    assert router._next_timeout(102.0) == 8.0

    app_state.modal_open.set(False)
    router._flush_if_ready(102.0)
    assert app_state.conversation.background_toast() == "✓ 1 agents finished · ◆ 1 need you"
    assert app_state.conversation.agents_toast_target() == "session-a"


def test_dnd_discards_attention_not_badge_counts() -> None:
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            local_enabled=True,
            local_backend="desktop",
        )
    )
    app_state = AppState.create()
    app_state.conversation.terminal_focused.set(False)
    router = _router(settings=settings, app_state=app_state)
    waiting = _session("session-a", SessionStatus.WAITING_INPUT)
    snapshot = _snapshot((waiting,), needs_you=1)

    router._publish_counts(snapshot)
    router._observe(_snapshot((_session("session-a", SessionStatus.RUNNING),)), snapshot)
    app_state.conversation.background_dnd.set(True)
    router._flush_if_ready(102.0)

    assert app_state.conversation.background_needs_you() == 1
    assert app_state.conversation.background_toast() is None
    assert app_state.conversation.agents_hint() is False
    assert not router._notification_tasks


def test_quiet_hours_discard_pending_toast() -> None:
    local_now = datetime.now().astimezone().replace(hour=23, minute=30, second=0, microsecond=0)
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            quiet_hours=QuietHoursSettings(time(22, 0), time(6, 0))
        )
    )
    app_state = AppState.create()
    router = _router(settings=settings, app_state=app_state, now=local_now)
    router._observe(
        _snapshot((_session("session-a", SessionStatus.RUNNING),)),
        _snapshot((_session("session-a", SessionStatus.WAITING_INPUT),), needs_you=1),
    )

    router._flush_if_ready(102.0)

    assert app_state.conversation.background_toast() is None


@pytest.mark.asyncio
async def test_system_notification_is_sent_only_when_terminal_is_unfocused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background import notifications

    delivered: list[tuple[dict[str, object], str]] = []

    def send(payload: Mapping[str, object], backend: str) -> str:
        delivered.append((dict(payload), backend))
        return "delivered"

    monkeypatch.setattr(notifications, "_send_local_notification", send)
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            local_enabled=True,
            local_backend="desktop",
        )
    )
    app_state = AppState.create()
    router = _router(settings=settings, app_state=app_state)
    router._queue_notice("finished", "session-a", "/workspace/session-a")
    router._flush_if_ready(102.0)
    assert not router._notification_tasks

    app_state.conversation.terminal_focused.set(False)
    router._queue_notice("finished", "session-b", "/workspace/session-b")
    router._flush_if_ready(102.0)
    tasks = tuple(router._notification_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert delivered == [
        (
            {"workspace": "agents", "summary": "session-b finished"},
            "desktop",
        )
    ]


@pytest.mark.asyncio
async def test_coalesced_batch_emits_one_system_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background import notifications

    delivered: list[tuple[dict[str, object], str]] = []

    def send(payload: Mapping[str, object], backend: str) -> str:
        delivered.append((dict(payload), backend))
        return "delivered"

    monkeypatch.setattr(notifications, "_send_local_notification", send)
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            local_enabled=True,
            local_backend="desktop",
        )
    )
    app_state = AppState.create()
    app_state.conversation.terminal_focused.set(False)
    router = _router(settings=settings, app_state=app_state)
    router._queue_notice("finished", "session-a", "/workspace/session-a")
    router._queue_notice("failed", "session-b", "/workspace/session-b")

    router._flush_if_ready(102.0)
    tasks = tuple(router._notification_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert delivered == [
        (
            {"workspace": "agents", "summary": "1 agents finished · 1 failed"},
            "desktop",
        )
    ]


@pytest.mark.asyncio
async def test_terminal_backend_uses_tui_notification_escape_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background import notifications

    def unexpected_send(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("terminal notifications must not use the stderr sender")

    monkeypatch.setattr(notifications, "_send_local_notification", unexpected_send)
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            local_enabled=True,
            local_backend="terminal",
        )
    )
    emitted: list[str] = []
    app_state = AppState.create()
    app_state.conversation.terminal_focused.set(False)
    router = _router(
        settings=settings,
        app_state=app_state,
        send_terminal_notification=emitted,
    )
    router._queue_notice("finished", "session-a", "/workspace/session-a")

    router._flush_if_ready(102.0)
    await asyncio.gather(*tuple(router._notification_tasks))

    assert emitted == ["session-a finished"]


@pytest.mark.asyncio
async def test_auto_backend_falls_back_to_tui_notification_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background import notifications

    monkeypatch.setattr(
        notifications,
        "_send_local_notification",
        lambda _payload, _backend: "local_notifier_unavailable",
    )
    settings = BackgroundSettings(
        notifications=BackgroundNotificationSettings(
            local_enabled=True,
            local_backend="auto",
        )
    )
    emitted: list[str] = []
    app_state = AppState.create()
    app_state.conversation.terminal_focused.set(False)
    router = _router(
        settings=settings,
        app_state=app_state,
        send_terminal_notification=emitted,
    )
    router._queue_notice("failed", "session-a", "/workspace/session-a")

    router._flush_if_ready(102.0)
    await asyncio.gather(*tuple(router._notification_tasks))

    assert emitted == ["session-a failed"]


def test_terminal_notification_osc_filters_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from rich.console import Console

    from agenthicc.tui.attention import _emit_terminal_notification

    monkeypatch.setenv("TERM", "xterm-256color")
    stream = StringIO()
    _emit_terminal_notification(
        Console(file=stream, force_terminal=True),
        "ready\n\x1b]52;clipboard\x07",
    )

    assert stream.getvalue() == "\x1b]9;ready]52;clipboard\x07"


@pytest.mark.asyncio
async def test_router_shows_a_temporary_agents_hint_on_start(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path)
    service = AgentsService(store, BackgroundSupervisor(store))
    app_state = AppState.create()
    router = AgentAttentionRouter(service, BackgroundSettings(), app_state)
    task = asyncio.create_task(router.run())

    try:
        for _ in range(50):
            if app_state.conversation.agents_hint():
                break
            await asyncio.sleep(0.01)
        assert app_state.conversation.agents_hint()
        assert router._hint_deadline is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_attention_projection_does_not_change_composer_or_cursor() -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import CommandBus

    app_state = AppState.create()
    input_session = UnifiedInputSession(app_state, CommandBus())
    for character in "prompt":
        await input_session._dispatch(Key.CHAR, character)
    await input_session._dispatch(Key.LEFT, "")
    before = (tuple(app_state.input.buf()), app_state.input.cursor())

    router = _router(app_state=app_state)
    router._publish_counts(
        _snapshot((_session("session-a", SessionStatus.WAITING_INPUT),), needs_you=1)
    )

    assert (tuple(app_state.input.buf()), app_state.input.cursor()) == before
