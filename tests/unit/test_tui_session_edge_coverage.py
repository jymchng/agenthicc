"""Additional deterministic coverage for session-owned TUI control paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.commands.command import UsageSnapshot
from agenthicc.tui.runtime import RuntimeMode

from .test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit


def test_session_helpers_project_terminal_usage_and_event_publication(
    tmp_path: Path,
) -> None:
    session, ctx, workspace, _input = _make_session()
    session._set_pending_skill("skill body")
    session._set_pending_replay("replay-id")
    assert session._pending_skill_body == ["skill body"]
    assert session._pending_replay_id == "replay-id"

    fallback = session._usage_snapshot()
    assert isinstance(fallback, UsageSnapshot)
    assert fallback.queue_depth == 0
    ctx.usage_ledger = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            input_tokens=1,
            output_tokens=2,
            cost_usd=0.1,
            usage_status="ok",
            cost_status="ok",
            calls=3,
            known_calls=2,
            unavailable_calls=0,
            provisional_calls=1,
            durability_status="durable",
        )
    )
    assert session._usage_snapshot().calls == 3

    redraws: list[bool] = []
    workspace._redraw = lambda: redraws.append(True)
    ctx.terminal_manager = SimpleNamespace(wait_snapshot=lambda: None)
    session._sync_terminal_status()
    assert redraws
    ctx.terminal_manager = SimpleNamespace(
        wait_snapshot=lambda: {
            "terminal_id": "term-1",
            "label": "test",
            "elapsed_s": "bad",
            "running_count": 2,
        }
    )
    session._sync_terminal_status()
    assert ctx.app_state.conversation.terminal_waiting() is True
    assert ctx.app_state.conversation.terminal_wait_elapsed_s() == 0.0

    class Service:
        async def publish(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

    ctx.session_service = Service()

    async def publish_event() -> None:
        session._publish_session_event("turn_started", {"text": "hello"}, turn_id="turn-1")
        await asyncio.sleep(0)

    asyncio.run(publish_event())
    assert ctx.session_service.kwargs["kind"] == "turn_started"


def test_session_reload_failure_paths_and_workflow_resume_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    monkeypatch.setattr(
        "agenthicc.plugins.discovery.discover_project_tools",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tools unavailable")),
    )
    assert session._reload_tools()[0] is False
    monkeypatch.setattr(
        "agenthicc.workflows.registry.build_workflow_registry",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("workflows unavailable")),
    )
    assert session._reload_workflows()[0] is False
    monkeypatch.setattr(
        "agenthicc.commands.plugin_loader.discover_command_plugins",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("commands unavailable")),
    )
    assert session._reload_commands()[0] is False

    assert session._handle_workflow_resume("missing") is True
    ctx.app_state.active_mode.set(RuntimeMode("Plan", default_workflow="demo"))
    ctx.workflow_registry.register(type("Demo", (), {"name": "demo"}))  # type: ignore[arg-type]
    assert session._handle_workflow_command("demo") is True
    assert session._handle_workflow_resume(None) is True


@pytest.mark.asyncio
async def test_session_cancellation_and_workflow_pause_branches() -> None:
    session, ctx, _workspace, input_session = _make_session()
    assert session._cancel_active_task() is False
    ctx.terminal_manager = SimpleNamespace(request_stop_current=lambda: True)

    async def idle() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(idle())
    session._agent_task = task
    assert session._cancel_active_task() is True
    with pytest.raises(asyncio.CancelledError):
        await task

    class Handle:
        workflow_name = "demo"
        lifecycle = "running"

        def request_pause(self) -> bool:
            self.lifecycle = "pausing"
            return True

        def is_pause_requested(self) -> bool:
            return True

    handle = Handle()
    session._workflow_handle = handle  # type: ignore[assignment]
    session.handle_interrupt(SimpleNamespace(disposition="pause"))
    assert "Pausing" in (ctx.app_state.conversation.notification() or "")
    from agenthicc.tui.input.unified_session import InputMode

    assert input_session.modes[-1] is InputMode.IDLE


def test_session_incomplete_workflow_notification_only_targets_active_runs() -> None:
    session, ctx, _workspace, _input = _make_session()
    from agenthicc.kernel.state import NodeStatus

    ctx.processor.get_state = lambda: SimpleNamespace(
        workflows={"complete": SimpleNamespace(name="done", status=NodeStatus.complete)}
    )
    session._notify_incomplete_workflow()
    assert ctx.app_state.conversation.notification() is None
    ctx.processor.get_state = lambda: SimpleNamespace(
        workflows={"active": SimpleNamespace(name="demo", status=NodeStatus.pending)}
    )
    session._notify_incomplete_workflow()
    assert "in-progress" in (ctx.app_state.conversation.notification() or "")
    assert session._has_incomplete_workflow() is True
