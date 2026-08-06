"""Unit tests for the create_workflow meta-workflow.

Covers, in order:

* :mod:`state` — terminality, artefacts, context bookkeeping
* :mod:`phase_tools` — the tool-call-only transition contract
* :mod:`inspection_tools` — the read-only authoring-surface tools
* :mod:`validation` — deterministic import-and-check of a generated file
* :mod:`definition` — the plugin metadata and params
* :mod:`runner` — the outer state machine and every phase method
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow.definition import CreateWorkflow, CreateWorkflowParams
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.phase_tools import (
    _transition_failure,
    make_design_tools,
    make_generation_tools,
    make_validation_tools,
    validate_workflow_name,
)
from agenthicc.workflows.create_workflow.runner import (
    _PHASE_INDEX,
    _PHASE_NAMES,
    CreateWorkflowRunner,
)
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.create_workflow.validation import (
    ValidationReport,
    validate_workflow_file,
)
from agenthicc.workflows.plugin import PhaseRole, PhaseSpec, WorkflowContext, WorkflowParams

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

#: A declarative-only workflow: valid, but flagged for shipping no runner.
_GOOD_SOURCE = '''\
"""demo workflow."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class Demo(WorkflowPlugin):
    name = "demo"
    description = "A demo workflow."
    mode_bindings = []
    phases = [
        PhaseSpec(name="one", next="two"),
        PhaseSpec(name="two", on_reject="one"),
    ]

'''

#: The recommended shape: the workflow ships its own state-machine runner.
_RUNNER_SOURCE = '''\
"""demo workflow with its own runner."""

from __future__ import annotations

from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DemoRunner(BaseWorkflowRunner):
    def __init__(self, config, mode_manager=None):
        self._cfg = config

    async def run(self, intent: str) -> str:
        return intent

    async def resume(self, context: object) -> str:
        return ""


class Demo(WorkflowPlugin):
    name = "demo"
    description = "A demo workflow."
    mode_bindings = []
    phases = [
        PhaseSpec(name="one", next="two"),
        PhaseSpec(name="two", on_reject="one"),
    ]

    @classmethod
    def build_runner(cls, config, mode_manager):
        return DemoRunner(config, mode_manager)

    @classmethod
    def checkpoint_context_to_payload(cls, context):
        return {"context": str(context)}

    @classmethod
    def checkpoint_context_from_payload(cls, payload, memory=None):
        return payload.get("context", "")
