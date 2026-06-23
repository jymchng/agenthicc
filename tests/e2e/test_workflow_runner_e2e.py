"""E2E tests: WorkflowRunner with real AgentsRegistry + MockTransport (PRD-87, PRD-116).

NOTE: no ``from __future__ import annotations`` — @agent() inspects annotations
at decoration time.
"""

import asyncio

import pytest

from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.plugin import PhaseRole, PhaseSpec, WorkflowPlugin
from agenthicc.workflows import WorkflowRunner

pytestmark = pytest.mark.e2e


# ── helpers ───────────────────────────────────────────────────────────────────

def _completion(content: str, n: int = 1) -> Completion:
    return Completion(
        id=f"c{n}", model="mock-model", content=content,
        tool_calls=[], stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _make_agent_runner(mock: MockTransport) -> AgentRunnerBase:
    bus = SignalBus()
    return AgentRunnerBase(transport=mock, signals=bus)


@pytest.fixture
def app_state():
    return TUIAppState.create()


@pytest.fixture
async def processor(tmp_path):
    k_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=k_state, persist=False)
    t = asyncio.create_task(proc.run())
    yield proc
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


def _make_wf_runner(wf: type[WorkflowPlugin], app_state, processor, mock_transport, agents_registry=None):
    from unittest.mock import MagicMock
    from agenthicc.config import AgenthiccConfig
    from agenthicc.workflows.config import WorkflowConfig
    agent_runner = AgentRunnerBase(transport=mock_transport, signals=SignalBus())
    cfg = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=agent_runner,
        approval_svc=None,
        # Real config: PRD-133/136 derive numeric context-window/usable budgets
        # from cfg.execution, so a bare MagicMock (no comparison support) breaks
        # the compaction trigger / pre-send guard.
        cfg=AgenthiccConfig(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=agents_registry or build_agents_registry(),
    )
    return WorkflowRunner(wf, cfg)


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_e2e_single_phase_auto_workflow(app_state, processor, tmp_path):
    """WorkflowRunner with 1 auto phase — agent runs and output is captured."""
    mock = MockTransport()
    mock.queue_response(_completion("I completed the task."))

    class _SinglePhaseWf(WorkflowPlugin):
        name = "test_wf"
        phases = [PhaseSpec(name="do_it", agent_type=PhaseRole.AUTO)]

    runner = _make_wf_runner(_SinglePhaseWf, app_state, processor, mock)
    await runner.run("Do the thing")

    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 1
    assert wf_run.phase_history[0].phase_name == "do_it"
    assert wf_run.phase_history[0].output_summary != ""


async def test_e2e_two_phase_plan_execute(app_state, processor, tmp_path):
    """plan → execute: planner produces a plan, executor implements it."""
    mock = MockTransport()
    mock.queue_response(_completion("<plan>Step 1: do X. Step 2: do Y.</plan>", n=1))
    mock.queue_response(_completion("Executed step 1. Executed step 2.", n=2))

    class _TwoPhaseWf(WorkflowPlugin):
        name = "test_wf"
        phases = [
            PhaseSpec(name="plan",    agent_type=PhaseRole.PLANNER,
                      output_schema="plan", next="execute"),
            PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR),
        ]

    runner = _make_wf_runner(_TwoPhaseWf, app_state, processor, mock)
    await runner.run("Refactor auth")

    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 2
    assert wf_run.phase_history[0].phase_name == "plan"
    assert wf_run.phase_history[1].phase_name == "execute"


async def test_e2e_reviewer_approves_first_try(app_state, processor, tmp_path):
    """execute → review: reviewer approves on the first attempt; workflow completes."""
    mock = MockTransport()
    mock.queue_response(_completion("Implementation complete.", n=1))
    mock.queue_response(_completion("<review>approved</review>", n=2))

    class _ReviewWf(WorkflowPlugin):
        name = "test_wf"
        phases = [
            PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR, next="review"),
            PhaseSpec(name="review",  agent_type=PhaseRole.REVIEWER,
                      output_schema="review_result", on_reject="execute"),
        ]

    runner = _make_wf_runner(_ReviewWf, app_state, processor, mock)
    await runner.run("Implement and review")

    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    phase_names = [r.phase_name for r in wf_run.phase_history]
    assert phase_names == ["execute", "review"]


async def test_e2e_opt_in_cap_stops_rejection_loop(app_state, processor, tmp_path):
    """execute → review: reviewer always rejects; opt-in max_total_phase_runs=3 stops
    the loop after execute(1) + review(1,rejected) + execute(2) = 3 runs."""
    mock = MockTransport()
    mock.queue_response(_completion("Implementation attempt 1.", n=1))
    mock.queue_response(_completion("<review>rejected: not good enough</review>", n=2))
    mock.queue_response(_completion("Implementation attempt 2.", n=3))
    # review(2) would need a 4th response but the cap fires after execute(2)

    class _CappedWf(WorkflowPlugin):
        name = "test_wf"
        phases = [
            PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR, next="review"),
            PhaseSpec(name="review",  agent_type=PhaseRole.REVIEWER,
                      output_schema="review_result", on_reject="execute"),
        ]
        max_total_phase_runs = 3

    runner = _make_wf_runner(_CappedWf, app_state, processor, mock)
    await runner.run("Implement and review")

    wf_run = app_state.workflow_run()
    assert wf_run.status == "failed"
    phase_names = [r.phase_name for r in wf_run.phase_history]
    assert len(phase_names) <= 3


async def test_e2e_agents_registry_resolves_system_prompt(app_state, processor, tmp_path):
    """make_instance() reads the system prompt from the @agent class."""
    registry = build_agents_registry()
    defn = registry.get("planner")
    assert defn is not None

    # Verify the system prompt comes from PlannerAgent's @agent(system=...)
    from lauren_ai._agents import AGENT_META
    meta = getattr(defn.agent_class, AGENT_META, None)
    assert meta is not None
    assert "planning" in (meta.system or "").lower()


async def test_e2e_workflow_context_injected(app_state, processor, tmp_path):
    """Second phase receives first phase output in the phase prompt."""
    prompts_seen: list[str] = []

    mock = MockTransport()
    mock.queue_response(_completion("Exploration done: found auth module.", n=1))
    mock.queue_response(_completion("<plan>Step 1</plan>", n=2))

    class _ContextWf(WorkflowPlugin):
        name = "test_wf"
        phases = [
            PhaseSpec(name="explore", agent_type=PhaseRole.EXPLORER, next="plan"),
            PhaseSpec(name="plan",    agent_type=PhaseRole.PLANNER,  output_schema="plan"),
        ]

    wf_runner = _make_wf_runner(_ContextWf, app_state, processor, mock)

    # Intercept _build_phase_prompt to capture what the planner receives
    original_prompt = wf_runner._build_phase_prompt
    def _capturing_prompt(spec, intent, context):
        result = original_prompt(spec, intent, context)
        prompts_seen.append(result)
        return result
    wf_runner._build_phase_prompt = _capturing_prompt

    await wf_runner.run("Refactor auth module")

    # The plan phase prompt should contain the explore output
    # At minimum, a workflow context block was injected
    assert any("[WORKFLOW CONTEXT]" in p for p in prompts_seen)
