"""Integration tests: WorkflowRunner with mocked _run_phase (PRD-87)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflow.plugin import (
    PhaseOutput, PhaseRole, PhaseSpec, WorkflowDefinition,
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


def _make_workflow(*specs: PhaseSpec) -> WorkflowDefinition:
    return WorkflowDefinition(name="test_wf", phases=specs)


def _make_runner(wf, app_state, processor):
    from agenthicc.workflow.runner import WorkflowRunner
    agents_registry = MagicMock()
    runner = MagicMock()
    runner._transport = MagicMock()
    runner._signals   = None
    return WorkflowRunner(
        definition=wf,
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=runner,
        session_mem=MagicMock(),
        approval_svc=None,
        cfg=MagicMock(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=agents_registry,
    )


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
    wf     = _make_workflow(PhaseSpec(name="plan", agent_type=PhaseRole.PLANNER))
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
    wf = _make_workflow(
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


async def test_on_reject_loops_back(app_state, processor):
    call_count: dict[str, int] = {}

    wf = _make_workflow(
        PhaseSpec(name="plan",   agent_type=PhaseRole.PLANNER,  next="review"),
        PhaseSpec(name="review", agent_type=PhaseRole.REVIEWER,
                  on_reject="plan", max_iterations=3),
    )
    runner = _make_runner(wf, app_state, processor)

    async def _phase(spec, intent, context):
        call_count[spec.name] = call_count.get(spec.name, 0) + 1
        approved = call_count.get("review", 0) >= 2   # approve on second review
        out = PhaseOutput(
            phase_name=spec.name, role=spec.agent_type,
            full_text="ok", approved=approved if spec.name == "review" else None,
        )
        context.add_output(out)
        return out

    runner._run_phase = _phase
    await runner.run("Fix it")
    assert call_count.get("plan", 0) == 2
    assert call_count.get("review", 0) == 2
    assert app_state.workflow_run().status == "complete"


async def test_max_iterations_fails_workflow(app_state, processor):
    wf = _make_workflow(
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


async def test_workflow_run_signal_updates(app_state, processor):
    wf = _make_workflow(
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
    wf = _make_workflow(
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
    wf = _make_workflow(
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
