"""Tests for composite workflow architecture (PRD-114)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.code_plan import CodePlanRunner
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
from agenthicc.workflows.code_plan.definition import CodePlan


# ── BaseWorkflowRunner contract ───────────────────────────────────────────────


@pytest.mark.unit
def test_base_runner_run_return_type_is_any() -> None:
    """run() return annotation is object so subclasses may narrow it."""
    import inspect

    hints = inspect.get_annotations(BaseWorkflowRunner.run, eval_str=True)
    assert hints.get("return") is object


@pytest.mark.unit
def test_base_runner_is_abstract() -> None:
    import abc

    assert abc.ABC in BaseWorkflowRunner.__mro__


# ── CodePlanRunner.run() returns CodePlanContext ──────────────────────────────


@pytest.mark.unit
async def test_code_plan_runner_run_returns_code_plan_context() -> None:
    """run() must return a CodePlanContext, not None."""
    import inspect

    hints = inspect.get_annotations(CodePlanRunner.run, eval_str=True)
    assert hints.get("return") is CodePlanContext


@pytest.mark.unit
async def test_code_plan_runner_run_result_accessible_via_super() -> None:
    """A subclass can receive the typed context from super().run()."""
    received: list[CodePlanContext] = []

    class _TestRunner(CodePlanRunner):
        async def run(self, intent: str) -> None:
            ctx = await super().run(intent)
            received.append(ctx)

    mock_cfg = MagicMock()
    mock_cfg.app_state.workflow_run.set = MagicMock()
    mock_cfg.app_state.active_mode.return_value = MagicMock(
        blocked_capabilities=frozenset(), badge="P"
    )
    mock_cfg.processor.emit = AsyncMock()
    mock_cfg.conv_store.append_event = MagicMock()
    mock_cfg.conv_store.close_turn = MagicMock()
    mock_cfg.approval_svc = None
    mock_cfg.plugin_tools = []
    mock_cfg.mcp_registry = None
    mock_cfg.memory_router = None
    mock_cfg.semantic_index = None
    mock_cfg.skills = {}
    mock_cfg.mention_cache = MagicMock()
    mock_cfg.completed_turns = 0
    mock_cfg.cfg.execution = MagicMock()
    mock_cfg.agent_runner = MagicMock()

    runner = _TestRunner(mock_cfg, None)

    # Patch the state machine so it exits immediately
    with patch.object(runner, "_plan", new=AsyncMock(return_value=CodePlanState.EXITED)):
        await runner.run("test intent")

    assert len(received) == 1
    assert isinstance(received[0], CodePlanContext)
    assert received[0].intent == "test intent"


# ── run_phase() public API ────────────────────────────────────────────────────


@pytest.mark.unit
def test_code_plan_runner_has_run_phase_method() -> None:
    assert hasattr(CodePlanRunner, "run_phase")
    assert callable(CodePlanRunner.run_phase)


@pytest.mark.unit
def test_run_phase_is_public() -> None:
    """run_phase must not start with underscore."""
    assert not "run_phase".__contains__("_run_phase")
    # double-check via name
    assert "run_phase" in dir(CodePlanRunner)
    assert not CodePlanRunner.run_phase.__name__.startswith("_")


@pytest.mark.unit
async def test_run_phase_calls_run_turn() -> None:
    """run_phase() delegates to _run_turn() internally."""
    mock_cfg = MagicMock()
    mock_cfg.app_state.active_mode.return_value = MagicMock(blocked_capabilities=frozenset())
    mock_cfg.approval_svc = None
    mock_cfg.plugin_tools = []
    mock_cfg.mcp_registry = None
    mock_cfg.memory_router = None
    mock_cfg.semantic_index = None

    runner = CodePlanRunner(mock_cfg, None)
    runner._run_id = "test-run"

    called_with: list = []

    async def fake_run_turn(text, *, tools, mode, system_prompt, max_turns, ctx):
        called_with.append(
            {
                "text": text,
                "mode": mode,
                "system_prompt": system_prompt,
                "max_turns": max_turns,
            }
        )

    with patch.object(runner, "_run_turn", new=fake_run_turn):
        with patch(
            "agenthicc.workflows.code_plan.runner.CodePlanRunner._base_tools", return_value=[]
        ):
            await runner.run_phase(
                intent="my intent",
                text="do the thing",
                system_prompt="you are a helper",
                mode="Auto",
                max_turns=5,
            )

    assert len(called_with) == 1
    assert called_with[0]["text"] == "do the thing"
    assert called_with[0]["mode"] == "Auto"
    assert called_with[0]["system_prompt"] == "you are a helper"
    assert called_with[0]["max_turns"] == 5


@pytest.mark.unit
async def test_run_phase_passes_shared_memory() -> None:
    """run_phase() forwards the caller's shared_memory to _run_turn()."""
    from unittest.mock import sentinel

    mock_cfg = MagicMock()
    mock_cfg.app_state.active_mode.return_value = MagicMock(blocked_capabilities=frozenset())
    mock_cfg.approval_svc = None
    mock_cfg.plugin_tools = []
    mock_cfg.mcp_registry = None
    mock_cfg.memory_router = None
    mock_cfg.semantic_index = None

    runner = CodePlanRunner(mock_cfg, None)
    runner._run_id = "test-run"

    received_ctx: list = []

    async def fake_run_turn(text, *, tools, mode, system_prompt, max_turns, ctx):
        received_ctx.append(ctx)

    fake_memory = sentinel.shared_memory

    with patch.object(runner, "_run_turn", new=fake_run_turn):
        with patch(
            "agenthicc.workflows.code_plan.runner.CodePlanRunner._base_tools", return_value=[]
        ):
            await runner.run_phase(
                intent="intent",
                text="text",
                system_prompt="prompt",
                shared_memory=fake_memory,
            )

    assert len(received_ctx) == 1
    assert received_ctx[0].shared_memory is fake_memory


