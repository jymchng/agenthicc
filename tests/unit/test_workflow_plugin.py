"""Unit tests: workflow/plugin.py — PhaseSpec, WorkflowPlugin, WorkflowContext (PRD-87, PRD-116)."""

from __future__ import annotations

import pytest

from agenthicc.agents.plugin import READ_CAPS
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.workflows.plugin import (
    PhaseRole,
    PhaseSpec,
    WorkflowContext,
    WorkflowPlugin,
    PhaseOutput,
    _parse_output_schema,
)

pytestmark = pytest.mark.unit


class TestPhaseRole:
    def test_planner_value(self):
        assert PhaseRole.PLANNER == "planner"

    def test_executor_value(self):
        assert PhaseRole.EXECUTOR == "executor"

    def test_is_string(self):
        assert isinstance(PhaseRole.PLANNER, str)

    def test_usable_as_agent_type(self):
        spec = PhaseSpec(name="p", agent_type=PhaseRole.PLANNER)
        assert spec.agent_type == "planner"


class TestPhaseSpec:
    def test_defaults(self):
        spec = PhaseSpec(name="p")
        assert spec.agent_type == "auto"
        assert spec.allowed_capabilities is None
        assert spec.max_turns == 20
        assert spec.next is None
        assert spec.max_iterations == -1

    def test_resolved_allowed_caps_planner_uses_role_default(self):
        spec = PhaseSpec(name="p", agent_type=PhaseRole.PLANNER)
        assert spec.resolved_allowed_caps == READ_CAPS

    def test_resolved_allowed_caps_executor_returns_none(self):
        spec = PhaseSpec(name="p", agent_type=PhaseRole.EXECUTOR)
        assert spec.resolved_allowed_caps is None

    def test_resolved_allowed_caps_explicit_field_wins(self):
        custom = frozenset({ToolCapability.READ})
        spec = PhaseSpec(name="p", agent_type=PhaseRole.PLANNER, allowed_capabilities=custom)
        assert spec.resolved_allowed_caps == custom

    def test_resolved_allowed_caps_override_wins_over_field(self):
        spec = PhaseSpec(
            name="p",
            agent_type=PhaseRole.PLANNER,
            allowed_capabilities=frozenset({ToolCapability.READ}),
            allowed_capabilities_override=frozenset({ToolCapability.SEARCH}),
        )
        assert spec.resolved_allowed_caps == frozenset({ToolCapability.SEARCH})

    def test_human_resolved_caps_is_empty(self):
        assert PhaseSpec(name="p", agent_type=PhaseRole.HUMAN).resolved_allowed_caps == frozenset()

    def test_frozen(self):
        spec = PhaseSpec(name="p")
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "changed"  # type: ignore[misc]


class TestWorkflowPlugin:
    def _plugin(self, *names: str) -> type[WorkflowPlugin]:
        specs = tuple(PhaseSpec(name=n) for n in names)

        class _Wf(WorkflowPlugin):
            name = "wf"
            phases = list(specs)

        return _Wf

    def test_get_phase_found(self):
        assert self._plugin("plan", "execute").get_phase("plan").name == "plan"

    def test_get_phase_missing(self):
        assert self._plugin("plan").get_phase("x") is None

    def test_first_phase(self):
        assert self._plugin("plan", "execute").first_phase().name == "plan"

    def test_first_phase_empty(self):
        class _Empty(WorkflowPlugin):
            name = "wf"
            phases = []

        assert _Empty.first_phase() is None

    def test_phase_names(self):
        assert self._plugin("a", "b", "c").phase_names() == ["a", "b", "c"]


class TestWorkflowContext:
    def _ctx(self):
        return WorkflowContext(intent="Fix bug", run_id="r1", workflow_name="wf")

    def test_block_no_outputs(self):
        block = self._ctx().as_system_block()
        assert "Original intent: Fix bug" in block
        assert "Completed phases" not in block

    def test_block_with_output(self):
        ctx = self._ctx()
        ctx.add_output(PhaseOutput(phase_name="plan", role="planner", full_text="Step 1."))
        block = ctx.as_system_block()
        assert "plan" in block and "Step 1" in block

    def test_output_truncated(self):
        ctx = self._ctx()
        ctx.add_output(PhaseOutput(phase_name="p", role="r", full_text="x" * 500))
        assert "..." in ctx.as_system_block()


class TestParseOutputSchema:
    def test_none(self):
        assert _parse_output_schema("x", None) is None

    def test_plan_found(self):
        assert _parse_output_schema("<plan>S1</plan>", "plan") == {"plan_text": "S1"}

    def test_plan_fallback(self):
        assert _parse_output_schema("no tags", "plan") == {"plan_text": "no tags"}

    def test_review_approved(self):
        assert (
            _parse_output_schema("<review>approved</review>", "review_result")["approved"] is True
        )

    def test_review_rejected(self):
        assert (
            _parse_output_schema("<review>rejected: x</review>", "review_result")["approved"]
            is False
        )

    def test_free_text(self):
        assert _parse_output_schema("hi", "free_text") == {"text": "hi"}

    def test_unknown(self):
        assert _parse_output_schema("hi", "other") == {"raw": "hi"}


class TestWorkflowPluginSubclass:
    def test_name_and_mode_bindings(self):
        class MyWf(WorkflowPlugin):
            name = "my_wf"
            description = "d"
            mode_bindings = ["Plan"]
            phases = [PhaseSpec(name="p", agent_type=PhaseRole.PLANNER)]

        assert MyWf.name == "my_wf"
        assert "Plan" in MyWf.mode_bindings

    def test_transition_approved(self):
        from unittest.mock import MagicMock
        from agenthicc.workflows import WorkflowRunner
        from agenthicc.workflows.config import WorkflowConfig

        class _Wf(WorkflowPlugin):
            name = "wf"
            phases = [PhaseSpec(name="p", next="q", on_reject="p")]

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
        runner = WorkflowRunner(_Wf, cfg)
        spec = PhaseSpec(name="p", next="q", on_reject="p")
        out = PhaseOutput(phase_name="p", role="r", approved=True)
        assert runner._determine_transition(spec, out) == "q"

    def test_transition_rejected(self):
        from unittest.mock import MagicMock
        from agenthicc.workflows import WorkflowRunner
        from agenthicc.workflows.config import WorkflowConfig

        class _Wf(WorkflowPlugin):
            name = "wf"
            phases = [PhaseSpec(name="p", next="q", on_reject="retry")]

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
        runner = WorkflowRunner(_Wf, cfg)
        spec = PhaseSpec(name="p", next="q", on_reject="retry")
        out = PhaseOutput(phase_name="p", role="r", approved=False)
        assert runner._determine_transition(spec, out) == "retry"