'''


def _write(root: Path, name: str, source: str) -> Path:
    """Write *source* to ``<root>/workflows/<name>`` and return the path."""
    directory = root / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def _runner(*, max_attempts: int = 3, max_turns: int = 5) -> CreateWorkflowRunner:
    """Build a CreateWorkflowRunner over stubbed session singletons."""
    app = AppState.create()

    async def emit(_event: object) -> None:
        return None

    cfg = AgenthiccConfig()
    cfg.execution.model = "global"  # type: ignore[misc]
    cfg.execution.authoring_max_generation_attempts = max_attempts
    cfg.execution.authoring_max_phase_turns = max_turns
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
    runner = CreateWorkflowRunner(config)
    runner._cfg.app_state.update_workflow_phase = MagicMock()  # type: ignore[method-assign]
    runner._cfg.conv_store.append_event = MagicMock()  # type: ignore[method-assign]
    return runner


def _ctx(intent: str = "make me a workflow") -> CreateWorkflowContext:
    return CreateWorkflowContext(intent=intent, run_id="run", shared_memory=MagicMock())


def _by_name(tools: object) -> dict[str, object]:
    """Index a phase's tool list by tool function name."""
    assert isinstance(tools, list)
    return {getattr(tool, "__name__", ""): tool for tool in tools}


async def _call(tools: object, name: str, *args: object) -> object:
    """Invoke the tool called *name* from a phase's tool list."""
    tool = _by_name(tools)[name]
    assert callable(tool)
    return await tool(*args)


# ── state ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "terminal"),
    [
        (CreateWorkflowState.DESIGN, False),
        (CreateWorkflowState.GENERATE, False),
        (CreateWorkflowState.VALIDATE, False),
        (CreateWorkflowState.SUMMARIZE, False),
        (CreateWorkflowState.COMPLETE, True),
        (CreateWorkflowState.EXITED, True),
        (CreateWorkflowState.FAILED, True),
    ],
)
def test_state_terminality(state: CreateWorkflowState, terminal: bool) -> None:
    assert state.is_terminal is terminal


def test_state_members_cover_every_phase_and_outcome() -> None:
    names = {member.name for member in CreateWorkflowState}
    assert names == {
        "DESIGN",
        "GENERATE",
        "VALIDATE",
        "SUMMARIZE",
        "COMPLETE",
        "EXITED",
        "FAILED",
    }


def test_phase_artifact_defaults_are_independent() -> None:
    first = PhaseArtifact(phase="design", kind="design", content="a")
    second = PhaseArtifact(phase="generate", kind="workflow_file", content="b")
    first.metadata["x"] = 1
    assert second.metadata == {}
    assert isinstance(first.created_at, float)


def test_context_add_artifact_is_keyed_by_phase_and_latest_wins() -> None:
    ctx = _ctx()
    ctx.add_artifact(PhaseArtifact(phase="generate", kind="workflow_file", content="first"))
    ctx.add_artifact(PhaseArtifact(phase="generate", kind="workflow_file", content="second"))
    assert list(ctx.artifacts) == ["generate"]
    assert ctx.artifacts["generate"].content == "second"


def test_context_defaults_are_empty() -> None:
    ctx = CreateWorkflowContext(intent="i", run_id="r")
    assert ctx.design == ""
    assert ctx.workflow_name == ""
    assert ctx.generated_path == ""
    assert ctx.repair_cycles == 0
    assert ctx.artifacts == {}
    assert ctx.command_outcomes == []
    assert ctx.shared_memory is None


@pytest.mark.asyncio
async def test_yolo_generation_override_restores_prior_mode_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    manager = ModeManager(app_state=runner._cfg.app_state)
    runner._mode_manager = manager

    async def cancelled_turn(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", cancelled_turn)
    with pytest.raises(asyncio.CancelledError):
        await runner._run_turn(
            "generate",
            tools=[],
            mode="Yolo",
            system_prompt="write the workflow",
            max_turns=1,
            ctx=_ctx(),
        )

    assert manager.active_name == "Safe"
    assert runner._cfg.app_state.active_mode().name == "Safe"


# ── phase_tools: name validation ──────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a", "demo", "my_workflow", "wf2", "a_1_b"])
def test_validate_workflow_name_accepts_slugs(name: str) -> None:
    assert validate_workflow_name(name) is None


@pytest.mark.parametrize(
    "name",
    ["", "  ", "Demo", "my-workflow", "2fast", "_leading", "with space", "trailing!"],
)
def test_validate_workflow_name_rejects_bad_slugs(name: str) -> None:
    assert validate_workflow_name(name) is not None


def test_validate_workflow_name_rejects_non_strings() -> None:
    assert validate_workflow_name(None) is not None
    assert validate_workflow_name(7) is not None


def test_transition_failure_shape() -> None:
    result = _transition_failure("Broke.", "Do this.", approved=False)
    assert result["ok"] is False
    assert result["error"] == "Broke."
    assert result["fix"] == "Do this."
    assert result["message"] == "Broke. Fix: Do this."
    assert result["approved"] is False


# ── phase_tools: design ───────────────────────────────────────────────────────


async def test_design_tools_expose_exactly_the_transition_surface() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_tools(None, event, data, exit_event=asyncio.Event())
    assert set(_by_name(tools)) == {
        "request_design_approval",
        "finalize_design",
        "exit_create_workflow",
    }


async def test_design_tools_omit_exit_when_no_exit_event() -> None:
    tools = make_design_tools(None, asyncio.Event(), {})
    assert set(_by_name(tools)) == {"request_design_approval", "finalize_design"}


async def test_finalize_design_refused_before_approval() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_tools(None, event, data)
    result = await _call(tools, "finalize_design", "the design", "demo")
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "not been approved" in str(result["error"])
    assert not event.is_set()
    assert data == {}


async def test_approval_then_finalize_sets_event_and_data() -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_tools(None, event, data)
    approved = await _call(tools, "request_design_approval", " the design ", "demo")
    assert isinstance(approved, dict)
    assert approved["approved"] is True
    result = await _call(tools, "finalize_design", " the design ", " demo ")
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert event.is_set()
    assert data == {"design": "the design", "workflow_name": "demo"}


@pytest.mark.parametrize(
    ("design", "name"),
    [("", "demo"), ("   ", "demo"), ("the design", ""), ("the design", "Bad-Name")],
)
async def test_request_design_approval_validates_payload(design: str, name: str) -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_tools(None, event, data)
    result = await _call(tools, "request_design_approval", design, name)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["approved"] is False
    assert not event.is_set()


@pytest.mark.parametrize(
    ("design", "name"),
    [("", "demo"), ("the design", "Bad-Name")],
)
async def test_finalize_design_validates_payload(design: str, name: str) -> None:
    event, data = asyncio.Event(), {}
    tools = make_design_tools(None, event, data)
    await _call(tools, "request_design_approval", "the design", "demo")
    result = await _call(tools, "finalize_design", design, name)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert not event.is_set()


async def test_denied_approval_keeps_the_gate_closed() -> None:
    event, data = asyncio.Event(), {}
    approval = SimpleNamespace(
        request_approval=_denying_approval(allowed=False, message="too vague")
    )
    tools = make_design_tools(approval, event, data)  # type: ignore[arg-type]
    denied = await _call(tools, "request_design_approval", "the design", "demo")
    assert isinstance(denied, dict)
    assert denied["approved"] is False
    assert denied["feedback"] == "too vague"
    blocked = await _call(tools, "finalize_design", "the design", "demo")
    assert isinstance(blocked, dict)
    assert blocked["ok"] is False
    assert not event.is_set()


async def test_approval_granted_then_revoked_closes_the_gate_again() -> None:
    event, data = asyncio.Event(), {}
    responses = [
        SimpleNamespace(allowed=True, message=""),
        SimpleNamespace(allowed=False, message="changed my mind"),
    ]

    async def request_approval(_req: object) -> object:
        return responses.pop(0)

    tools = make_design_tools(SimpleNamespace(request_approval=request_approval), event, data)  # type: ignore[arg-type]
    await _call(tools, "request_design_approval", "the design", "demo")
    await _call(tools, "request_design_approval", "the design", "demo")
    blocked = await _call(tools, "finalize_design", "the design", "demo")
    assert isinstance(blocked, dict)
    assert blocked["ok"] is False
    assert not event.is_set()


async def test_approval_service_error_is_reported_not_raised() -> None:
    event, data = asyncio.Event(), {}

    async def boom(_req: object) -> object:
        raise RuntimeError("overlay died")

    tools = make_design_tools(SimpleNamespace(request_approval=boom), event, data)  # type: ignore[arg-type]
    result = await _call(tools, "request_design_approval", "the design", "demo")
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert "RuntimeError" in str(result["error"])
    assert not event.is_set()


async def test_exit_create_workflow_sets_exit_event_and_suggestion() -> None:
    event, data = asyncio.Event(), {}
    exit_event = asyncio.Event()
    tools = make_design_tools(None, event, data, exit_event=exit_event)
    result = await _call(tools, "exit_create_workflow", "  use /workflow code_plan  ")
    assert result == {"accepted": True}
    assert exit_event.is_set()
    assert data["suggestion"] == "use /workflow code_plan"
    assert not event.is_set()


def _denying_approval(*, allowed: bool, message: str) -> object:
    async def request_approval(_req: object) -> object:
        return SimpleNamespace(allowed=allowed, message=message)

    return request_approval


# ── phase_tools: generation ───────────────────────────────────────────────────


async def test_mark_generation_complete_records_summary_and_path() -> None:
    event, data = asyncio.Event(), {}
    tools = make_generation_tools(event, data)
    assert set(_by_name(tools)) == {"mark_generation_complete"}
    result = await _call(tools, "mark_generation_complete", " wrote it ", " a/b.py ")
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert event.is_set()
    assert data == {"summary": "wrote it", "path": "a/b.py"}


@pytest.mark.parametrize(("summary", "path"), [("", "a.py"), ("wrote it", ""), ("  ", "  ")])
async def test_mark_generation_complete_validates_payload(summary: str, path: str) -> None:
    event, data = asyncio.Event(), {}
    tools = make_generation_tools(event, data)
    result = await _call(tools, "mark_generation_complete", summary, path)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert not event.is_set()
    assert data == {}


# ── phase_tools: validation ───────────────────────────────────────────────────


async def test_validation_tools_expose_approve_and_reject() -> None:
    tools = make_validation_tools(asyncio.Event(), {})
    assert set(_by_name(tools)) == {"approve_workflow", "reject_workflow"}


async def test_approve_workflow_records_action_and_summary() -> None:
    event, data = asyncio.Event(), {}
    tools = make_validation_tools(event, data)
    result = await _call(tools, "approve_workflow", " it loads ")
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert event.is_set()
    assert data == {"action": "approve", "summary": "it loads"}


async def test_reject_workflow_records_action_and_reason() -> None:
    event, data = asyncio.Event(), {}
    tools = make_validation_tools(event, data)
    result = await _call(tools, "reject_workflow", " dangling next edge ")
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert event.is_set()
    assert data == {"action": "reject", "reason": "dangling next edge"}


@pytest.mark.parametrize("tool_name", ["approve_workflow", "reject_workflow"])
async def test_validation_tools_reject_empty_payload(tool_name: str) -> None:
    event, data = asyncio.Event(), {}
    tools = make_validation_tools(event, data)
    result = await _call(tools, tool_name, "   ")
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert not event.is_set()
    assert data == {}


# ── inspection_tools ──────────────────────────────────────────────────────────


def test_inspection_tools_are_the_documented_set() -> None:
    tools = make_inspection_tools()
    assert [getattr(tool, "__name__", "") for tool in tools] == [
        "describe_phasespec",
        "list_tool_capabilities",
        "list_agent_roles",
        "describe_cloakbrowser_tools",
        "describe_playwright_tools",
        "describe_runner_pattern",
        "describe_transition_tool_pattern",
        "show_example_workflow",
        "describe_prompt_cache_contract",
        "show_workflow_template",
        "validate_workflow_cache_contract",
    ]


async def test_describe_phasespec_covers_every_live_field() -> None:
    import dataclasses

    result = await _call(make_inspection_tools(), "describe_phasespec")
    assert isinstance(result, dict)
    fields = result["phasespec_fields"]
    assert isinstance(fields, list)
    described = {entry["name"] for entry in fields if isinstance(entry, dict)}
    assert described == {f.name for f in dataclasses.fields(PhaseSpec)}
    assert all(entry["purpose"] for entry in fields if isinstance(entry, dict))
    required = next(entry for entry in fields if entry["name"] == "name")  # type: ignore[index]
    assert required["default"] == "(required)"


async def test_list_tool_capabilities_covers_every_capability() -> None:
    result = await _call(make_inspection_tools(), "list_tool_capabilities")
    assert isinstance(result, dict)
    caps = result["capabilities"]
    assert isinstance(caps, list)
    listed = {entry["value"] for entry in caps if isinstance(entry, dict)}
    assert listed == {cap.value for cap in ToolCapability}
    assert all(entry["description"] for entry in caps if isinstance(entry, dict))


async def test_list_agent_roles_covers_every_phase_role() -> None:
    result = await _call(make_inspection_tools(), "list_agent_roles")
    assert isinstance(result, dict)
    expected = {
        value
        for name, value in vars(PhaseRole).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    assert set(result["agent_types"]) == expected  # type: ignore[arg-type]


async def test_show_example_workflow_defaults_to_the_custom_runner_shape(
    tmp_path: Path,
) -> None:
    result = await _call(make_inspection_tools(), "show_example_workflow")
    assert isinstance(result, dict)
    assert result["style"] == "runner"
    assert result["path"] == ".agenthicc/workflows/release_check"
    assert result["entry_point"] == ".agenthicc/workflows/release_check/runner.py"
    source = result["source"]
    assert isinstance(source, str)

    # The example is the shape the prompts demand: enum state, dataclass context,
    # per-state methods, an explicit driver, resume, and build_runner.
    for required in (
        "class ReleaseState(Enum)",
        "def is_terminal",
        "@dataclasses.dataclass",
        "class ReleaseContext",
        "class ReleaseCheckRunner(CodePlanRunner)",
        "while not state.is_terminal",
        "match state:",
        "async def resume(",
        "asyncio.Event",
        "def build_runner(",
        "run_phase(",
        "checkpoint_context_to_payload",
        "checkpoint_context_from_payload",
        "successful submit_release_plan(plan) call changes phase",
        "transition tools changes phase",
        "from agenthicc.tools.capabilities import tool_control",
        "@tool_control",
    ):
        assert required in source, required

    path = tmp_path / "workflows" / "release_check"
    path.mkdir(parents=True)
    entry_point = path / "runner.py"
    entry_point.write_text(source, encoding="utf-8")
    report = validate_workflow_file(str(path), expected_name="release_check", root=tmp_path)
    assert report.ok, report.render()
    assert report.warnings == (), report.render()
    assert report.phase_names == ("plan", "verify", "report")

    strict_report = validate_workflow_file(
        str(path),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
    )
    assert strict_report.ok, strict_report.render()
    assert strict_report.cache_contract == "contract-native"

    spec = importlib.util.spec_from_file_location("generated_release_check", entry_point)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    memory = object()
    context = module.ReleaseContext(  # type: ignore[attr-defined]
        intent="release it",
        run_id="generated-run",
        state=module.ReleaseState.VERIFY,  # type: ignore[attr-defined]
        phase_iteration=4,
        artifacts={"plan": "checks"},
        shared_memory=memory,
    )
    payload = module.ReleaseCheckWorkflow.checkpoint_context_to_payload(context)  # type: ignore[attr-defined]
    restored = module.ReleaseCheckWorkflow.checkpoint_context_from_payload(payload, memory)  # type: ignore[attr-defined]
    assert restored.state is module.ReleaseState.VERIFY  # type: ignore[attr-defined]
    assert restored.phase_iteration == 4
    assert restored.artifacts == {"plan": "checks"}
    assert restored.shared_memory is memory
    assert "shared_memory" not in payload


async def test_show_example_workflow_declarative_style_is_opt_in(tmp_path: Path) -> None:
    result = await _call(make_inspection_tools(), "show_example_workflow", "declarative")
    assert isinstance(result, dict)
    assert result["style"] == "declarative"
    assert result["path"] == ".agenthicc/workflows/doc_review"
    assert result["entry_point"] == ".agenthicc/workflows/doc_review/runner.py"
    assert "show_example_workflow('runner')" in str(result["note"])
    source = result["source"]
    assert isinstance(source, str)
    assert "BaseWorkflowRunner" not in source
    assert "phase-transition tool is" in source

    path = tmp_path / "workflows" / "doc_review"
    path.mkdir(parents=True)
    (path / "runner.py").write_text(source, encoding="utf-8")
    report = validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    assert report.ok, report.render()
    # Valid, but flagged: two phases with no runner of its own.
    assert any("ships no runner" in warn for warn in report.warnings)


async def test_strict_validation_catches_factory_local_tool_control_mistakes(
    tmp_path: Path,
) -> None:
    result = await _call(make_inspection_tools(), "show_example_workflow")
    assert isinstance(result, dict)
    source = result["source"]
    assert isinstance(source, str)
    broken = source.replace(
        "from agenthicc.tools.capabilities import tool_control",
        "from lauren_ai._tools import tool_control",
    ).replace("@tool_control\n", "@tool_control()\n")
    path = tmp_path / "workflows" / "release_check"
    path.mkdir(parents=True)
    (path / "runner.py").write_text(broken, encoding="utf-8")

    report = validate_workflow_file(
        str(path),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
    )
    assert not report.ok
    assert any("agenthicc.tools.capabilities" in error for error in report.errors)
    assert any("bare decorator" in error for error in report.errors)


async def test_prompt_cache_inspection_describes_contract_and_template() -> None:
    tools = make_inspection_tools()
    contract = await _call(tools, "describe_prompt_cache_contract")
    assert isinstance(contract, dict)
    assert contract["contract_version"] == "agenthicc.prompt-cache.v1"
    assert any("CACHE_CONTRACT" in str(rule) for rule in contract["authoring_rules"])
    assert "ask_user" in contract["required_policy"]
    assert "history_compacted" in contract["invalidation_reasons"]

    template = await _call(tools, "show_workflow_template")
    assert isinstance(template, dict)
    assert template["style"] == "cache-stable-runner"
    assert "stable_system_prompt=CACHE_CONTRACT" in template["required_call"]
    assert "ask_user" in template["source"]


def test_strict_cache_validation_rejects_dynamic_or_guessing_runner(tmp_path: Path) -> None:
    source = """
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

CACHE_CONTRACT = f"dynamic-{object()}"

class BadRunner(CodePlanRunner):
    async def run(self, intent):
        return None
    async def resume(self, context):
        return None

class BadWorkflow(WorkflowPlugin):
    name = "bad_cache"
    description = "bad"
    phases = [PhaseSpec(name="one")]
    @classmethod
    def build_runner(cls, config, mode_manager):
        return BadRunner(config, mode_manager)
"""
    path = tmp_path / "bad_cache.py"
    path.write_text(source, encoding="utf-8")
    report = validate_workflow_file(
        str(path),
        expected_name="bad_cache",
        root=tmp_path,
        strict_cache_contract=True,
    )
    assert not report.ok
    assert report.cache_contract == "invalid"
    assert any("immutable literal policy" in error for error in report.errors)
    assert any("ask_user" in error for error in report.errors)


def test_generated_runner_validation_rejects_resume_restart(tmp_path: Path) -> None:
    source = """
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

CACHE_CONTRACT = "stable ask_user clarifying ambiguous do not guess workspace_access"

class RestartRunner(CodePlanRunner):
    async def run(self, intent):
        return None
    async def resume(self, context):
        return await self.run(context.intent)

class RestartWorkflow(WorkflowPlugin):
    name = "restart_workflow"
    description = "restart test"
    phases = [PhaseSpec(name="one")]
    @classmethod
    def build_runner(cls, config, mode_manager):
        return RestartRunner(config, mode_manager)
    @classmethod
    def checkpoint_context_to_payload(cls, context):
        return {}
    @classmethod
    def checkpoint_context_from_payload(cls, payload, memory=None):
        return None
"""
    path = tmp_path / "restart_workflow.py"
    path.write_text(source, encoding="utf-8")
    report = validate_workflow_file(
        str(path),
        expected_name="restart_workflow",
        root=tmp_path,
        strict_cache_contract=False,
    )
    assert not report.ok
    assert any("silently restarts" in error for error in report.errors)


async def test_describe_runner_pattern_lists_every_required_element() -> None:
    result = await _call(make_inspection_tools(), "describe_runner_pattern")
    assert isinstance(result, dict)
    elements = " ".join(str(item) for item in result["required_elements"])  # type: ignore[union-attr]
    for required in (
        "State(Enum)",
        "dataclass",
        "is_terminal",
        "match state",
        "resume",
        "asyncio.Event",
        "@tool_control",
        "only a successful transition-tool call changes phase",
    ):
        assert required in elements, required
    assert result["when_required"]
    assert "run_phase(" in str(result["turn_api"])
    assert "CodePlanRunner" in " ".join(
        str(item)
        for item in result["reference_implementations"]  # type: ignore[union-attr]
    )


async def test_describe_transition_tool_pattern_returns_canonical_import_and_decorator() -> None:
    result = await _call(make_inspection_tools(), "describe_transition_tool_pattern")
    assert isinstance(result, dict)
    assert "from agenthicc.tools.capabilities import tool_control" in result["canonical_import"]
    assert (
        result["canonical_decorators"] == "@tool_control\n@tool()\nasync def transition(...): ..."
    )
    assert any("never write @tool_control()" in rule for rule in result["decorator_rules"])
    assert any("lauren_ai._tools" in rule for rule in result["decorator_rules"])


# ── validation: path handling ─────────────────────────────────────────────────


def test_validation_rejects_empty_path() -> None:
    report = validate_workflow_file("")
    assert not report.ok
    assert "mark_generation_complete" in report.errors[0]


def test_validation_refuses_paths_outside_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text(_GOOD_SOURCE, encoding="utf-8")
    root = tmp_path / "project"
    root.mkdir()
    report = validate_workflow_file(str(outside), root=root)
    assert not report.ok
    assert "outside the workspace root" in report.errors[0]


def test_validation_reports_missing_file(tmp_path: Path) -> None:
    report = validate_workflow_file("workflows/nope.py", root=tmp_path)
    assert not report.ok
    assert "No file exists" in report.errors[0]


def test_validation_resolves_relative_paths_against_root(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    report = validate_workflow_file("workflows/demo.py", expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert report.path == str((tmp_path / "workflows" / "demo.py").resolve())


def test_validation_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "workflows").mkdir()
    report = validate_workflow_file("workflows", root=tmp_path)
    assert not report.ok
    assert "is a directory" in report.errors[0]


def test_validation_rejects_non_python_suffix(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.txt", _GOOD_SOURCE)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "does not end in .py" in report.errors[0]


def test_validation_rejects_underscore_prefixed_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "_demo.py", _GOOD_SOURCE)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "starts with an underscore" in report.errors[0]


def test_validation_rejects_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", "   \n")
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "is empty" in report.errors[0]


# ── validation: source handling ───────────────────────────────────────────────


def test_validation_reports_syntax_error_with_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", "def broken(:\n    pass\n")
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "syntax error on line 1" in report.errors[0]


def test_validation_reports_import_failure_with_exception_type(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", "raise ValueError('nope')\n")
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "ValueError: nope" in report.errors[0]


def test_validation_reports_sys_exit_as_import_failure(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", "import sys\nsys.exit(3)\n")
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "SystemExit" in report.errors[0]


def test_validation_requires_a_workflow_plugin_subclass(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", "class NotAPlugin:\n    pass\n")
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "no WorkflowPlugin subclass" in report.errors[0]


def test_validation_ignores_plugins_with_empty_names(tmp_path: Path) -> None:
    source = (
        "from agenthicc.workflows.plugin import WorkflowPlugin\n\n\n"
        "class Anonymous(WorkflowPlugin):\n    pass\n"
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert "no WorkflowPlugin subclass" in report.errors[0]


def test_validation_accepts_a_correct_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", _RUNNER_SOURCE)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok
    assert report.errors == ()
    assert report.warnings == ()
    assert report.plugin_names == ("demo",)
    assert report.phase_names == ("one", "two")


def test_validation_rejects_a_custom_runner_without_checkpoint_codecs(tmp_path: Path) -> None:
    source = _RUNNER_SOURCE.split("    @classmethod\n    def checkpoint_context_to_payload", 1)[0]
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert not report.ok
    assert any("checkpoint codec" in error for error in report.errors)
    assert any("checkpoint_context_to_payload()" in error for error in report.errors)
    assert any("checkpoint_context_from_payload()" in error for error in report.errors)


def test_validation_rejects_a_custom_runner_with_only_one_codec(tmp_path: Path) -> None:
    source = _RUNNER_SOURCE.replace(
        "    @classmethod\n    def checkpoint_context_from_payload(cls, payload, memory=None):\n"
        '        return payload.get("context", "")\n',
        "",
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert not report.ok
    assert any("checkpoint_context_from_payload()" in error for error in report.errors)


# ── validation: the workflow's own runner ─────────────────────────────────────


def test_validation_warns_when_a_multi_phase_workflow_ships_no_runner(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", _GOOD_SOURCE)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert any("ships no runner" in warn for warn in report.warnings)
    assert any("2 phases" in warn for warn in report.warnings)


def test_validation_stays_quiet_for_a_single_phase_workflow(tmp_path: Path) -> None:
    source = _GOOD_SOURCE.replace(
        '        PhaseSpec(name="one", next="two"),\n        PhaseSpec(name="two", on_reject="one"),\n',
        '        PhaseSpec(name="one"),\n',
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert not any("ships no runner" in warn for warn in report.warnings)


def test_validation_warns_when_build_runner_returns_a_foreign_runner(tmp_path: Path) -> None:
    source = _GOOD_SOURCE + (
        "\n    @classmethod\n"
        "    def build_runner(cls, config, mode_manager):\n"
        "        raise NotImplementedError\n"
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert any("defines no BaseWorkflowRunner subclass" in warn for warn in report.warnings)


def test_validation_rejects_an_abstract_runner(tmp_path: Path) -> None:
    source = _RUNNER_SOURCE.replace(
        '    async def resume(self, context: object) -> str:\n        return ""\n', ""
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert not report.ok
    assert any("is abstract" in err and "resume" in err for err in report.errors)


def test_validation_accepts_a_runner_that_extends_code_plan(tmp_path: Path) -> None:
    source = '''\
