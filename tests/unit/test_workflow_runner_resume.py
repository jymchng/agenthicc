"""Unit tests for WorkflowRunner.resume() and _find_resume_phase() (PRD-94, PRD-116)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.plugin import (
    PhaseOutput,
    PhaseSpec,
    WorkflowContext,
    WorkflowPlugin,
)
from agenthicc.workflows import WorkflowRunner

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_plugin(phases: list[PhaseSpec]) -> type[WorkflowPlugin]:
    _phases = list(phases)

    class _TestWorkflow(WorkflowPlugin):
        name = "test_wf"

    _TestWorkflow.phases = _phases
    return _TestWorkflow


def _make_runner(plugin_cls: type[WorkflowPlugin]) -> WorkflowRunner:
    """Build a WorkflowRunner with all dependencies mocked."""
    app_state = MagicMock()
    app_state.active_mode.return_value.blocked_capabilities = frozenset()

    cfg = WorkflowConfig(
        conv_store=MagicMock(),
        app_state=app_state,
        processor=MagicMock(),
        agent_runner=MagicMock(),
        approval_svc=None,
        cfg=MagicMock(),
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
    )
    runner = WorkflowRunner(plugin_cls, cfg)
    # Stub processor.emit
    runner._cfg.processor.emit = AsyncMock()
    return runner


# ── _find_resume_phase tests ──────────────────────────────────────────────────


class TestFindResumePhase:
    def test_empty_context_returns_first_phase(self):
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next="execute"),
                PhaseSpec(name="execute"),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        assert runner._find_resume_phase(ctx) == "plan"

    def test_first_phase_done_returns_second(self):
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next="execute"),
                PhaseSpec(name="execute"),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        ctx.add_output(
            PhaseOutput(phase_name="plan", role="planner", full_text="done", approved=True)
        )
        assert runner._find_resume_phase(ctx) == "execute"

    def test_all_phases_done_returns_none(self):
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next="execute"),
                PhaseSpec(name="execute"),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        ctx.add_output(PhaseOutput(phase_name="plan", role="planner", full_text="p", approved=True))
        ctx.add_output(PhaseOutput(phase_name="execute", role="executor", full_text="e"))
        assert runner._find_resume_phase(ctx) is None

    def test_on_reject_path_skipped_if_phase_approved(self):
        """When plan was approved, on_reject branch is ignored."""
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next="execute", on_reject="plan"),
                PhaseSpec(name="execute"),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        ctx.add_output(PhaseOutput(phase_name="plan", role="planner", full_text="p", approved=True))
        assert runner._find_resume_phase(ctx) == "execute"

    def test_no_phases_returns_none(self):
        plugin_cls = _make_plugin([])
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        assert runner._find_resume_phase(ctx) is None

    def test_cycle_guard(self):
        """If phase graph has a cycle of already-completed phases, returns None."""
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="a", next="b"),
                PhaseSpec(name="b", next="a"),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        ctx.add_output(PhaseOutput(phase_name="a", role="r", full_text=""))
        ctx.add_output(PhaseOutput(phase_name="b", role="r", full_text=""))
        result = runner._find_resume_phase(ctx)
        assert result is None


# ── resume() tests ────────────────────────────────────────────────────────────


class TestWorkflowRunnerResume:
    async def test_resume_all_done_marks_complete(self):
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next=None),
            ]
        )
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="run1", workflow_name="test_wf")
        ctx.add_output(
            PhaseOutput(phase_name="plan", role="planner", full_text="done", approved=True)
        )

        await runner.resume(ctx)

        wf_run = runner._cfg.app_state.workflow_run.set.call_args_list[-1][0][0]
        assert wf_run.status == "complete"
        assert runner._run_id == "run1"

    async def test_resume_missing_phase_runs_it(self):
        plugin_cls = _make_plugin(
            [
                PhaseSpec(name="plan", next="execute"),
                PhaseSpec(name="execute", next=None),
            ]
        )
        runner = _make_runner(plugin_cls)
        # Pre-populate plan phase only
        ctx = WorkflowContext(intent="x", run_id="run1", workflow_name="test_wf")
        ctx.add_output(
            PhaseOutput(phase_name="plan", role="planner", full_text="plan done", approved=True)
        )

        # Patch _run_phase_loop to just record which start_phase it got
        called_with: list[str | None] = []

        async def _fake_loop(intent, context, wf_run, run_id, start_phase):
            called_with.append(start_phase)

        runner._run_phase_loop = _fake_loop  # type: ignore[method-assign]

        await runner.resume(ctx)

        assert called_with == ["execute"]

    async def test_resume_sets_run_id_on_self(self):
        plugin_cls = _make_plugin([PhaseSpec(name="plan")])
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="my-run-id", workflow_name="wf")
        ctx.add_output(PhaseOutput(phase_name="plan", role="planner", full_text="done"))
        await runner.resume(ctx)
        assert runner._run_id == "my-run-id"

    async def test_resume_initialises_shared_memory(self):
        plugin_cls = _make_plugin([PhaseSpec(name="plan")])
        runner = _make_runner(plugin_cls)
        ctx = WorkflowContext(intent="x", run_id="r", workflow_name="wf")
        ctx.add_output(PhaseOutput(phase_name="plan", role="p", full_text=""))
        assert runner._shared_memory is None
        await runner.resume(ctx)
        assert runner._shared_memory is not None


# ── integration: restore_from_log + Workflow state ───────────────────────────


class TestRestoreFromLog:
    async def test_restore_produces_workflow_entry(self, tmp_path):
        """Replaying WorkflowRunStarted + phases + Completed via restore_from_log
        produces a fully populated kernel AppState.workflows entry."""
        import json

        from agenthicc.kernel import AppState, Event, SecurityPolicy, SystemSettings
        from agenthicc.kernel.processor import restore_from_log
        from agenthicc.kernel.state import NodeStatus

        log_path = str(tmp_path / "events.jsonl")
        events = [
            Event.create(
                "WorkflowRunStarted",
                {
                    "run_id": "r1",
                    "workflow_name": "code_plan",
                    "intent": "add auth",
                    "phase_names": ["plan", "execute"],
                },
            ),
            Event.create(
                "WorkflowPhaseCompleted",
                {
                    "run_id": "r1",
                    "phase_name": "plan",
                    "role": "planner",
                    "full_text": "Here is the plan.",
                    "approved": True,
                    "structured": {"plan_text": "step 1"},
                },
            ),
            Event.create(
                "WorkflowRunCompleted",
                {
                    "run_id": "r1",
                    "status": "complete",
                },
            ),
        ]
        with open(log_path, "w") as f:
            for e in events:
                f.write(json.dumps(e.to_dict()) + "\n")

        initial = AppState.create(
            settings=SystemSettings(event_log_path=log_path),
            policy=SecurityPolicy(),
        )
        restored = await restore_from_log(log_path, initial)

        assert "r1" in restored.workflows
        wf = restored.workflows["r1"]
        assert wf.name == "code_plan"
        assert wf.intent_text == "add auth"
        assert wf.status == NodeStatus.complete
        assert "plan" in wf.nodes
        assert wf.nodes["plan"].result["full_text"] == "Here is the plan."
