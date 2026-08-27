"""Exercise the code-plan state machine through real phase tool closures."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager
from agenthicc.workflows.code_plan.runner import CACHE_CONTRACT, CodePlanRunner
from agenthicc.workflows.code_plan.phase_tools import make_executor_tools
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.plugin import WorkflowContext

pytestmark = pytest.mark.unit


def _runner() -> CodePlanRunner:
    app = AppState.create()

    async def emit(_event: object) -> None:
        return None

    cfg = AgenthiccConfig()
    cfg.execution.model = "global"  # type: ignore[misc]
    cfg.execution.effective_usable_budget = lambda: 10_000  # type: ignore[method-assign]
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=SimpleNamespace(emit=emit),  # type: ignore[arg-type]
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="transport"))
        ),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
    )
    runner = CodePlanRunner(config)
    runner._cfg.app_state.update_workflow_phase = MagicMock()  # type: ignore[method-assign]
    runner._cfg.conv_store.append_event = MagicMock()  # type: ignore[method-assign]
    return runner


async def _complete_tools(tools: list[object], *, review_action: str = "approve") -> None:
    for tool in tools:
        name = getattr(tool, "__name__", "")
        if name == "request_plan_approval":
            await tool("a safe plan")  # type: ignore[operator]
        elif name == "finalize_plan":
            await tool("a safe plan")  # type: ignore[operator]
        elif name == "mark_execute_complete":
            await tool("implemented")  # type: ignore[operator]
        elif name == "approve_review" and review_action == "approve":
            await tool("looks good")  # type: ignore[operator]
        elif name == "reject_review" and review_action == "reject":
            await tool("needs work")  # type: ignore[operator]


@pytest.mark.asyncio
async def test_full_run_reaches_complete_and_records_phase_state() -> None:
    runner = _runner()

    async def run_turn(_text: str, **kwargs: object) -> None:
        tools = kwargs.get("tools")
        if isinstance(tools, list):
            await _complete_tools(tools)

    runner._run_turn = run_turn  # type: ignore[method-assign]
    ctx = await runner.run("implement the feature")
    assert isinstance(ctx, CodePlanContext)
    assert ctx.plan == "a safe plan"
    assert ctx.execute_summary == "implemented"
    assert ctx.review_summary == "looks good"
    assert runner._cfg.app_state.workflow_run().status == "complete"


@pytest.mark.asyncio
async def test_individual_phase_success_rejection_exit_and_summary_error() -> None:
    runner = _runner()
    ctx = CodePlanContext("intent", "run", shared_memory=MagicMock())

    async def plan_turn(_text: str, **kwargs: object) -> None:
        await _complete_tools(kwargs["tools"])  # type: ignore[arg-type]

    runner._run_turn = plan_turn  # type: ignore[method-assign]
    assert await runner._plan(ctx) is CodePlanState.EXECUTE

    async def exit_turn(_text: str, **kwargs: object) -> None:
        for tool in kwargs["tools"]:  # type: ignore[union-attr]
            if getattr(tool, "__name__", "") == "exit_code_plan":
                await tool()  # type: ignore[operator]

    runner._run_turn = exit_turn  # type: ignore[method-assign]
    assert (
        await runner._plan(CodePlanContext("intent", "run", shared_memory=MagicMock()))
        is CodePlanState.EXITED
    )

    async def execute_turn(_text: str, **kwargs: object) -> None:
        await _complete_tools(kwargs["tools"])  # type: ignore[arg-type]

    runner._run_turn = execute_turn  # type: ignore[method-assign]
    ctx.plan = "plan"
    assert await runner._execute(ctx) is CodePlanState.REVIEW

    async def reject_turn(_text: str, **kwargs: object) -> None:
        await _complete_tools(kwargs["tools"], review_action="reject")  # type: ignore[arg-type]

    runner._run_turn = reject_turn  # type: ignore[method-assign]
    assert await runner._review(ctx) is CodePlanState.EXECUTE
    assert ctx.rejection_reason == "needs work"

    async def summarize_turn(_text: str, **_kwargs: object) -> None:
        return None

    runner._run_turn = summarize_turn  # type: ignore[method-assign]
    assert await runner._summarize(ctx) is CodePlanState.COMPLETE

    async def broken_turn(_text: str, **_kwargs: object) -> None:
        raise RuntimeError("summary unavailable")

    runner._run_turn = broken_turn  # type: ignore[method-assign]
    assert await runner._summarize(ctx) is CodePlanState.COMPLETE


@pytest.mark.asyncio
async def test_execute_uses_mode_selected_during_plan_approval() -> None:
    runner = _runner()
    seen_modes: list[object] = []

    async def execute_turn(_text: str, **kwargs: object) -> None:
        seen_modes.append(kwargs["mode"])
        await _complete_tools(kwargs["tools"])  # type: ignore[arg-type]

    runner._run_turn = execute_turn  # type: ignore[method-assign]
    ctx = CodePlanContext("intent", "run", plan="plan", execute_mode="Yolo")

    assert await runner._execute(ctx) is CodePlanState.REVIEW
    assert seen_modes == ["Yolo"]


def test_base_tools_filters_blocked_capabilities_and_phase_model() -> None:
    runner = _runner()
    runner.plan_model = "planner-model"
    assert runner._phase_model("plan") == "planner-model"
    assert runner._phase_model("unknown") == ""
    tools = runner._base_tools()
    assert tools


@pytest.mark.asyncio
async def test_code_plan_plan_phase_injects_the_existing_question_tool() -> None:
    runner = _runner()
    captured: list[str] = []

    async def run_turn(_text: str, **kwargs: object) -> None:
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        captured.extend(getattr(tool, "__name__", "") for tool in tools)
        await _complete_tools(tools)

    runner._run_turn = run_turn  # type: ignore[method-assign]
    assert (
        await runner._plan(CodePlanContext("intent", "run", shared_memory=MagicMock()))
        is CodePlanState.EXECUTE
    )
    assert "ask_user" in captured


@pytest.mark.asyncio
async def test_run_phase_filters_tools_using_the_effective_phase_mode() -> None:
    runner = _runner()
    manager = ModeManager(app_state=runner._cfg.app_state)
    runner._mode_manager = manager

    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_write

    @tool_write
    @tool()
    async def write_fixture(path: str) -> dict[str, str]:
        return {"path": path}

    runner._cfg.plugin_tools.append(write_fixture)  # type: ignore[union-attr]
    captured: dict[str, object] = {}

    async def fake_turn(_text: str, **kwargs: object) -> None:
        captured.update(kwargs)

    runner._run_turn = fake_turn  # type: ignore[method-assign]
    await runner.run_phase(
        intent="intent",
        text="write",
        system_prompt="phase",
        mode="Yolo",
        max_turns=1,
        shared_memory=MagicMock(),
    )

    tools = captured["tools"]
    assert isinstance(tools, list)
    assert any(getattr(item, "__name__", "") == "write_fixture" for item in tools)
    assert manager.active_name == "Safe"


@pytest.mark.asyncio
async def test_resume_type_validation_and_extension_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    with pytest.raises(TypeError):
        await runner.resume(object())

    captured: list[object] = []

    async def fake_turn(_text: str, **kwargs: object) -> None:
        captured.append(kwargs["session_memory"])

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", fake_turn)
    await runner.run_phase(
        intent="intent",
        text="extension",
        system_prompt="system",
        mode=None,
        max_turns=2,
        shared_memory=MagicMock(),
    )
    assert captured


@pytest.mark.asyncio
async def test_run_turn_appends_the_phase_transition_tool_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    captured: dict[str, object] = {}

    async def fake_turn(_text: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", fake_turn)
    await runner._run_turn(
        "implement the approved plan",
        tools=make_executor_tools(asyncio.Event(), {}),
        mode=None,
        system_prompt="Execute the approved work.",
        max_turns=2,
        ctx=CodePlanContext("intent", "run", shared_memory=MagicMock()),
        phase_name="execute",
    )

    suffix = captured["system_prompt_suffix"]
    assert isinstance(suffix, str)
    assert "[PHASE TRANSITION TOOLS]" in suffix
    assert "`mark_execute_complete`" in suffix
    assert "prose" in suffix
    assert "[REQUIREMENTS CLARIFICATION]" in suffix
    assert "multiple focused questions" in suffix
    prompt_contract = captured["prompt_contract"]
    assert CACHE_CONTRACT in prompt_contract.stable_system_prefix
    assert "Execute the approved work." not in prompt_contract.stable_system_prefix


@pytest.mark.asyncio
async def test_yolo_phase_override_restores_safe_after_cancellation(monkeypatch) -> None:
    runner = _runner()
    manager = ModeManager(app_state=runner._cfg.app_state)
    runner._mode_manager = manager

    async def cancelled_turn(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", cancelled_turn)
    with pytest.raises(asyncio.CancelledError):
        await runner._run_turn(
            "write",
            tools=[],
            mode="Yolo",
            system_prompt="system",
            max_turns=1,
            ctx=CodePlanContext("intent", "run", shared_memory=MagicMock()),
        )

    assert manager.active_name == "Safe"
    assert runner._cfg.app_state.active_mode().name == "Safe"


@pytest.mark.asyncio
async def test_run_and_resume_failure_and_completed_phase_paths() -> None:
    runner = _runner()

    async def broken_plan(_ctx: CodePlanContext) -> CodePlanState:
        raise RuntimeError("phase crashed")

    runner._plan = broken_plan  # type: ignore[method-assign]
    result = await runner.run("intent")
    assert result.fail_reason == ""
    assert runner._cfg.app_state.workflow_run().status == "failed"

    async def summarize(_ctx: CodePlanContext) -> CodePlanState:
        return CodePlanState.COMPLETE

    runner._summarize = summarize  # type: ignore[method-assign]
    await runner.resume(
        CodePlanContext(
            "intent",
            "resume",
            state=CodePlanState.SUMMARIZE,
            shared_memory=MagicMock(),
        )
    )
    assert runner._cfg.app_state.workflow_run().status == "complete"

    empty = WorkflowContext("intent", "empty", runner.workflow_name)
    runner.run = AsyncMock(return_value=CodePlanContext("intent", "empty"))  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="exact recoverable state"):
        await runner.resume(empty)
    runner.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_code_plan_phase_retry_exhaustion_and_permanent_errors() -> None:
    runner = _runner()

    async def no_turn(*_args: object, **_kwargs: object) -> None:
        return None

    runner._run_turn = no_turn  # type: ignore[method-assign]
    assert await runner._plan(CodePlanContext("intent", "run")) is CodePlanState.FAILED
    assert (
        await runner._execute(CodePlanContext("intent", "run", plan="plan")) is CodePlanState.FAILED
    )
    assert await runner._review(CodePlanContext("intent", "run")) is CodePlanState.FAILED

    async def broken_turn(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provider is permanently unavailable")

    runner._run_turn = broken_turn  # type: ignore[method-assign]
    for method, context in (
        (runner._plan, CodePlanContext("intent", "run")),
        (runner._execute, CodePlanContext("intent", "run", plan="plan")),
        (runner._review, CodePlanContext("intent", "run")),
    ):
        assert await method(context) is CodePlanState.FAILED
        assert "RuntimeError" in context.fail_reason


@pytest.mark.asyncio
async def test_code_plan_execute_command_gate_retries_failed_command() -> None:
    runner = _runner()
    context = CodePlanContext("intent", "run", plan="plan")

    async def failed_command(_text: str, **kwargs: object) -> None:
        context.command_outcomes.append(
            {
                "terminal_id": "terminal-1",
                "state": "failed",
                "ok": False,
                "returncode": 1,
                "stderr": "compiler failed",
            }
        )
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        complete = next(
            tool for tool in tools if getattr(tool, "__name__", "") == "mark_execute_complete"
        )
        await complete("implemented")  # type: ignore[operator]

    runner._run_turn = failed_command  # type: ignore[method-assign]
    assert await runner._execute(context) is CodePlanState.FAILED
    assert "exhausted" in context.fail_reason


def test_command_gate_deduplicates_terminal_retries_and_is_fail_closed() -> None:
    assert CodePlanRunner._command_gate_error([]) is None
    assert (
        CodePlanRunner._command_gate_error(
            [
                {"terminal_id": "one", "state": "failed", "ok": False, "stderr": "old"},
                {"terminal_id": "one", "state": "exited", "ok": True, "returncode": 0},
            ]
        )
        is None
    )
    error = CodePlanRunner._command_gate_error([{"state": "running", "ok": True}])
    assert error is not None and "running" in error


@pytest.mark.asyncio
async def test_code_plan_resume_dispatches_every_state_and_preserves_terminal_status() -> None:
    runner = _runner()
    calls: list[str] = []

    async def phase(name: str, result: CodePlanState, _ctx: CodePlanContext) -> CodePlanState:
        calls.append(name)
        return result

    runner._plan = lambda ctx: phase("plan", CodePlanState.EXECUTE, ctx)  # type: ignore[method-assign]
    runner._execute = lambda ctx: phase("execute", CodePlanState.REVIEW, ctx)  # type: ignore[method-assign]
    runner._review = lambda ctx: phase("review", CodePlanState.SUMMARIZE, ctx)  # type: ignore[method-assign]
    runner._summarize = lambda ctx: phase("summarize", CodePlanState.COMPLETE, ctx)  # type: ignore[method-assign]
    await runner.resume(
        CodePlanContext("intent", "resume", state=CodePlanState.PLAN, shared_memory=MagicMock())
    )
    assert calls == ["plan", "execute", "review", "summarize"]
    assert runner._cfg.app_state.workflow_run().status == "complete"

    await runner.resume(
        CodePlanContext("intent", "failed", state=CodePlanState.FAILED, shared_memory=MagicMock())
    )
    assert runner._cfg.app_state.workflow_run().status == "failed"


def test_code_plan_model_and_phase_helpers_cover_extension_paths() -> None:
    runner = _runner()
    object.__setattr__(
        runner._cfg,
        "params",
        SimpleNamespace(
            model_for_phase=lambda name, fallback: "override" if name == "plan" else fallback
        ),
    )
    assert runner._phase_model("plan") == "override"
    runner._set_phase("extension", 10, CodePlanContext("intent", "run"))
    runner._cfg.app_state.update_workflow_phase.assert_called_with(
        workflow_name="code_plan",
        phase_name="extension",
        phase_index=10,
        total_phases=4,
        run_id="run",
        intent="intent",
        model_id="transport",
    )


@pytest.mark.asyncio
async def test_resume_restores_execute_mode_from_plan_metadata() -> None:
    runner = _runner()
    context = CodePlanContext(
        "intent",
        "resume-mode",
        state=CodePlanState.EXECUTE,
        execute_mode="Yolo",
        shared_memory=MagicMock(),
    )

    seen: list[str] = []

    async def execute(ctx: CodePlanContext) -> CodePlanState:
        seen.append(ctx.execute_mode)
        return CodePlanState.REVIEW

    async def review(_ctx: CodePlanContext) -> CodePlanState:
        return CodePlanState.COMPLETE

    runner._execute = execute  # type: ignore[method-assign]
    runner._review = review  # type: ignore[method-assign]
    await runner.resume(context)

    assert seen == ["Yolo"]
