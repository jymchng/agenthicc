"""Integration tests: WorkflowRunner with mocked _run_phase (PRD-87, PRD-116)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.plugin import (
    PhaseOutput, PhaseRole, PhaseSpec, WorkflowPlugin,
)

pytestmark = pytest.mark.integration


# ── fixtures ──────────────────────────────────────────────────────────────────

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


def _make_plugin(*specs: PhaseSpec) -> type[WorkflowPlugin]:
    _phases = list(specs)

    class _TestWorkflow(WorkflowPlugin):
        name = "test_wf"
        phases = _phases

    return _TestWorkflow


def _make_runner(wf: type[WorkflowPlugin], app_state, processor):
    from agenthicc.workflows import WorkflowRunner
    from agenthicc.workflows.config import WorkflowConfig
    agents_registry = MagicMock()
    agent_runner = MagicMock()
    agent_runner._transport = MagicMock()
    agent_runner._signals   = None
    cfg = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=agent_runner,
        approval_svc=None,
        cfg=MagicMock(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=agents_registry,
    )
    return WorkflowRunner(wf, cfg)


def _patch_run_phase(runner, outputs: dict[str, PhaseOutput]):
    """Patch WorkflowRunner._run_phase to return canned outputs by phase name."""
    async def _fake_run_phase(spec, intent, context):
        out = outputs.get(spec.name) or PhaseOutput(
            phase_name=spec.name, role=spec.agent_type, full_text="ok",
        )
        context.add_output(out)
        return out
    runner._run_phase = _fake_run_phase


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_single_phase_workflow_completes(app_state, processor):
    wf     = _make_plugin(PhaseSpec(name="plan", agent_type=PhaseRole.PLANNER))
    runner = _make_runner(wf, app_state, processor)
    _patch_run_phase(runner, {"plan": PhaseOutput(
        phase_name="plan", role="planner", full_text="Step 1. Step 2.",
    )})
    await runner.run("Fix the bug")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert len(wf_run.phase_history) == 1
    assert wf_run.phase_history[0].phase_name == "plan"


async def test_two_phase_sequential(app_state, processor):
    wf = _make_plugin(
        PhaseSpec(name="plan",    agent_type=PhaseRole.PLANNER, next="execute"),
        PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_phase(runner, {
        "plan":    PhaseOutput(phase_name="plan",    role="planner",  full_text="plan done"),
        "execute": PhaseOutput(phase_name="execute", role="executor", full_text="exec done"),
    })
    await runner.run("Do the work")
    wf_run = app_state.workflow_run()
    assert wf_run.status == "complete"
    assert [r.phase_name for r in wf_run.phase_history] == ["plan", "execute"]


async def test_on_reject_loops_back_and_eventually_completes(app_state, processor):
    """on_reject routes back; second review approves; workflow completes."""
    call_count: dict[str, int] = {}

    wf = _make_plugin(
        PhaseSpec(name="plan",   agent_type=PhaseRole.PLANNER,  next="review"),
        PhaseSpec(name="review", agent_type=PhaseRole.REVIEWER, on_reject="plan"),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _phase(spec, intent, context):
        call_count[spec.name] = call_count.get(spec.name, 0) + 1
        # review rejects once, approves on second attempt
        approved = None
        if spec.name == "review":
            approved = call_count["review"] >= 2
        out = PhaseOutput(
            phase_name=spec.name, role=spec.agent_type,
            full_text="ok", approved=approved,
        )
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("Fix it")
    assert call_count.get("plan", 0) == 2
    assert call_count.get("review", 0) == 2
    assert app_state.workflow_run().status == "complete"


async def test_per_phase_max_iterations_stops_loop(app_state, processor):
    """Per-phase max_iterations terminates a phase that keeps rejecting."""
    wf = _make_plugin(
        PhaseSpec(name="plan",   agent_type=PhaseRole.PLANNER,  next="review"),
        PhaseSpec(name="review", agent_type=PhaseRole.REVIEWER,
                  on_reject="plan", max_iterations=2),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _phase(spec, intent, context):
        out = PhaseOutput(
            phase_name=spec.name, role=spec.agent_type, full_text="x",
            approved=False if spec.name == "review" else None,
        )
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("Always fail")
    assert app_state.workflow_run().status == "failed"


async def test_opt_in_global_cap_stops_infinite_loop(app_state, processor):
    """max_total_phase_runs on a WorkflowPlugin subclass provides an opt-in hard ceiling."""
    call_count: dict[str, int] = {}

    class _CappedWf(WorkflowPlugin):
        name = "test_wf"
        phases = [
            PhaseSpec(name="plan",   agent_type=PhaseRole.PLANNER,  next="review"),
            PhaseSpec(name="review", agent_type=PhaseRole.REVIEWER, on_reject="plan"),
        ]
        max_total_phase_runs = 3  # hard ceiling: plan + review + plan = 3, then stop

    runner = _make_runner(_CappedWf, app_state, processor)

    async def _phase(spec, intent, context):
        call_count[spec.name] = call_count.get(spec.name, 0) + 1
        out = PhaseOutput(
            phase_name=spec.name, role=spec.agent_type, full_text="x",
            approved=False if spec.name == "review" else None,
        )
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("Loop forever")
    assert sum(call_count.values()) <= 3
    assert app_state.workflow_run().status == "failed"


async def test_no_global_cap_by_default(app_state, processor):
    """Default WorkflowPlugin has no global cap; only per-phase limits apply."""
    wf = _make_plugin(
        PhaseSpec(name="plan",      next="execute"),
        PhaseSpec(name="execute",   next="review"),
        PhaseSpec(name="review",    next="summarize"),
        PhaseSpec(name="summarize"),
    )
    runner = _make_runner(wf, app_state, processor)
    _patch_run_phase(runner, {})
    await runner.run("Do the work")
    assert app_state.workflow_run().status == "complete"
    assert len(app_state.workflow_run().phase_history) == 4


async def test_workflow_run_signal_updates(app_state, processor):
    wf = _make_plugin(
        PhaseSpec(name="plan",    agent_type=PhaseRole.PLANNER,  next="execute"),
        PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR),
    )
    runner = _make_runner(wf, app_state, processor)
    phases_seen: list[str | None] = []
    app_state.workflow_run.subscribe(
        lambda: phases_seen.append(
            getattr(app_state.workflow_run(), "current_phase", None)
        )
    )
    _patch_run_phase(runner, {})
    await runner.run("work")
    assert "plan" in phases_seen or None in phases_seen  # signal fired


async def test_parallel_phases(app_state, processor):
    called: list[str] = []
    wf = _make_plugin(
        PhaseSpec(name="exp_a", agent_type=PhaseRole.EXPLORER,
                  parallel_with=("exp_b",), next="plan"),
        PhaseSpec(name="exp_b", agent_type=PhaseRole.EXPLORER,
                  parallel_with=("exp_a",), next="plan"),
        PhaseSpec(name="plan",  agent_type=PhaseRole.PLANNER),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _phase(spec, intent, context):
        called.append(spec.name)
        out = PhaseOutput(phase_name=spec.name, role=spec.agent_type, full_text="ok")
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("explore then plan")
    assert "exp_a" in called and "exp_b" in called and "plan" in called
    # plan runs exactly once
    assert called.count("plan") == 1


async def test_dynamic_next_override(app_state, processor):
    wf = _make_plugin(
        PhaseSpec(name="plan",    agent_type=PhaseRole.PLANNER,  next="execute"),
        PhaseSpec(name="execute", agent_type=PhaseRole.EXECUTOR),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _phase(spec, intent, context):
        # plan phase overrides next to None → skip execute
        meta = {"__next_phase__": None} if spec.name == "plan" else {}
        out = PhaseOutput(
            phase_name=spec.name, role=spec.agent_type, full_text="ok", metadata=meta,
        )
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("work")
    wf_run = app_state.workflow_run()
    # only plan ran; execute was skipped
    assert len(wf_run.phase_history) == 1
    assert wf_run.phase_history[0].phase_name == "plan"
    assert wf_run.status == "complete"