# ── WorkflowRunner.run() returns WorkflowContext ──────────────────────────────


@pytest.mark.unit
def test_workflow_runner_run_return_annotation() -> None:
    import inspect
    from agenthicc.workflows.default.runner import WorkflowRunner

    # With from __future__ import annotations, the hint is stored as a string.
    hints = inspect.get_annotations(WorkflowRunner.run)
    assert "WorkflowContext" in str(hints.get("return", ""))


# ── Composite workflow pattern ─────────────────────────────────────────────────


@pytest.mark.unit
def test_composite_runner_inherits_code_plan_runner() -> None:
    """A composite runner subclasses CodePlanRunner correctly."""

    class _MyRunner(CodePlanRunner):
        async def run(self, intent: str) -> None:
            ctx = await super().run(intent)
            # type is CodePlanContext
            assert isinstance(ctx, CodePlanContext)

    assert issubclass(_MyRunner, CodePlanRunner)
    assert issubclass(_MyRunner, BaseWorkflowRunner)


@pytest.mark.unit
def test_composite_plugin_subclasses_code_plan() -> None:
    class _MyPlugin(CodePlan):
        name = "my_composite"
        mode_bindings = ["Plan"]

    assert _MyPlugin.name == "my_composite"
    assert "Plan" in _MyPlugin.mode_bindings


@pytest.mark.unit
def test_composite_plugin_registered_in_registry() -> None:
    """A composite plugin placed in the registry is discoverable by name."""
    from agenthicc.workflows.registry import WorkflowRegistry

    class _MyPlugin(CodePlan):
        name = "my_composite_v2"
        mode_bindings = []

    registry = WorkflowRegistry()
    registry.register(_MyPlugin, source="user")
    assert registry.get("my_composite_v2") is not None
    assert registry.get("code_plan") is None  # only registered one


# ── /workflow command logic ────────────────────────────────────────────────────


@pytest.mark.unit
def test_workflow_override_signal_on_conversation_store() -> None:
    """ConversationStore.workflow_override signal exists and defaults to None."""
    from agenthicc.tui.conversation_store import ConversationStore

    store = ConversationStore()
    assert hasattr(store, "workflow_override")
    assert store.workflow_override() is None


@pytest.mark.unit
def test_workflow_override_signal_settable() -> None:
    from agenthicc.tui.conversation_store import ConversationStore

    store = ConversationStore()
    store.workflow_override.set("code_plan_docs")
    assert store.workflow_override() == "code_plan_docs"
    store.workflow_override.set(None)
    assert store.workflow_override() is None


# ── create-workflow skill in bootstrap ────────────────────────────────────────


@pytest.mark.unit
def test_create_workflow_skill_in_defaults() -> None:
    from agenthicc.skills.bootstrap import _DEFAULTS

    assert "create-workflow" in _DEFAULTS


@pytest.mark.unit
def test_create_workflow_skill_has_required_frontmatter() -> None:
    from agenthicc.skills.bootstrap import _DEFAULTS

    content = _DEFAULTS["create-workflow"]
    assert "name:" in content
    assert "source: default" in content
    assert "WorkflowPlugin" in content
    assert "run_phase" in content


@pytest.mark.unit
def test_create_workflow_skill_bootstrap_includes_it(tmp_path) -> None:
    from agenthicc.skills.bootstrap import bootstrap_default_skills

    n = bootstrap_default_skills(global_dir=tmp_path)
    assert n >= 7  # original 6 + create-workflow
    assert (tmp_path / "skills" / "create-workflow").is_dir()
    assert (tmp_path / "skills" / "create-workflow" / "SKILL.md").exists()