"""demo workflow reusing the code_plan turn helper."""

from __future__ import annotations

from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DemoRunner(CodePlanRunner):
    workflow_name = "demo"
    total_phases = 1

    async def run(self, intent: str) -> str:
        await self.run_phase(intent=intent, text=intent, system_prompt="Do it.")
        return intent

    async def resume(self, context: object) -> str:
        return ""


class Demo(WorkflowPlugin):
    name = "demo"
    description = "A demo workflow."
    mode_bindings = []
    phases = [PhaseSpec(name="one", next="two"), PhaseSpec(name="two")]

    @classmethod
    def build_runner(cls, config, mode_manager):
        return DemoRunner(config, mode_manager)

    @classmethod
    def checkpoint_context_to_payload(cls, context):
        return {"intent": getattr(context, "intent", "")}

    @classmethod
    def checkpoint_context_from_payload(cls, payload, memory=None):
        return payload
'''
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert report.warnings == (), report.render()


# ── validation: plugin contract ───────────────────────────────────────────────


def test_validation_flags_name_mismatch_against_approved_design(tmp_path: Path) -> None:
    path = _write(tmp_path, "other.py", _GOOD_SOURCE)
    report = validate_workflow_file(str(path), expected_name="expected", root=tmp_path)
    assert not report.ok
    assert any("approved workflow name is 'expected'" in err for err in report.errors)


def test_validation_warns_when_filename_does_not_match_workflow(tmp_path: Path) -> None:
    path = _write(tmp_path, "elsewhere.py", _GOOD_SOURCE)
    report = validate_workflow_file(str(path), expected_name="demo", root=tmp_path)
    assert report.ok, report.render()
    assert any("conventional filename" in warn for warn in report.warnings)


@pytest.mark.parametrize("reserved", ["code_plan", "create_workflow"])
def test_validation_rejects_builtin_names(tmp_path: Path, reserved: str) -> None:
    path = _write(tmp_path, f"{reserved}.py", _GOOD_SOURCE.replace('"demo"', f'"{reserved}"'))
    report = validate_workflow_file(str(path), expected_name=reserved, root=tmp_path)
    assert not report.ok
    assert any("builtin workflow" in err for err in report.errors)


def test_validation_requires_a_description(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", _GOOD_SOURCE.replace('"A demo workflow."', '""'))
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("description is empty" in err for err in report.errors)


def test_validation_requires_list_mode_bindings(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "demo.py", _GOOD_SOURCE.replace("mode_bindings = []", 'mode_bindings = "Plan"')
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("mode_bindings must be a list" in err for err in report.errors)


def test_validation_reports_build_params_failure(tmp_path: Path) -> None:
    source = _GOOD_SOURCE + (
        "\n    @classmethod\n    def build_params(cls, source):\n        raise KeyError('model')\n"
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("build_params({}) raised KeyError" in err for err in report.errors)


def test_validation_reports_build_params_wrong_return_type(tmp_path: Path) -> None:
    source = _GOOD_SOURCE + (
        "\n    @classmethod\n    def build_params(cls, source):\n        return {}\n"
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("not a WorkflowParams" in err for err in report.errors)


# ── validation: phase graph ───────────────────────────────────────────────────


def test_validation_rejects_dangling_transition_edges(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", _GOOD_SOURCE.replace('next="two"', 'next="nowhere"'))
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("next='nowhere'" in err for err in report.errors)


def test_validation_rejects_duplicate_phase_names(tmp_path: Path) -> None:
    path = _write(tmp_path, "demo.py", _GOOD_SOURCE.replace('name="two"', 'name="one"'))
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("repeats the phase name" in err for err in report.errors)


def test_validation_rejects_non_phasespec_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "demo.py", _GOOD_SOURCE.replace('PhaseSpec(name="two", on_reject="one")', '"two"')
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("not a PhaseSpec" in err for err in report.errors)


def test_validation_rejects_non_positive_max_turns(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.py",
        _GOOD_SOURCE.replace('name="one", next="two"', 'name="one", next="two", max_turns=0'),
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("max_turns=0" in err for err in report.errors)


def test_validation_rejects_non_list_phases(tmp_path: Path) -> None:
    source = _GOOD_SOURCE.split("    phases = [")[0] + '    phases = "one"\n'
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("must be a list of PhaseSpec" in err for err in report.errors)


def test_validation_rejects_empty_phases_with_default_runner(tmp_path: Path) -> None:
    source = _GOOD_SOURCE.split("    phases = [")[0] + "    phases = []\n"
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert not report.ok
    assert any("phases is empty" in err for err in report.errors)


def test_validation_only_warns_about_empty_phases_with_a_custom_runner(tmp_path: Path) -> None:
    source = _GOOD_SOURCE.split("    phases = [")[0] + (
        "    phases = []\n\n"
        "    @classmethod\n"
        "    def build_runner(cls, config, mode_manager):\n"
        "        raise NotImplementedError\n"
    )
    path = _write(tmp_path, "demo.py", source)
    report = validate_workflow_file(str(path), root=tmp_path)
    assert report.ok, report.render()
    assert any("custom runner drives every phase" in warn for warn in report.warnings)


def test_validation_warns_about_unknown_output_schema(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.py",
        _GOOD_SOURCE.replace(
            'name="one", next="two"', 'name="one", next="two", output_schema="wat"'
        ),
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert report.ok, report.render()
    assert any("output_schema='wat'" in warn for warn in report.warnings)


def test_validation_warns_about_unknown_agent_type(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.py",
        _GOOD_SOURCE.replace(
            'name="one", next="two"', 'name="one", next="two", agent_type="wizard"'
        ),
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert report.ok, report.render()
    assert any("agent_type='wizard'" in warn for warn in report.warnings)


def test_validation_warns_about_unreachable_phases(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "demo.py",
        _GOOD_SOURCE.replace('PhaseSpec(name="one", next="two")', 'PhaseSpec(name="one")'),
    )
    report = validate_workflow_file(str(path), root=tmp_path)
    assert report.ok, report.render()
    assert any("unreachable" in warn for warn in report.warnings)


# ── validation: report rendering ──────────────────────────────────────────────


def test_report_render_pass_mentions_clean_import() -> None:
    text = ValidationReport(path="/x/demo.py", ok=True, phase_names=("a", "b")).render()
    assert "result: PASS" in text
    assert "a → b" in text
    assert "imports cleanly" in text


def test_report_render_fail_numbers_errors_and_warnings() -> None:
    text = ValidationReport(
        path="/x/demo.py",
        ok=False,
        errors=("first bad thing", "second bad thing"),
        warnings=("a nit",),
        plugin_names=("demo",),
    ).render()
    assert "result: FAIL" in text
    assert "errors (2)" in text
    assert "1. first bad thing" in text
    assert "2. second bad thing" in text
    assert "warnings (1)" in text
    assert "workflows found: demo" in text


def test_report_render_handles_unresolved_path() -> None:
    assert "(unresolved)" in ValidationReport(path="", ok=False).render()


# ── definition ────────────────────────────────────────────────────────────────


def test_plugin_identity_and_phase_order() -> None:
    assert CreateWorkflow.name == "create_workflow"
    assert CreateWorkflow.description
    assert CreateWorkflow.mode_bindings == []
    assert CreateWorkflow.phase_names() == list(_PHASE_NAMES)


def test_plugin_phase_graph_matches_the_runner_state_machine() -> None:
    phases = {phase.name: phase for phase in CreateWorkflow.phases}
    assert phases["design"].next == "generate"
    assert phases["design"].require_plan_finalization is True
    assert phases["generate"].next == "validate"
    assert phases["generate"].mode_override == "Yolo"
    assert phases["generate"].require_explicit_completion is True
    assert phases["validate"].next == "summarize"
    assert phases["validate"].on_reject == "generate"
    assert phases["validate"].require_explicit_review is True
    assert phases["summarize"].next is None
    assert phases["summarize"].output_schema == "free_text"


def test_every_phase_has_a_prompt_and_positive_turn_budget() -> None:
    for phase in CreateWorkflow.phases:
        assert phase.system_prompt_override.strip(), phase.name
        assert phase.max_turns >= 1, phase.name


def test_plugin_phase_indexes_match_the_runner() -> None:
    assert _PHASE_INDEX == {name: i for i, name in enumerate(CreateWorkflow.phase_names())}
    assert len(CreateWorkflow.phases) == CreateWorkflowRunner.total_phases
    assert CreateWorkflowRunner.workflow_name == CreateWorkflow.name


def test_build_params_reads_strings_and_ignores_other_types() -> None:
    params = CreateWorkflow.build_params(
        {
            "design_model": "d",
            "generate_model": "g",
            "validate_model": 7,
            "summary_model": None,
            "unrelated": "x",
        }
    )
    assert isinstance(params, CreateWorkflowParams)
    assert params.get_phase_models() == {
        "design": "d",
        "generate": "g",
        "validate": "",
        "summarize": "",
    }


def test_build_params_defaults_to_empty_overrides() -> None:
    params = CreateWorkflow.build_params({})
    assert isinstance(params, WorkflowParams)
    assert set(params.get_phase_models().values()) == {""}


def test_build_runner_returns_the_state_machine_runner() -> None:
    runner = _runner()
    built = CreateWorkflow.build_runner(runner._cfg, None)
    assert isinstance(built, CreateWorkflowRunner)
    assert built.workflow_name == "create_workflow"
    assert built.total_phases == 4


# ── runner: helpers ───────────────────────────────────────────────────────────


def test_runner_reads_authoring_budgets_from_config() -> None:
    runner = _runner(max_attempts=7, max_turns=11)
    assert runner._max_attempts == 7
    assert runner._max_phase_turns == 11
    assert runner._max_repair_cycles == 7


def test_runner_clamps_non_positive_budgets() -> None:
    runner = _runner(max_attempts=0, max_turns=-5)
    assert runner._max_attempts == 1
    assert runner._max_phase_turns == 1


def test_phase_model_prefers_params_then_class_attribute() -> None:
    runner = _runner()
    assert runner._phase_model("generate") == ""

    runner.generate_model = "class-attr"
    assert runner._phase_model("generate") == "class-attr"

    runner._cfg = __import__("dataclasses").replace(
        runner._cfg, params=CreateWorkflowParams(generate_model="from-toml")
    )
    assert runner._phase_model("generate") == "from-toml"


def test_phase_model_maps_summarize_onto_summary_model() -> None:
    runner = _runner()
    runner.summary_model = "cheap"
    assert runner._phase_model("summarize") == "cheap"
    assert runner._phase_model("unknown-phase") == ""


def test_target_path_uses_the_project_workflow_directory() -> None:
    runner = _runner()
    assert runner._target_path("demo") == ".agenthicc/workflows/demo"
    assert runner._target_path("") == ".agenthicc/workflows/my_workflow"


def test_workspace_root_is_the_current_directory() -> None:
    assert _runner()._workspace_root() == Path.cwd()


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (CreateWorkflowState.COMPLETE, "complete"),
        (CreateWorkflowState.EXITED, "exited"),
        (CreateWorkflowState.FAILED, "failed"),
    ],
)
def test_final_status_mapping(state: CreateWorkflowState, status: str) -> None:
    assert CreateWorkflowRunner._final_status(state) == status


def test_route_repair_counts_cycles_then_fails() -> None:
    runner = _runner(max_attempts=2)
    ctx = _ctx()
    assert runner._route_repair(ctx, " fix the edge ") is CreateWorkflowState.GENERATE
    assert ctx.repair_cycles == 1
    assert ctx.rejection_reason == "fix the edge"
    assert runner._route_repair(ctx, "again") is CreateWorkflowState.GENERATE
    assert runner._route_repair(ctx, "and again") is CreateWorkflowState.FAILED
    assert "limit 2" in ctx.fail_reason


async def test_dispatch_returns_terminal_states_unchanged() -> None:
    runner = _runner()
    ctx = _ctx()
    for state in (
        CreateWorkflowState.COMPLETE,
        CreateWorkflowState.EXITED,
        CreateWorkflowState.FAILED,
    ):
        assert await runner._dispatch(state, ctx) is state


def test_set_phase_forwards_everything_to_app_state() -> None:
    runner = _runner()
    ctx = _ctx()
    runner._set_phase("validate", 2, ctx)
    update = runner._cfg.app_state.update_workflow_phase
    assert isinstance(update, MagicMock)
    update.assert_called_once_with(
        workflow_name="create_workflow",
        phase_name="validate",
        phase_index=2,
        total_phases=4,
        run_id="run",
        intent=ctx.intent,
        model_id="transport",
    )


def test_base_tools_drops_blocked_capabilities_and_adds_memory_tools() -> None:
    import dataclasses

    from lauren_ai._tools import tool as _tool

    from agenthicc.tools.capabilities import tool_read, tool_write
    from agenthicc.tui.runtime.mode_manager import RuntimeMode

    @tool_write
    @_tool()
    async def writer(path: str) -> str:
        """A write-capable tool."""
        return path

    @tool_read
    @_tool()
    async def reader(path: str) -> str:
        """A read-only tool."""
        return path

    runner = _runner()
    runner._cfg = dataclasses.replace(runner._cfg, plugin_tools=[writer, reader])
    runner._cfg.app_state.active_mode.set(
        RuntimeMode(name="Locked", blocked_capabilities=frozenset({ToolCapability.WRITE}))
    )

    names = {getattr(tool, "__name__", "") for tool in runner._base_tools()}
    assert "reader" in names
    assert "writer" not in names
    assert any("memory" in name for name in names)


def test_base_tools_keeps_everything_when_nothing_is_blocked() -> None:
    import dataclasses

    from lauren_ai._tools import tool as _tool

    from agenthicc.tools.capabilities import tool_write

    @tool_write
    @_tool()
    async def writer(path: str) -> str:
        """A write-capable tool."""
        return path

    runner = _runner()
    runner._cfg = dataclasses.replace(runner._cfg, plugin_tools=[writer])
    names = {getattr(tool, "__name__", "") for tool in runner._base_tools()}
    assert "writer" in names


def test_base_tools_survives_a_broken_mcp_registry() -> None:
    import dataclasses

    class Broken:
        def all_tools(self) -> list[object]:
            raise RuntimeError("mcp server is down")

    runner = _runner()
    runner._cfg = dataclasses.replace(runner._cfg, mcp_registry=Broken())  # type: ignore[arg-type]
    assert runner._base_tools()  # memory tools still returned


# ── runner: design phase ──────────────────────────────────────────────────────


async def _design_turn(_text: str, **kwargs: object) -> None:
    tools = kwargs["tools"]
    await _call(tools, "request_design_approval", "the design", "demo")
    await _call(tools, "finalize_design", "the design", "demo")


async def test_design_transitions_to_generate_and_records_the_artifact() -> None:
    runner = _runner()
    ctx = _ctx()
    runner._run_turn = _design_turn  # type: ignore[method-assign]

    assert await runner._design(ctx) is CreateWorkflowState.GENERATE
    assert ctx.design == "the design"
    assert ctx.workflow_name == "demo"
    artifact = ctx.artifacts["design"]
    assert artifact.kind == "design"
    assert artifact.content == "the design"
    assert artifact.metadata["workflow_name"] == "demo"


async def test_design_exits_when_the_agent_calls_exit() -> None:
    runner = _runner()
    ctx = _ctx()

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "exit_create_workflow", "just ask a question")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._design(ctx) is CreateWorkflowState.EXITED
    assert ctx.suggestion == "just ask a question"
    assert ctx.artifacts["design"].kind == "exit"


async def test_design_retries_then_fails_without_a_tool_call() -> None:
    runner = _runner(max_attempts=3)
    ctx = _ctx()
    seen: list[str] = []

    async def turn(text: str, **_kwargs: object) -> None:
        seen.append(text)

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._design(ctx) is CreateWorkflowState.FAILED
    assert len(seen) == 3
    assert seen[0] == ctx.intent
    assert seen[1] == seen[2] != ctx.intent
    assert "exhausted 3 attempts" in ctx.fail_reason
    assert "design" not in ctx.artifacts


async def test_design_finalization_on_a_later_attempt_still_advances() -> None:
    runner = _runner(max_attempts=3)
    ctx = _ctx()
    attempts = {"n": 0}

    async def turn(_text: str, **kwargs: object) -> None:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return
        await _design_turn(_text, **kwargs)

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._design(ctx) is CreateWorkflowState.GENERATE
    assert ctx.artifacts["design"].metadata["attempts"] == 2


async def test_design_fails_fast_on_a_permanent_turn_error() -> None:
    runner = _runner(max_attempts=5)
    ctx = _ctx()
    calls = {"n": 0}

    async def turn(_text: str, **_kwargs: object) -> None:
        calls["n"] += 1
        raise RuntimeError("transport is gone")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._design(ctx) is CreateWorkflowState.FAILED
    assert calls["n"] == 1
    assert ctx.fail_reason == "RuntimeError: transport is gone"


async def test_design_propagates_cancellation() -> None:
    runner = _runner()

    async def turn(_text: str, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    runner._run_turn = turn  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await runner._design(_ctx())


async def test_design_injects_inspection_and_question_tools() -> None:
    runner = _runner()
    captured: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        captured.extend(_by_name(kwargs["tools"]))
        await _design_turn(_text, **kwargs)

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._design(_ctx())
    assert {
        "describe_phasespec",
        "list_tool_capabilities",
        "list_agent_roles",
        "describe_runner_pattern",
        "show_example_workflow",
        "request_design_approval",
        "finalize_design",
        "exit_create_workflow",
        "ask_user",
    } <= set(captured)


@pytest.mark.asyncio
async def test_run_turn_appends_the_design_transition_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    event = asyncio.Event()
    data: dict[str, object] = {}
    tools = make_design_tools(None, event, data, asyncio.Event())
    captured: dict[str, object] = {}

    async def fake_turn(_text: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("agenthicc.runners.agent_turn._run_agent_turn", fake_turn)
    await runner._run_turn(
        "design a workflow",
        tools=tools,
        mode=None,
        system_prompt="Design the workflow.",
        max_turns=2,
        ctx=_ctx(),
        phase_name="design",
    )

    suffix = captured["system_prompt_suffix"]
    assert isinstance(suffix, str)
    assert "[PHASE TRANSITION TOOLS]" in suffix
    assert "`request_design_approval`" in suffix
    assert "`finalize_design`" in suffix
    assert "`exit_create_workflow`" in suffix
    assert "only after a transition tool call succeeds" in suffix
    assert "[REQUIREMENTS CLARIFICATION]" in suffix
    assert "multiple focused questions" in suffix


async def test_design_prompt_requires_the_workflow_to_ship_its_own_runner() -> None:
    runner = _runner()
    prompts: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        prompt = kwargs["system_prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        await _design_turn(_text, **kwargs)

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._design(_ctx())
    prompt = prompts[0]
    assert "state enum" in prompt
    assert "context dataclass" in prompt
    assert "describe_runner_pattern()" in prompt
    assert "while not state.is_terminal" in prompt
    assert "match state" in prompt
    assert "resume(context)" in prompt
    assert "run_phase(" in prompt
    assert "build_runner()" in prompt
    assert "checkpoint payload fields" in prompt
    assert "memory reattachment" in prompt
    assert "exact transition tool(s)" in prompt
    assert "prose such as 'done' never advances" in prompt


async def test_generate_prompt_requires_writing_the_runner_not_a_stub() -> None:
    runner = _runner()
    ctx = _ctx()
    ctx.design = "the design"
    ctx.workflow_name = "demo"
    prompts: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        prompt = kwargs["system_prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "x/demo")

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._generate(ctx)
    prompt = prompts[0]
    assert "runner.py" in prompt
    assert "workflow directory" in prompt
    assert "state enum" in prompt
    assert "build_runner()" in prompt
    assert "checkpoint_context_to_payload" in prompt
    assert "checkpoint_context_from_payload" in prompt
    assert "session_memory" in prompt
    assert "stub" in prompt
    assert "show_example_workflow()" in prompt
    assert "@tool_control" in prompt
    assert "only a successful transition-tool call changes phase" in prompt


async def test_generate_prompt_tells_the_agent_to_write_in_chunks() -> None:
    """A whole runner in one tool call can exceed the response limit and vanish."""
    runner = _runner()
    ctx = _ctx()
    ctx.design = "the design"
    ctx.workflow_name = "demo"
    prompts: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        prompt = kwargs["system_prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "x/demo")

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._generate(ctx)
    prompt = prompts[0]
    assert "WRITE THE PACKAGE IN CHUNKS" in prompt
    assert "write_file(path/runner.py, content)" in prompt
    assert "append_file(path/runner.py, content)" in prompt
    assert "read_file(path/runner.py)" in prompt
    assert "response limit" in prompt
    assert "Never re-write runner.py from the start after appending" in prompt


async def test_generate_reminder_tells_the_agent_to_resume_not_restart() -> None:
    runner = _runner(max_attempts=2)
    ctx = _ctx()
    ctx.design = "the design"
    ctx.workflow_name = "demo"
    texts: list[str] = []

    async def turn(text: str, **_kwargs: object) -> None:
        texts.append(text)

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._generate(ctx)
    reminder = texts[1]
    assert "response was too long and was discarded" in reminder
    assert "append_file(path/runner.py, content)" in reminder
    assert "Do not start the file over" in reminder


def test_definition_phase_prompts_name_the_runner_requirement() -> None:
    phases = {phase.name: phase for phase in CreateWorkflow.phases}
    assert "state-machine runner" in phases["design"].system_prompt_override
    assert "describe_runner_pattern()" in phases["design"].system_prompt_override
    assert "build_runner()" in phases["generate"].system_prompt_override
    assert "No stubs" in phases["generate"].system_prompt_override


async def test_design_prompt_carries_the_intent_and_authoring_guide() -> None:
    runner = _runner()
    prompts: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        prompt = kwargs["system_prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        await _design_turn(_text, **kwargs)

    runner._run_turn = turn  # type: ignore[method-assign]
    ctx = _ctx("build me a doc review workflow")
    await runner._design(ctx)
    assert "build me a doc review workflow" in prompts[0]
    assert ".agenthicc/workflows/<name>/" in prompts[0]
    assert "describe_phasespec()" in prompts[0]


# ── runner: generate phase ────────────────────────────────────────────────────


def _generated_ctx(path: str = ".agenthicc/workflows/demo") -> CreateWorkflowContext:
    ctx = _ctx()
    ctx.design = "the design"
    ctx.workflow_name = "demo"
    ctx.generated_path = path
    return ctx


async def test_generate_transitions_to_validate_and_records_the_artifact() -> None:
    runner = _runner()
    ctx = _ctx()
    ctx.design = "the design"
    ctx.workflow_name = "demo"

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "x/demo")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._generate(ctx) is CreateWorkflowState.VALIDATE
    assert ctx.generated_path == "x/demo"
    assert ctx.generation_summary == "wrote it"
    artifact = ctx.artifacts["generate"]
    assert artifact.kind == "workflow_file"
    assert artifact.metadata["path"] == "x/demo"


async def test_generate_runs_in_yolo_mode_with_the_design_in_the_prompt() -> None:
    runner = _runner()
    ctx = _ctx()
    ctx.design = "phases: draft then review"
    ctx.workflow_name = "demo"
    seen: dict[str, object] = {}

    async def turn(text: str, **kwargs: object) -> None:
        seen.update(kwargs)
        seen["text"] = text
        await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "x/demo")

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._generate(ctx)
    assert seen["mode"] == "Yolo"
    prompt = seen["system_prompt"]
    assert isinstance(prompt, str)
    assert "phases: draft then review" in prompt
    assert ".agenthicc/workflows/demo" in prompt
    assert ".agenthicc/workflows/demo" in str(seen["text"])


async def test_generate_repair_prompt_includes_the_rejection_and_report() -> None:
    runner = _runner()
    ctx = _generated_ctx()
    ctx.rejection_reason = "dangling next edge"
    ctx.validation_report = "[DETERMINISTIC VALIDATION REPORT]\nresult: FAIL"
    seen: dict[str, object] = {}

    async def turn(text: str, **kwargs: object) -> None:
        seen.update(kwargs)
        seen["text"] = text
        await _call(kwargs["tools"], "mark_generation_complete", "fixed it", ctx.generated_path)

    runner._run_turn = turn  # type: ignore[method-assign]
    await runner._generate(ctx)
    prompt = seen["system_prompt"]
    assert isinstance(prompt, str)
    assert "dangling next edge" in prompt
    assert "result: FAIL" in prompt
    assert "dangling next edge" in str(seen["text"])


async def test_generate_retries_then_fails_without_completion() -> None:
    runner = _runner(max_attempts=2)
    ctx = _ctx()
    seen: list[str] = []

    async def turn(text: str, **_kwargs: object) -> None:
        seen.append(text)

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._generate(ctx) is CreateWorkflowState.FAILED
    assert len(seen) == 2
    assert "exhausted 2 attempts" in ctx.fail_reason
    assert "generate" not in ctx.artifacts


async def test_generate_fails_fast_on_a_permanent_turn_error() -> None:
    runner = _runner()
    ctx = _ctx()

    async def turn(_text: str, **_kwargs: object) -> None:
        raise OSError("disk full")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._generate(ctx) is CreateWorkflowState.FAILED
    assert ctx.fail_reason == "OSError: disk full"


# ── runner: validate phase ────────────────────────────────────────────────────


def _validating_runner(tmp_path: Path, *, max_attempts: int = 3) -> CreateWorkflowRunner:
    runner = _runner(max_attempts=max_attempts)
    runner._workspace_root = lambda: tmp_path  # type: ignore[method-assign]
    return runner


async def test_validate_approves_a_loadable_workflow(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/demo.py")

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "approve_workflow", "imports and matches the design")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.SUMMARIZE
    assert ctx.validation_summary == "imports and matches the design"
    assert ctx.rejection_reason == ""
    artifact = ctx.artifacts["validate"]
    assert artifact.kind == "validation_report"
    assert artifact.metadata["ok"] is True
    assert artifact.metadata["phase_names"] == ["one", "two"]


async def test_validate_rejection_routes_back_to_generate(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/demo.py")

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "reject_workflow", "the review phase prompt is wrong")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.GENERATE
    assert ctx.rejection_reason == "the review phase prompt is wrong"
    assert ctx.repair_cycles == 1


async def test_validate_overrides_an_approval_that_contradicts_the_report(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", "class Nope:\n    pass\n")
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/demo.py")

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "approve_workflow", "looks great to me")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.GENERATE
    assert ctx.validation_summary == ""
    assert "overridden" in ctx.rejection_reason
    assert "no WorkflowPlugin subclass" in ctx.rejection_reason
    assert ctx.artifacts["validate"].metadata["ok"] is False


async def test_validate_rejection_appends_deterministic_errors(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", "class Nope:\n    pass\n")
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/demo.py")

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "reject_workflow", "it does not import")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.GENERATE
    assert ctx.rejection_reason.startswith("it does not import")
    assert "no WorkflowPlugin subclass" in ctx.rejection_reason


async def test_validate_fails_when_the_repair_budget_is_exhausted(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path, max_attempts=1)
    ctx = _generated_ctx("workflows/demo.py")
    ctx.repair_cycles = 1

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "reject_workflow", "still wrong")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.FAILED
    assert "limit 1" in ctx.fail_reason


async def test_validate_retries_then_fails_without_a_verdict(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path, max_attempts=2)
    ctx = _generated_ctx("workflows/demo.py")
    seen: list[str] = []

    async def turn(text: str, **_kwargs: object) -> None:
        seen.append(text)

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.FAILED
    assert len(seen) == 2
    assert "exhausted 2 attempts" in ctx.fail_reason


async def test_validate_reports_a_missing_file_to_the_agent(tmp_path: Path) -> None:
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/never_written.py")
    prompts: list[str] = []

    async def turn(_text: str, **kwargs: object) -> None:
        prompt = kwargs["system_prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        await _call(kwargs["tools"], "reject_workflow", "no file")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.GENERATE
    assert "No file exists" in prompts[0]
    assert "result: FAIL" in prompts[0]


async def test_validate_fails_fast_on_a_permanent_turn_error(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path)
    ctx = _generated_ctx("workflows/demo.py")

    async def turn(_text: str, **_kwargs: object) -> None:
        raise RuntimeError("model refused")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._validate(ctx) is CreateWorkflowState.FAILED
    assert ctx.fail_reason == "RuntimeError: model refused"


# ── runner: summarize phase ───────────────────────────────────────────────────


async def test_summarize_completes_and_records_the_artifact() -> None:
    runner = _runner()
    ctx = _generated_ctx()
    ctx.generation_summary = "wrote demo.py"
    ctx.validation_summary = "imports cleanly"
    seen: dict[str, object] = {}

    async def turn(text: str, **kwargs: object) -> None:
        seen["text"] = text
        seen.update(kwargs)

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._summarize(ctx) is CreateWorkflowState.COMPLETE
    assert "demo" in str(seen["text"])
    assert "imports cleanly" in str(seen["text"])
    artifact = ctx.artifacts["summarize"]
    assert artifact.kind == "summary"
    assert artifact.content == "imports cleanly"


async def test_summarize_survives_a_turn_error() -> None:
    runner = _runner()
    ctx = _generated_ctx()

    async def turn(_text: str, **_kwargs: object) -> None:
        raise RuntimeError("summary model down")

    runner._run_turn = turn  # type: ignore[method-assign]
    assert await runner._summarize(ctx) is CreateWorkflowState.COMPLETE
    assert "summarize" in ctx.artifacts


async def test_summarize_propagates_cancellation() -> None:
    runner = _runner()

    async def turn(_text: str, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    runner._run_turn = turn  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await runner._summarize(_generated_ctx())


# ── runner: outer loop ────────────────────────────────────────────────────────


async def _happy_turn(_text: str, **kwargs: object) -> None:
    """Drive whichever phase's transition tool is present in *tools*."""
    tools = _by_name(kwargs["tools"])
    if "finalize_design" in tools:
        await _call(kwargs["tools"], "request_design_approval", "the design", "demo")
        await _call(kwargs["tools"], "finalize_design", "the design", "demo")
    elif "mark_generation_complete" in tools:
        await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "workflows/demo.py")
    elif "approve_workflow" in tools:
        await _call(kwargs["tools"], "approve_workflow", "it loads")


async def test_run_walks_every_phase_and_completes(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path)
    runner._run_turn = _happy_turn  # type: ignore[method-assign]

    ctx = await runner.run("author a demo workflow")

    assert isinstance(ctx, CreateWorkflowContext)
    assert ctx.workflow_name == "demo"
    assert ctx.design == "the design"
    assert ctx.generation_summary == "wrote it"
    assert ctx.validation_summary == "it loads"
    assert ctx.fail_reason == ""
    assert set(ctx.artifacts) == {"design", "generate", "validate", "summarize"}
    assert runner._cfg.app_state.workflow_run().status == "complete"
    assert runner._cfg.app_state.workflow_run().current_phase is None


async def test_run_emits_the_expected_event_sequence(tmp_path: Path) -> None:
    _write(tmp_path, "demo.py", _GOOD_SOURCE)
    runner = _validating_runner(tmp_path)
    runner._run_turn = _happy_turn  # type: ignore[method-assign]

    emitted: list[tuple[str, dict[str, object]]] = []

    async def emit(event: object) -> None:
        emitted.append((event.event_type, dict(event.payload)))  # type: ignore[attr-defined]

    runner._cfg = __import__("dataclasses").replace(
        runner._cfg, processor=SimpleNamespace(emit=emit)
    )

    await runner.run("author a demo workflow")

    types = [name for name, _payload in emitted]
    assert types[0] == "WorkflowRunStarted"
    assert types[-1] == "WorkflowRunCompleted"
    started = [p for n, p in emitted if n == "WorkflowPhaseStarted"]
    assert [p["phase_name"] for p in started] == list(_PHASE_NAMES)
    completed = [p for n, p in emitted if n == "WorkflowPhaseCompleted"]
    assert [p["edge_label"] for p in completed] == ["generate", "validate", "summarize", None]
    assert emitted[0][1]["phase_names"] == list(_PHASE_NAMES)
    assert emitted[-1][1]["status"] == "complete"


async def test_run_records_the_exited_status() -> None:
    runner = _runner()

    async def turn(_text: str, **kwargs: object) -> None:
        await _call(kwargs["tools"], "exit_create_workflow", "ask a question instead")

    runner._run_turn = turn  # type: ignore[method-assign]
    ctx = await runner.run("what is a workflow?")
    assert ctx.suggestion == "ask a question instead"
    assert runner._cfg.app_state.workflow_run().status == "exited"


async def test_run_records_failure_and_reports_it_to_the_conversation() -> None:
    runner = _runner(max_attempts=1)

    async def turn(_text: str, **_kwargs: object) -> None:
        return None

    runner._run_turn = turn  # type: ignore[method-assign]
    ctx = await runner.run("author a demo workflow")
    assert "exhausted 1 attempts" in ctx.fail_reason
    assert runner._cfg.app_state.workflow_run().status == "failed"
    append = runner._cfg.conv_store.append_event
    assert isinstance(append, MagicMock)
    assert "create_workflow failed" in str(append.call_args_list[-1])


async def test_run_reports_an_unexpected_error_without_raising() -> None:
    runner = _runner()

    async def boom(_state: object, _ctx: object) -> None:
        raise RuntimeError("state machine exploded")

    runner._dispatch = boom  # type: ignore[method-assign]
    ctx = await runner.run("author a demo workflow")
    assert isinstance(ctx, CreateWorkflowContext)
    assert runner._cfg.app_state.workflow_run().status == "failed"
    append = runner._cfg.conv_store.append_event
    assert isinstance(append, MagicMock)
    assert "state machine exploded" in str(append.call_args_list[-1])


async def test_run_marks_the_run_failed_and_reraises_on_cancellation() -> None:
    runner = _runner()

    async def turn(_text: str, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    runner._run_turn = turn  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await runner.run("author a demo workflow")
    assert runner._cfg.app_state.workflow_run().status == "failed"


async def test_run_gives_every_run_a_fresh_id_and_shared_memory() -> None:
    runner = _runner(max_attempts=1)

    async def turn(_text: str, **_kwargs: object) -> None:
        return None

    runner._run_turn = turn  # type: ignore[method-assign]
    first = await runner.run("one")
    second = await runner.run("two")
    assert first.run_id != second.run_id
    assert first.shared_memory is not second.shared_memory


async def test_run_loops_generate_after_a_rejection_then_completes(tmp_path: Path) -> None:
    """The repair edge validate → generate is driven entirely by tool calls."""
    runner = _validating_runner(tmp_path)
    rejected = {"done": False}

    async def turn(_text: str, **kwargs: object) -> None:
        tools = _by_name(kwargs["tools"])
        if "finalize_design" in tools:
            await _call(kwargs["tools"], "request_design_approval", "the design", "demo")
            await _call(kwargs["tools"], "finalize_design", "the design", "demo")
        elif "mark_generation_complete" in tools:
            source = _GOOD_SOURCE if rejected["done"] else "class Nope:\n    pass\n"
            _write(tmp_path, "demo.py", source)
            await _call(
                kwargs["tools"], "mark_generation_complete", "wrote it", "workflows/demo.py"
            )
        elif "approve_workflow" in tools:
            if rejected["done"]:
                await _call(kwargs["tools"], "approve_workflow", "now it loads")
            else:
                rejected["done"] = True
                await _call(kwargs["tools"], "reject_workflow", "it does not import")

    runner._run_turn = turn  # type: ignore[method-assign]
    ctx = await runner.run("author a demo workflow")

    assert ctx.repair_cycles == 1
    assert ctx.validation_summary == "now it loads"
    assert runner._cfg.app_state.workflow_run().status == "complete"


# ── runner: resume ────────────────────────────────────────────────────────────


async def test_resume_rejects_a_foreign_context() -> None:
    runner = _runner()
    with pytest.raises(TypeError, match="WorkflowContext"):
        await runner.resume({"intent": "x"})


async def test_resume_rejects_a_legacy_generic_context_instead_of_restarting() -> None:
    runner = _runner()
    with pytest.raises(TypeError, match="legacy generic context"):
        await runner.resume(
            WorkflowContext(intent="author a workflow", run_id="r", workflow_name="create_workflow")
        )
