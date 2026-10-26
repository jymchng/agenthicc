"""Input while the agent runtime loads after the first frame (AGX-02)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.agents.registry import AgentsRegistry
from agenthicc.commands import build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.runners.startup import ReadinessState, StartupCoordinator
from agenthicc.runners.tui_session import TUISession
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime import CommandBus, ModeManager, ModeRegistry, RuntimeMode
from agenthicc.tui.runtime.commands import SendMessageCommand
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.workflows.registry import WorkflowRegistry
from tests.session_fakes import session_context

pytestmark = pytest.mark.unit


class _Workspace:
    def __init__(self, app_state: AppState) -> None:
        self.overlays = OverlayHost(app_state)

    def frame_stats(self) -> None:
        return None

    def flush_scroll(self) -> None:
        pass


class _Input:
    def set_mode(self, mode: object) -> None:
        pass


class _Conversation:
    """A session conversation whose fold finishes when the test says so."""

    def __init__(self, *, fail: bool = False) -> None:
        self.release = asyncio.Event()
        self.fail = fail
        self.memory = SimpleNamespace(_messages=[])

    async def load(self) -> object:
        await self.release.wait()
        if self.fail:
            raise OSError("journal unreadable")
        return self.memory


def _session(conversation: _Conversation) -> tuple[TUISession, SimpleNamespace, list[str]]:
    app = AppState.create()
    modes = ModeRegistry()
    modes.register(RuntimeMode("Safe", default_workflow=None))
    approval = SimpleNamespace(reset_turn_memory=lambda: None)
    ctx = session_context(
        app_state=app,
        processor=SimpleNamespace(get_state=lambda: SimpleNamespace(workflows={})),
        agent_runner=object(),
        approval_svc=approval,
        cfg=AgenthiccConfig(),
        skills={},
        project_plugins=SimpleNamespace(all_tools=[]),
        mcp_registry=None,
        mention_cache=SimpleNamespace(),
        cmd_registry=build_builtin_registry(),
        workflow_registry=WorkflowRegistry(),
        agents_registry=AgentsRegistry(),
        mode_manager=ModeManager(modes, app),
        command_bus=CommandBus(),
        session_id="runtime-gate",
        session_conversation=conversation,
        session_memory=conversation.memory,
        model_label="test/model",
        console=Console(record=True),
        startup=StartupCoordinator(),
        resumed=False,
        command_plugin_names=set(),
    )
    session = TUISession(ctx, _Workspace(app), _Input())
    started: list[str] = []

    async def agent_task_body(text: str, **_kwargs: object) -> None:
        started.append(text)

    session.agent_task_body = agent_task_body  # type: ignore[method-assign]
    return session, ctx, started


async def test_messages_wait_for_the_runtime_then_run_in_order() -> None:
    conversation = _Conversation()
    session, ctx, started = _session(conversation)
    session._start_agent_runtime()

    await session.handle_send(SendMessageCommand(text="first"))
    await session.handle_send(SendMessageCommand(text="second"))

    assert started == []
    assert session._msg_queue == ["first", "second"]
    notice = ctx.app_state.conversation.notification.get()
    assert notice is not None and "loading agent runtime" in notice
    assert session._loop_is_idle() is False

    conversation.release.set()
    assert session._runtime_task is not None
    await session._runtime_task
    while session._agent_task is not None and not session._agent_task.done():
        await asyncio.sleep(0)

    assert started[0] == "first"
    assert session._msg_queue == ["second"]
    assert ctx.startup.report("agent_runtime").state is ReadinessState.READY
    assert session._runtime_loading() is False


async def test_read_only_commands_answer_while_the_runtime_loads() -> None:
    conversation = _Conversation()
    session, ctx, started = _session(conversation)
    session._start_agent_runtime()

    await session.handle_send(SendMessageCommand(text="/startup"))

    assert session._msg_queue == []
    assert "agent_runtime: loading" in ctx.console.export_text()
    conversation.release.set()
    assert session._runtime_task is not None
    await session._runtime_task


async def test_recovery_runs_once_the_conversation_is_loaded() -> None:
    conversation = _Conversation()
    session, _ctx, _started = _session(conversation)
    calls: list[str] = []
    for name in (
        "_restore_paused_workflow",
        "_publish_session_readiness",
        "_notify_tool_history_recovery",
        "_notify_incomplete_workflow",
        "_maybe_resume_interrupted_turn",
    ):
        setattr(session, name, lambda name=name: calls.append(name))

    session._start_agent_runtime()
    await asyncio.sleep(0.01)
    assert calls == []
    conversation.release.set()
    assert session._runtime_task is not None
    await session._runtime_task

    assert calls == [
        "_restore_paused_workflow",
        "_publish_session_readiness",
        "_notify_tool_history_recovery",
        "_notify_incomplete_workflow",
        "_maybe_resume_interrupted_turn",
    ]


async def test_a_failed_load_reports_and_still_releases_queued_input() -> None:
    conversation = _Conversation(fail=True)
    session, ctx, started = _session(conversation)
    notices: list[str] = []
    ctx.app_state.conversation.notify_transient = lambda text, **_kwargs: notices.append(text)
    session._start_agent_runtime()
    await session.handle_send(SendMessageCommand(text="hello"))

    conversation.release.set()
    assert session._runtime_task is not None
    await session._runtime_task
    while session._agent_task is not None and not session._agent_task.done():
        await asyncio.sleep(0)

    assert ctx.startup.report("provider_journal_restore").state is ReadinessState.FAILED
    assert ctx.startup.report("agent_runtime").state is ReadinessState.DEGRADED
    assert any("could not be loaded" in notice for notice in notices)
    # The queued turn runs and reports the failed dependency itself.
    assert started == ["hello"]


async def test_turns_fail_clearly_when_the_conversation_did_not_load() -> None:
    from agenthicc.runners.startup import StartupDependencyError

    conversation = _Conversation(fail=True)
    session, ctx, _started = _session(conversation)
    session._start_agent_runtime()
    conversation.release.set()
    assert session._runtime_task is not None
    await session._runtime_task

    with pytest.raises(StartupDependencyError, match="provider_journal_restore"):
        await session._await_agent_runtime()


@pytest.mark.parametrize(
    ("tier", "expect_ads"), [("free", True), ("pro", False), ("signed_out", False)]
)
async def test_sponsored_messages_start_only_for_free_accounts(
    monkeypatch: pytest.MonkeyPatch, tier: str, expect_ads: bool
) -> None:
    from agenthicc.ads import AdRotator
    from agenthicc.auth import AuthClient

    session, _ctx, _started = _session(_Conversation())
    rotations: list[str] = []

    async def run(self: AdRotator) -> None:
        rotations.append("started")

    monkeypatch.setattr(AuthClient, "account_tier", lambda self: tier)
    monkeypatch.setattr(AdRotator, "run", run)

    await session._check_account()
    if session._ad_task is not None:
        await session._ad_task

    assert (session._ad_task is not None) is expect_ads
    assert rotations == (["started"] if expect_ads else [])


async def test_an_unavailable_account_tier_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.auth import AuthClient

    session, _ctx, _started = _session(_Conversation())

    def broken(self: AuthClient) -> str:
        raise RuntimeError("secret service unavailable")

    monkeypatch.setattr(AuthClient, "account_tier", broken)
    await session._check_account()
    assert session._ad_task is None
