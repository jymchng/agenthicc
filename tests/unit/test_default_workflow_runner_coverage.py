"""Execution-path coverage for the generic workflow runner."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime import ModeManager, ModeRegistry, RuntimeMode
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec, WorkflowContext, WorkflowPlugin

pytestmark = pytest.mark.unit


def _runner_config() -> tuple[WorkflowConfig, AppState, list[object], ModeManager]:
    app = AppState.create()
    emitted: list[object] = []

    async def emit(event: object) -> None:
        emitted.append(event)

    cfg = AgenthiccConfig()
    processor = SimpleNamespace(emit=emit)
    modes = ModeRegistry()
    modes.register(RuntimeMode("Auto", badge="A"))
    modes.register(RuntimeMode("Plan", badge="P", blocked_capabilities=frozenset()))
    mode = ModeManager(modes, app)
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=processor,
        agent_runner=SimpleNamespace(_transport=SimpleNamespace(_config=SimpleNamespace(model=""))),
        approval_svc=None,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),
        agents_registry=SimpleNamespace(get_role_system_prompt=lambda role: f"role:{role}"),
    )
    return config, app, emitted, mode


class OnePhase(WorkflowPlugin):
    name = "one_phase"
    phases = [PhaseSpec(name="plan", agent_type="planner", output_schema="plan")]


class Parallel(WorkflowPlugin):
    name = "parallel"
    phases = [
        PhaseSpec(name="a", next=None, parallel_with=("b",)),
        PhaseSpec(name="b", next=None),
    ]


@pytest.mark.asyncio
async def test_workflow_runner_run_resume_parallel_and_phase_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.workflows import default

    config, app, emitted, mode = _runner_config()
    calls: list[str] = []

    async def run_turn(text: str, **kwargs: object) -> None:
        calls.append(text)
        collector = kwargs.get("output_collector")
        if isinstance(collector, list):
            collector.append("<plan>safe plan</plan>")

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", run_turn)
    runner = WorkflowRunner(OnePhase, config, mode)
    context = await runner.run("build it")
    assert context.phase_outputs["plan"].structured == {"plan_text": "safe plan"}
    assert app.workflow_run().status == "complete"
    assert emitted
    assert runner._build_phase_prompt(OnePhase.phases[0], "intent", context).startswith("[WORKFLOW")
    assert (
        runner._determine_transition(OnePhase.phases[0], PhaseOutput("x", "x", approved=False))
        is None
    )
    assert runner._find_resume_phase(context) is None

    resumed = WorkflowContext("build it", "resume", OnePhase.name, dict(context.phase_outputs))
    await runner.resume(resumed)
    assert app.workflow_run().status == "complete"

    parallel_runner = WorkflowRunner(Parallel, config, mode)
    parallel_context = await parallel_runner.run("parallel work")
    assert set(parallel_context.phase_outputs) == {"a", "b"}

    monkeypatch.setattr(default, "_run_agent_turn", run_turn, raising=False)
    error = await runner._run_phase(
        PhaseSpec(name="error", agent_type="auto", mode_override="missing"),
        "intent",
        context,
    )
    assert error.approved is None


@pytest.mark.asyncio
async def test_workflow_runner_human_and_limit_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, app, _emitted, _mode = _runner_config()

    class Limited(WorkflowPlugin):
        name = "limited"
        max_total_phase_runs = 1
        phases = [PhaseSpec(name="first", next="missing")]

    runner = WorkflowRunner(Limited, config)

    async def noop(text: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", noop)
    await runner.run("limit")
    assert app.workflow_run().status == "failed"

    no_approval = await runner._run_human_phase(
        PhaseSpec(name="human", agent_type="human"), WorkflowContext("x", "r", "limited")
    )
    assert no_approval.approved is True

    # A human phase with an approval service uses its response message and decision.
    class Approval:
        async def request_approval(self, request: object) -> object:
            return SimpleNamespace(allowed=False, message="needs changes")

    approval_runner = WorkflowRunner(Limited, dataclasses.replace(config, approval_svc=Approval()))
    output = await approval_runner._run_human_phase(
        PhaseSpec(name="human", agent_type="human"),
        WorkflowContext("x", "r", "limited", {"first": PhaseOutput("first", "auto", "prior")}),
    )
    assert output.approved is False and output.full_text == "needs changes"
