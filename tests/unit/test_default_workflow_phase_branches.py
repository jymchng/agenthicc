"""Phase continuation and structured-output branches for WorkflowRunner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

pytestmark = pytest.mark.unit


def _config():
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime import ModeManager, ModeRegistry, RuntimeMode
    from agenthicc.workflows.config import WorkflowConfig

    app = AppState.create()

    async def emit(_event: object) -> None:
        return None

    modes = ModeRegistry()
    modes.register(RuntimeMode("Auto", badge="A"))
    modes.register(RuntimeMode("Plan", badge="P"))
    mode = ModeManager(modes, app)
    cfg = AgenthiccConfig()

    class Approval:
        async def request_approval(self, _request: object) -> object:
            return SimpleNamespace(allowed=True, message="")

    return (
        WorkflowConfig(
            conv_store=app.conversation,
            app_state=app,
            processor=SimpleNamespace(emit=emit),  # type: ignore[arg-type]
            agent_runner=SimpleNamespace(
                _transport=SimpleNamespace(_config=SimpleNamespace(model="m"))
            ),  # type: ignore[arg-type]
            approval_svc=Approval(),  # type: ignore[arg-type]
            cfg=cfg,
            skills={},
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
            agents_registry=SimpleNamespace(get_role_system_prompt=lambda role: f"role:{role}"),  # type: ignore[arg-type]
        ),
        app,
        mode,
    )


class Branches(WorkflowPlugin):
    name = "branches"
    phases = [PhaseSpec(name="phase", agent_type="auto")]


class UnknownNext(WorkflowPlugin):
    name = "unknown_next"
    phases = [PhaseSpec(name="phase", agent_type="auto", next="missing")]


class ParallelFailure(WorkflowPlugin):
    name = "parallel_failure"
    phases = [PhaseSpec(name="a", parallel_with=("b",), next="b"), PhaseSpec(name="b")]


async def _call_named_tools(kwargs: dict[str, object], name: str, arg: str) -> None:
    tools = kwargs.get("project_plugin_tools", [])
    for tool in tools:  # type: ignore[union-attr]
        if getattr(tool, "__name__", "") == name:
            await tool(arg)  # type: ignore[operator]


@pytest.mark.asyncio
async def test_phase_specific_continuations_and_results(monkeypatch: pytest.MonkeyPatch) -> None:
    config, app, mode = _config()
    runner = WorkflowRunner(Branches, config, mode)
    context = WorkflowContext("intent", "run", Branches.name)

    async def planner(_text: str, **kwargs: object) -> None:
        await _call_named_tools(kwargs, "request_plan_approval", "plan")
        await _call_named_tools(kwargs, "finalize_plan", "plan")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", planner)
    planned = await runner._run_phase(
        PhaseSpec(name="plan", agent_type="planner", require_plan_finalization=True),
        "intent",
        context,
    )
    assert planned.full_text == "plan"

    async def executor(_text: str, **kwargs: object) -> None:
        await _call_named_tools(kwargs, "mark_execute_complete", "done")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", executor)
    executed = await runner._run_phase(
        PhaseSpec(name="execute", agent_type="executor", require_explicit_completion=True),
        "intent",
        context,
    )
    assert executed.full_text == "done"

    async def reviewer(_text: str, **kwargs: object) -> None:
        await _call_named_tools(kwargs, "approve_review", "approved")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", reviewer)
    reviewed = await runner._run_phase(
        PhaseSpec(name="review", agent_type="reviewer", require_explicit_review=True),
        "intent",
        context,
    )
    assert reviewed.approved is True and reviewed.full_text == "approved"


@pytest.mark.asyncio
async def test_phase_retry_incomplete_and_agent_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _app, mode = _config()
    runner = WorkflowRunner(Branches, config, mode)
    context = WorkflowContext("intent", "run", Branches.name)

    async def no_decision(_text: str, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", no_decision)
    retry = await runner._run_phase(
        PhaseSpec(name="review", agent_type="reviewer", require_explicit_review=True),
        "intent",
        context,
    )
    assert retry.metadata["__next_phase__"] == "review"

    async def parse_output(_text: str, **kwargs: object) -> None:
        collector = kwargs.get("output_collector")
        if isinstance(collector, list):
            collector.append("<plan>text</plan>")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", parse_output)
    parsed = await runner._run_phase(
        PhaseSpec(name="plan", agent_type="planner", output_schema="plan"),
        "intent",
        context,
    )
    assert parsed.structured == {"plan_text": "text"}

    async def broken(_text: str, **_kwargs: object) -> None:
        raise RuntimeError("agent failed")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", broken)
    failed = await runner._run_phase(PhaseSpec(name="plain", agent_type="auto"), "intent", context)
    assert failed.approved is False and "agent failed" in failed.full_text


@pytest.mark.asyncio
async def test_phase_continuation_errors_and_missing_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _app, mode = _config()
    runner = WorkflowRunner(Branches, config, mode)
    context = WorkflowContext("intent", "run", Branches.name)

    async def broken(_text: str, **_kwargs: object) -> None:
        raise RuntimeError("turn broke")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", broken)
    for spec in (
        PhaseSpec(
            name="execute",
            agent_type="executor",
            require_explicit_completion=True,
            max_iterations=1,
        ),
        PhaseSpec(
            name="review", agent_type="reviewer", require_explicit_review=True, max_iterations=1
        ),
        PhaseSpec(
            name="plan", agent_type="planner", require_plan_finalization=True, max_iterations=1
        ),
    ):
        output = await runner._run_phase(spec, "intent", context)
        assert output.approved is False

    async def no_tools(_text: str, **kwargs: object) -> None:
        collector = kwargs.get("output_collector")
        if isinstance(collector, list):
            collector.append("plain")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", no_tools)
    plan_missing = await runner._run_phase(
        PhaseSpec(
            name="plan", agent_type="planner", require_plan_finalization=True, max_iterations=1
        ),
        "intent",
        context,
    )
    execute_missing = await runner._run_phase(
        PhaseSpec(
            name="execute",
            agent_type="executor",
            require_explicit_completion=True,
            max_iterations=1,
        ),
        "intent",
        context,
    )
    assert plan_missing.approved is False and execute_missing.approved is False

    async def incomplete(_text: str, **kwargs: object) -> None:
        collector = kwargs.get("output_collector")
        if isinstance(collector, list):
            collector.append("no review tag")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", incomplete)
    incomplete_output = await runner._run_phase(
        PhaseSpec(name="review", agent_type="reviewer", output_schema="review_result"),
        "intent",
        context,
    )
    assert incomplete_output.metadata["__next_phase__"] == "review"


@pytest.mark.asyncio
async def test_workflow_loop_unknown_parallel_cancel_and_human_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, app, mode = _config()

    async def no_op(_text: str, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", no_op)
    unknown = WorkflowRunner(UnknownNext, config, mode)
    await unknown.run("intent")
    assert app.workflow_run().status == "failed"

    parallel = WorkflowRunner(ParallelFailure, config, mode)

    async def parallel_run(spec: object, _intent: str, _context: object) -> object:
        if getattr(spec, "name", "") == "b":
            raise RuntimeError("parallel failure")
        from agenthicc.workflows.plugin import PhaseOutput

        return PhaseOutput("a", "auto", "ok")

    parallel._run_phase = parallel_run  # type: ignore[method-assign]
    await parallel.run("parallel")
    assert app.workflow_run().status == "complete"

    cancelled = WorkflowRunner(Branches, config, mode)

    async def cancel_phase(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    cancelled._run_phase = cancel_phase  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await cancelled.run("cancel")
    assert app.workflow_run().status == "failed"

    human = WorkflowRunner(Branches, config, mode)
    output = await human._run_phase(
        PhaseSpec(name="human", agent_type="human"),
        "intent",
        WorkflowContext("intent", "r", "w"),
    )
    assert output.approved is True

    with pytest.raises(TypeError):
        await human.resume(object())

    async def cancelled_turn(_text: str, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", cancelled_turn)
    for spec in (
        PhaseSpec(
            name="execute",
            agent_type="executor",
            require_explicit_completion=True,
            max_iterations=1,
        ),
        PhaseSpec(
            name="review", agent_type="reviewer", require_explicit_review=True, max_iterations=1
        ),
        PhaseSpec(
            name="plan", agent_type="planner", require_plan_finalization=True, max_iterations=1
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await human._run_phase(spec, "intent", WorkflowContext("intent", "r", "w"))

    with pytest.raises(asyncio.CancelledError):
        await human._run_phase(
            PhaseSpec(name="plain", agent_type="auto"),
            "intent",
            WorkflowContext("intent", "r", "w"),
        )

    rejected_runner = WorkflowRunner(Branches, config, mode)

    async def rejecter(_text: str, **kwargs: object) -> None:
        await _call_named_tools(kwargs, "reject_review", "fix this")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", rejecter)
    rejected = await rejected_runner._run_phase(
        PhaseSpec(name="review", agent_type="reviewer", require_explicit_review=True),
        "intent",
        WorkflowContext("intent", "r", "w"),
    )
    assert rejected.approved is False and rejected.full_text == "fix this"

    failed_loop = WorkflowRunner(Branches, config, mode)

    async def broken_phase(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("loop failed")

    failed_loop._run_phase = broken_phase  # type: ignore[method-assign]
    await failed_loop.run("loop failure")
    assert app.workflow_run().status == "failed"

    resume_context = WorkflowContext("intent", "r", UnknownNext.name)
    from agenthicc.workflows.plugin import PhaseOutput

    resume_context.add_output(PhaseOutput("phase", "auto", "done"))
    resume_context.add_output(PhaseOutput("missing", "auto", "done"))
    assert WorkflowRunner(UnknownNext, config, mode)._find_resume_phase(resume_context) is None

    from dataclasses import replace
    from agenthicc.tools.capabilities import ToolCapability, tool_write

    @tool_write
    def writes() -> None:
        return None

    def plain() -> None:
        return None

    class BadMcp:
        def all_tools(self) -> list[object]:
            raise RuntimeError("mcp unavailable")

    filtered_runner = WorkflowRunner(
        Branches,
        replace(config, plugin_tools=[writes, plain], mcp_registry=BadMcp()),
        mode,
    )
    filtered = filtered_runner._filter_tools(
        PhaseSpec(name="filter", allowed_capabilities=frozenset())
    )
    assert plain in filtered and writes not in filtered
    assert ToolCapability.WRITE not in app.active_mode().blocked_capabilities
    app.active_mode.set(SimpleNamespace(blocked_capabilities=frozenset({ToolCapability.WRITE})))
    assert writes not in filtered_runner._filter_tools(PhaseSpec(name="blocked"))
