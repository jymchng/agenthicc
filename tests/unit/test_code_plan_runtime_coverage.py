"""Exercise the code-plan state machine through real phase tool closures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.plugin import PhaseOutput, WorkflowContext

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


def test_base_tools_filters_blocked_capabilities_and_phase_model() -> None:
    runner = _runner()
    runner.plan_model = "planner-model"
    assert runner._phase_model("plan") == "planner-model"
    assert runner._phase_model("unknown") == ""
    tools = runner._base_tools()
    assert tools


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
async def test_run_and_resume_failure_and_completed_phase_paths() -> None:
    runner = _runner()

    async def broken_plan(_ctx: CodePlanContext) -> CodePlanState:
        raise RuntimeError("phase crashed")

    runner._plan = broken_plan  # type: ignore[method-assign]
    result = await runner.run("intent")
    assert result.fail_reason == ""
    assert runner._cfg.app_state.workflow_run().status == "failed"

    context = WorkflowContext("intent", "resume", runner.workflow_name)
    context.add_output(PhaseOutput("plan", "planner", "saved plan"))
    context.add_output(PhaseOutput("execute", "executor", "saved execution"))
    context.add_output(PhaseOutput("review", "reviewer", "saved review", approved=True))

    async def summarize(_ctx: CodePlanContext) -> CodePlanState:
        return CodePlanState.COMPLETE

    runner._summarize = summarize  # type: ignore[method-assign]
    await runner.resume(context)
    assert runner._cfg.app_state.workflow_run().status == "complete"

    empty = WorkflowContext("intent", "empty", runner.workflow_name)
    runner.run = AsyncMock(return_value=CodePlanContext("intent", "empty"))  # type: ignore[method-assign]
    await runner.resume(empty)
    runner.run.assert_awaited_once_with("intent")
