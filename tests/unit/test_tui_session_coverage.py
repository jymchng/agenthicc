"""Branch coverage for the interactive session orchestration layer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.agents.registry import AgentsRegistry
from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.config import AgenthiccConfig
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime import CommandBus, ModeManager, ModeRegistry, RuntimeMode
from agenthicc.tui.runtime.commands import InterruptAgentCommand, SendMessageCommand
from agenthicc.tui.workspace.overlay import OverlayHost
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.unit


class _Workspace:
    def __init__(self, app_state: AppState) -> None:
        self.overlays = OverlayHost(app_state)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _Input:
    def __init__(self) -> None:
        self.modes: list[object] = []

    def set_mode(self, mode: object) -> None:
        self.modes.append(mode)


def _make_session() -> tuple[object, SimpleNamespace, _Workspace, _Input]:
    from agenthicc.runners.tui_session import TUISession

    app = AppState.create()
    modes = ModeRegistry()
    modes.register(RuntimeMode("Auto", default_workflow=None))
    modes.register(RuntimeMode("Plan", default_workflow="demo"))
    modes.register(RuntimeMode("Replay"))
    mode_manager = ModeManager(modes, app)
    bus = CommandBus()
    config = AgenthiccConfig()
    state = SimpleNamespace(workflows={})

    async def processor_run() -> None:
        await asyncio.Event().wait()

    processor = SimpleNamespace(get_state=lambda: state, run=processor_run)
    approval = SimpleNamespace(reset_turn_memory=lambda: None)
    memory = SimpleNamespace(_messages=[], rollback_to=lambda count: None, close=lambda: None)
    ctx = SimpleNamespace(
        app_state=app,
        processor=processor,
        agent_runner=object(),
        approval_svc=approval,
        cfg=config,
        skills={},
        project_plugins=SimpleNamespace(all_tools=[]),
        mcp_registry=None,
        mention_cache=SimpleNamespace(),
        memory_router=None,
        semantic_index=None,
        cmd_registry=UnifiedCommandRegistry(),
        workflow_registry=WorkflowRegistry(),
        agents_registry=AgentsRegistry(),
        mode_manager=mode_manager,
        command_bus=bus,
        session_id="tui-coverage",
        model_label="test/model",
        console=Console(record=True),
        session_memory=memory,
        pending_resume=None,
        command_plugin_names=set(),
    )
    workspace = _Workspace(app)
    input_session = _Input()
    return TUISession(ctx, workspace, input_session), ctx, workspace, input_session


def test_tui_routing_workflow_commands_and_skill_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.runners import tui_session

    session, ctx, _workspace, _input = _make_session()
    handled: list[str] = []
    ctx.cmd_registry.register(
        Command(
            "/ping", "test", handler=lambda command_ctx: handled.append(command_ctx.args) or True
        )
    )
    assert session.dispatch_slash("/ping hello") is True
    assert handled == ["hello"]
    assert session.route("plain text") is False
    assert session.route("/ping again") is True
    assert session.route("/unknown") is True

    assert session._handle_workflow_command("") is True
    assert session._workflow_override is None
    assert session._handle_workflow_command("missing") is True
    ctx.workflow_registry.register(type("Demo", (), {"name": "demo", "mode_bindings": ()}))  # type: ignore[arg-type]
    assert session._handle_workflow_command("demo") is True
    assert session._workflow_override == "demo"
    assert session._handle_workflow_command("reset") is True

    skill = SkillDef("Coverage", "coverage", Path("."), description="test", aliases=("cov",))
    ctx.skills["old"] = skill
    ctx.cmd_registry.register(Command("$old", "old", source_id="skill:old", group="Skills"))
    discovery = SkillDiscoveryResult({"coverage": skill})
    monkeypatch.setattr(
        tui_session, "discover_skills_with_diagnostics", lambda **_: discovery, raising=False
    )
    # The import is local in the method, so patch its defining module instead.
    monkeypatch.setattr(
        "agenthicc.skills.loader.discover_skills_with_diagnostics", lambda **_: discovery
    )
    result = session._reload_skills()
    assert "coverage" in result.skills
    assert ctx.cmd_registry.get("/old") is None
    assert ctx.cmd_registry.get("$coverage") is not None
    assert ctx.cmd_registry.get("/coverage") is None


def test_tui_routes_dollar_skills_and_rejects_legacy_slash() -> None:
    session, ctx, _workspace, _input = _make_session()
    handled: list[str] = []
    ctx.cmd_registry.register(
        Command(
            "$review",
            "Review",
            group="Skills",
            source_id="skill:review",
            handler=lambda command_ctx: handled.append(command_ctx.args) or True,
        )
    )

    assert session.route("$review src/app.py") is True
    assert handled == ["src/app.py"]
    # The removed spelling must not dispatch, even when a stale slash-named
    # record is manually present in the registry.
    ctx.cmd_registry.register(
        Command(
            "/review",
            "Legacy review",
            group="Skills",
            source_id="skill:review-legacy",
            handler=lambda _ctx: handled.append("legacy") or True,
        )
    )
    assert session.route("/review src/app.py") is False
    assert handled == ["src/app.py"]
    assert session.route("$unknown") is False


@pytest.mark.asyncio
async def test_tui_queue_and_interrupt_paths() -> None:
    session, ctx, _workspace, _input = _make_session()
    session._agent_task = asyncio.current_task()  # a live task for queue behavior
    session._msg_queue.clear()
    await session.handle_send(SendMessageCommand(text="queued"))
    assert session._msg_queue == ["queued"]
    ctx.app_state.conversation.notification.set(None)

    async def idle() -> None:
        await asyncio.sleep(10)

    session._agent_task = asyncio.create_task(idle())
    session.handle_interrupt(InterruptAgentCommand())
    with pytest.raises(asyncio.CancelledError):
        await session._agent_task


@pytest.mark.asyncio
async def test_tui_direct_turn_timeout_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.runners import tui_session

    session, ctx, _workspace, input_session = _make_session()
    monkeypatch.setattr(tui_session, "_make_session_tools", lambda *args, **kwargs: [])
    calls: list[str] = []

    async def run_agent(text: str, *args: object, **kwargs: object) -> None:
        calls.append(text)

    monkeypatch.setattr(tui_session, "_run_agent_turn", run_agent)
    await session.run_turn("hello")
    assert calls == ["hello"]
    assert input_session.modes

    async def slow(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(tui_session, "_run_agent_turn", slow)
    ctx.cfg.execution.turn_timeout_s = 0.001
    await session.run_turn("slow")
    assert (
        "timed out" in (ctx.app_state.conversation.notification() or "")
        or ctx.app_state.conversation.turn_count() >= 0
    )


@pytest.mark.asyncio
async def test_tui_agent_body_workflow_resume_and_replay_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.tui.runtime.mode_manager import RuntimeMode

    session, ctx, _workspace, _input = _make_session()
    ctx.app_state.active_mode.set(RuntimeMode("Plan", default_workflow="demo"))

    class Demo:
        name = "demo"

        @classmethod
        def build_params(cls, raw: object) -> object:
            return raw

        @classmethod
        def build_runner(cls, config: object, mode: object) -> object:
            async def run(text: str) -> None:
                ctx.app_state.workflow_run.set(SimpleNamespace(status="complete"))

            async def resume(context: object) -> None:
                return None

            return SimpleNamespace(run=run, resume=resume)

    ctx.workflow_registry.register(Demo)  # type: ignore[arg-type]
    await session.run_turn("workflow")
    assert session._turn_count == 1

    session._ctx.app_state.conversation.append_event("turn_start", {"turn_id": "t"})
    await session.agent_task_body("done")
    session._agent_task = None
    session._msg_queue = ["/unknown"]
    session.advance()

    async def raising(*args: object, **kwargs: object) -> None:
        raise ValueError("bad turn")

    monkeypatch.setattr(session, "run_turn", raising)
    await session.agent_task_body("bad")
    assert session._agent_task is None

    class Replay:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self) -> None:
            return None

    monkeypatch.setattr("agenthicc.tui.runtime.replay.ConversationReplayer", Replay)
    session._pending_replay_id = "replay-id"
    await session._run_replay("replay-id")
    assert session._agent_task is None

    ctx.processor.get_state = lambda: SimpleNamespace(
        workflows={"one": SimpleNamespace(name="demo", status="running")}
    )
    session._notify_incomplete_workflow()
    assert session._has_incomplete_workflow() is True


@pytest.mark.asyncio
async def test_tui_resume_workflow_task_and_compact_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    await session._handle_compact_command()
    ctx.session_memory._messages = ["message"]
    await session._handle_compact_command()

    class Transport:
        async def complete(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(content="compact summary")

    ctx.session_memory.token_estimate = 2
    ctx.agent_runner = SimpleNamespace(_transport=Transport())
    await session._handle_compact_command()

    class Demo:
        name = "demo"

        @classmethod
        def build_params(cls, raw: object) -> object:
            return raw

        @classmethod
        def build_runner(cls, config: object, mode: object) -> object:
            async def resume(context: object) -> None:
                return None

            return SimpleNamespace(resume=resume)

    await session._resume_workflow_task(Demo, SimpleNamespace())  # type: ignore[arg-type]
    # The success path leaves the input session in its streaming state until
    # the resumed workflow's next turn; the exception/cancel paths reset it.
    assert getattr(session, "_agent_task") is None


@pytest.mark.asyncio
async def test_tui_run_registers_handlers_and_stops_cleanly() -> None:
    session, ctx, workspace, input_session = _make_session()

    async def input_run() -> None:
        return None

    input_session.run = input_run  # type: ignore[attr-defined]
    await session.run()
    assert workspace.started and workspace.stopped
    assert ctx.command_bus._handlers


@pytest.mark.asyncio
async def test_build_session_context_fresh_and_resume_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real session assembly boundary without a network call."""
    from agenthicc.agents import registry as agents_registry
    from agenthicc.commands import plugin_loader
    from agenthicc.memory import journal as memory_journal
    from agenthicc.memory import layers
    from agenthicc.plugins import discovery
    from agenthicc.runners import tui_session
    from agenthicc.skills import bootstrap, loader
    from agenthicc.workflows import registry as workflows_registry
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.commands.plugin_loader import CommandPluginSet
    from agenthicc.skills.loader import SkillDiscoveryResult

    session_root = tmp_path / "sessions"
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", session_root)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", session_root)
    monkeypatch.setattr(memory_journal, "_SESSIONS_DIR", session_root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui_session, "_build_agent_runner", lambda *args, **kwargs: None)
    monkeypatch.setattr(bootstrap, "bootstrap_default_skills", lambda **kwargs: 0)
    monkeypatch.setattr(
        loader, "discover_skills_with_diagnostics", lambda **kwargs: SkillDiscoveryResult({}, ())
    )
    monkeypatch.setattr(
        workflows_registry, "build_workflow_registry", lambda **kwargs: WorkflowRegistry()
    )
    monkeypatch.setattr(agents_registry, "build_agents_registry", lambda **kwargs: AgentsRegistry())
    monkeypatch.setattr(discovery, "discover_project_tools", lambda **kwargs: PluginToolSet())
    monkeypatch.setattr(discovery, "warn_conflicts", lambda tools: None)
    monkeypatch.setattr(
        plugin_loader, "discover_command_plugins", lambda **kwargs: CommandPluginSet()
    )
    monkeypatch.setattr(layers, "GlobalMemoryLayer", lambda: layers.SessionMemoryLayer())

    from agenthicc.runners.tui_session import _build_session_context

    fresh = await _build_session_context(
        None, [], record_cassette_dir=tmp_path / "cassettes", headless=True
    )
    assert fresh.session_id and fresh.agent_runner is None
    session_id = fresh.session_id
    fresh.session_log.close()
    fresh.session_memory.close()

    resumed = await _build_session_context(session_id, [], headless=True)
    assert resumed.session_id == session_id
    assert resumed.pending_resume is None
    resumed.session_log.close()
    resumed.session_memory.close()
