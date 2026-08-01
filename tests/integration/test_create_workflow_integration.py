"""Integration tests for create_workflow across real component boundaries.

Every test here wires at least one real collaborator rather than a stub:

* a real :class:`~agenthicc.kernel.processor.EventProcessor` receiving the
  workflow lifecycle events;
* a real :class:`~agenthicc.tools.approval.ApprovalService` driving the design
  approval gate through ``pending_approval`` / ``respond``;
* real TOML config through :func:`~agenthicc.config.load_config`;
* the real workflow loader and registry importing the file the run produced.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.config import AgenthiccConfig, load_config
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.approval import ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow import (
    CreateWorkflow,
    CreateWorkflowParams,
    CreateWorkflowRunner,
    CreateWorkflowState,
    validate_workflow_file,
)
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.integration


# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture
async def processor(tmp_path: Path):
    """A real, running EventProcessor with persistence disabled."""
    kernel_state = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


_GENERATED_SOURCE = '''\
"""doc_review — draft a document, then review it."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DocReview(WorkflowPlugin):
    name = "doc_review"
    description = "Draft a document, then review it."
    mode_bindings = []
    phases = [
        PhaseSpec(
            name="draft",
            max_turns=20,
            next="review",
            mode_override="Yolo",
            system_prompt_override="You are in the DRAFT phase. Write the document.",
        ),
        PhaseSpec(
            name="review",
            max_turns=8,
            on_reject="draft",
            output_schema="free_text",
            system_prompt_override="You are in the REVIEW phase. Check the document.",
        ),
    ]
'''

_BROKEN_SOURCE = '''\
"""doc_review — first attempt with a dangling edge."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DocReview(WorkflowPlugin):
    name = "doc_review"
    description = "Draft a document, then review it."
    mode_bindings = []
    phases = [PhaseSpec(name="draft", next="reviewww")]
'''


def _workflow_config(
    *,
    app: AppState,
    proc: EventProcessor,
    cfg: AgenthiccConfig,
    approval_svc: ApprovalService | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=proc,
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="transport"))
        ),  # type: ignore[arg-type]
        approval_svc=approval_svc,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
        params=CreateWorkflow.build_params({}),
    )


def _base_config(tmp_path: Path) -> AgenthiccConfig:
    cfg = AgenthiccConfig()
    cfg.execution.authoring_max_generation_attempts = 3
    cfg.execution.authoring_max_phase_turns = 4
    cfg.execution.effective_usable_budget = lambda: 10_000  # type: ignore[method-assign]
    return cfg


def _runner(
    tmp_path: Path,
    proc: EventProcessor,
    *,
    approval_svc: ApprovalService | None = None,
    app: AppState | None = None,
    cfg: AgenthiccConfig | None = None,
) -> CreateWorkflowRunner:
    app = app or AppState.create()
    runner = CreateWorkflowRunner(
        _workflow_config(
            app=app,
            proc=proc,
            cfg=cfg or _base_config(tmp_path),
            approval_svc=approval_svc,
        )
    )
    runner._workspace_root = lambda: tmp_path  # type: ignore[method-assign]
    runner._cfg.app_state.update_workflow_phase = MagicMock()  # type: ignore[method-assign]
    return runner


def _by_name(tools: object) -> dict[str, object]:
    assert isinstance(tools, list)
    return {getattr(tool, "__name__", ""): tool for tool in tools}


async def _call(tools: object, name: str, *args: object) -> object:
    tool = _by_name(tools)[name]
    assert callable(tool)
    return await tool(*args)


def _authoring_turn(
    tmp_path: Path,
    *,
    sources: list[str],
    verdicts: list[str],
) -> object:
    """Return a fake ``_run_turn`` that really writes files and really votes.

    *sources* is consumed one entry per generation phase; *verdicts* one entry
    per validation phase (``"approve"`` or ``"reject"``).
    """
    target = tmp_path / ".agenthicc" / "workflows" / "doc_review.py"

    async def run_turn(_text: str, **kwargs: object) -> None:
        tools = _by_name(kwargs["tools"])
        if "finalize_design" in tools:
            await _call(kwargs["tools"], "request_design_approval", "draft → review", "doc_review")
            await _call(kwargs["tools"], "finalize_design", "draft → review", "doc_review")
        elif "mark_generation_complete" in tools:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sources.pop(0), encoding="utf-8")
            await _call(
                kwargs["tools"],
                "mark_generation_complete",
                "wrote the doc_review workflow",
                str(target),
            )
        elif "approve_workflow" in tools:
            if verdicts.pop(0) == "approve":
                await _call(kwargs["tools"], "approve_workflow", "imports and matches the design")
            else:
                await _call(kwargs["tools"], "reject_workflow", "the phase graph is broken")

    return run_turn


# ── builtin registration ──────────────────────────────────────────────────────


async def test_create_workflow_is_a_registered_builtin() -> None:
    assert CreateWorkflow in load_builtin_workflows()
    registry = build_workflow_registry(project_dir=Path("/nonexistent"), user_dir=Path("/nope"))
    assert registry.get("create_workflow") is CreateWorkflow
    entry = registry.get_entry("create_workflow")
    assert entry is not None
    assert entry.source == "builtin"
    assert entry.plugin_cls is CreateWorkflow


async def test_registry_builds_the_state_machine_runner(tmp_path: Path, processor) -> None:
    registry = build_workflow_registry(project_dir=Path("/nonexistent"), user_dir=Path("/nope"))
    plugin_cls = registry.get("create_workflow")
    assert plugin_cls is not None
    app = AppState.create()
    config = _workflow_config(app=app, proc=processor, cfg=_base_config(tmp_path))
    built = plugin_cls.build_runner(config, None)
    assert isinstance(built, CreateWorkflowRunner)
    assert built.total_phases == len(plugin_cls.phases)


# ── real processor: full lifecycle ────────────────────────────────────────────


async def test_full_run_against_a_real_processor(tmp_path: Path, processor) -> None:
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path, sources=[_GENERATED_SOURCE], verdicts=["approve"]
    )

    ctx = await runner.run("build me a doc review workflow")
    await processor.drain()

    assert ctx.workflow_name == "doc_review"
    assert ctx.repair_cycles == 0
    assert set(ctx.artifacts) == {"design", "generate", "validate", "summarize"}
    assert runner._cfg.app_state.workflow_run().status == "complete"
    assert (tmp_path / ".agenthicc" / "workflows" / "doc_review.py").exists()


async def test_repair_loop_against_a_real_processor(tmp_path: Path, processor) -> None:
    """A broken first attempt is rejected, regenerated, and then accepted."""
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path,
        sources=[_BROKEN_SOURCE, _GENERATED_SOURCE],
        verdicts=["reject", "approve"],
    )

    ctx = await runner.run("build me a doc review workflow")
    await processor.drain()

    assert ctx.repair_cycles == 1
    assert ctx.validation_summary == "imports and matches the design"
    assert runner._cfg.app_state.workflow_run().status == "complete"
    assert ctx.artifacts["validate"].metadata["ok"] is True


async def test_deterministic_check_overrides_a_wrong_approval(tmp_path: Path, processor) -> None:
    """Approving a broken file cannot finish the run — it loops back to generate."""
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path,
        sources=[_BROKEN_SOURCE, _GENERATED_SOURCE],
        verdicts=["approve", "approve"],
    )

    ctx = await runner.run("build me a doc review workflow")
    await processor.drain()

    assert ctx.repair_cycles == 1
    # The second generation ran inside repair cycle 1, i.e. the wrong approval
    # was re-routed instead of finishing the run.
    assert ctx.artifacts["generate"].metadata["repair_cycle"] == 1
    assert runner._cfg.app_state.workflow_run().status == "complete"
    assert ctx.artifacts["validate"].metadata["ok"] is True


# ── real registry: the generated workflow becomes loadable ────────────────────


async def test_generated_workflow_is_discovered_by_the_real_registry(
    tmp_path: Path, processor
) -> None:
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path, sources=[_GENERATED_SOURCE], verdicts=["approve"]
    )
    ctx = await runner.run("build me a doc review workflow")
    await processor.drain()

    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "no-user-dir",
    )
    entry = registry.get_entry("doc_review")
    assert entry is not None
    assert entry.source == "project"
    assert entry.path == ctx.generated_path
    assert entry.plugin_cls.phase_names() == ["draft", "review"]
    first = entry.plugin_cls.first_phase()
    assert first is not None
    assert first.mode_override == "Yolo"


async def test_generated_workflow_loads_through_load_python_workflows(
    tmp_path: Path, processor
) -> None:
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path, sources=[_GENERATED_SOURCE], verdicts=["approve"]
    )
    ctx = await runner.run("build me a doc review workflow")
    await processor.drain()

    plugins = load_python_workflows(Path(ctx.generated_path), source="project")
    assert [plugin.name for plugin in plugins] == ["doc_review"]
    assert plugins[0].build_params({}) is not None


async def test_cache_stable_template_round_trips_through_real_validator(tmp_path: Path) -> None:
    """The authoring inspection surface produces a strict-valid custom runner."""
    template = await _call(make_inspection_tools(), "show_workflow_template")
    assert isinstance(template, dict)
    source = template["source"]
    assert isinstance(source, str)

    package = tmp_path / ".agenthicc" / "workflows" / "release_check"
    package.mkdir(parents=True)
    entry = package / "runner.py"
    entry.write_text(source, encoding="utf-8")

    report = validate_workflow_file(
        str(package),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
    )
    assert report.ok, report.render()
    assert report.cache_contract == "contract-native"
    assert report.phase_names == ("plan", "verify", "report")


async def test_generated_workflow_does_not_shadow_the_builtins(tmp_path: Path, processor) -> None:
    runner = _runner(tmp_path, processor)
    runner._run_turn = _authoring_turn(  # type: ignore[method-assign]
        tmp_path, sources=[_GENERATED_SOURCE], verdicts=["approve"]
    )
    await runner.run("build me a doc review workflow")
    await processor.drain()

    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "no-user-dir",
    )
    assert {"code_plan", "create_workflow", "doc_review"} <= set(registry.names())
    code_plan = registry.get_entry("code_plan")
    assert code_plan is not None
    assert code_plan.source == "builtin"


# ── real approval service ─────────────────────────────────────────────────────


async def test_design_gate_uses_the_real_approval_service(tmp_path: Path, processor) -> None:
    app = AppState.create()
    approval = ApprovalService(app)
    runner = _runner(tmp_path, processor, approval_svc=approval, app=app)

    async def responder() -> None:
        """Approve the first plan_review request that appears."""
        for _ in range(200):
            pending = app.pending_approval()
            if pending is not None:
                assert pending.kind == "plan_review"
                assert pending.tool_input["workflow_name"] == "doc_review"
                approval.respond(True, message="looks good")
                return
            await asyncio.sleep(0.005)
        raise AssertionError("no approval request was ever raised")

    async def run_turn(_text: str, **kwargs: object) -> None:
        task = asyncio.create_task(responder())
        try:
            await _call(kwargs["tools"], "request_design_approval", "draft → review", "doc_review")
            await _call(kwargs["tools"], "finalize_design", "draft → review", "doc_review")
        finally:
            await task

    runner._run_turn = run_turn  # type: ignore[method-assign]
    from agenthicc.workflows.create_workflow.state import CreateWorkflowContext

    ctx = CreateWorkflowContext(intent="build doc_review", run_id="run", shared_memory=MagicMock())
    assert await runner._design(ctx) is CreateWorkflowState.GENERATE
    assert ctx.workflow_name == "doc_review"
    assert app.pending_approval() is None


async def test_denied_design_blocks_the_handoff_with_the_real_service(
    tmp_path: Path, processor
) -> None:
    app = AppState.create()
    approval = ApprovalService(app)
    runner = _runner(tmp_path, processor, approval_svc=approval, app=app)

    async def responder() -> None:
        for _ in range(200):
            if app.pending_approval() is not None:
                approval.respond(False, message="too vague")
                return
            await asyncio.sleep(0.005)
        raise AssertionError("no approval request was ever raised")

    async def run_turn(_text: str, **kwargs: object) -> None:
        task = asyncio.create_task(responder())
        try:
            denied = await _call(
                kwargs["tools"], "request_design_approval", "draft → review", "doc_review"
            )
            assert isinstance(denied, dict)
            assert denied["approved"] is False
            blocked = await _call(
                kwargs["tools"], "finalize_design", "draft → review", "doc_review"
            )
            assert isinstance(blocked, dict)
            assert blocked["ok"] is False
        finally:
            await task

    runner._run_turn = run_turn  # type: ignore[method-assign]
    from agenthicc.workflows.create_workflow.state import CreateWorkflowContext

    ctx = CreateWorkflowContext(intent="build doc_review", run_id="run", shared_memory=MagicMock())
    assert await runner._design(ctx) is CreateWorkflowState.FAILED
    assert "without calling finalize_design()" in ctx.fail_reason


# ── real TOML config ──────────────────────────────────────────────────────────


def test_authoring_budgets_come_from_real_toml(tmp_path: Path) -> None:
    toml = tmp_path / "agenthicc.toml"
    toml.write_text(
        "[execution]\nauthoring_max_generation_attempts = 6\nauthoring_max_phase_turns = 9\n",
        encoding="utf-8",
    )
    cfg = load_config(project_path=toml, user_path=tmp_path / "missing.toml")
    assert cfg.execution.authoring_max_generation_attempts == 6
    assert cfg.execution.authoring_max_phase_turns == 9

    app = AppState.create()
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=SimpleNamespace(emit=None),  # type: ignore[arg-type]
        agent_runner=SimpleNamespace(_transport=None),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
    )
    runner = CreateWorkflowRunner(config)
    assert runner._max_attempts == 6
    assert runner._max_phase_turns == 9
    assert runner._max_repair_cycles == 6


def test_phase_models_come_from_real_toml(tmp_path: Path) -> None:
    toml = tmp_path / "agenthicc.toml"
    toml.write_text(
        '[workflows.create_workflow]\ngenerate_model = "big-model"\nvalidate_model = "small"\n',
        encoding="utf-8",
    )
    cfg = load_config(project_path=toml, user_path=tmp_path / "missing.toml")
    params = CreateWorkflow.build_params(cfg.workflows.get("create_workflow", {}))
    assert isinstance(params, CreateWorkflowParams)

    app = AppState.create()
    base = _base_config(tmp_path)
    config = WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=SimpleNamespace(emit=None),  # type: ignore[arg-type]
        agent_runner=SimpleNamespace(_transport=None),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=base,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=SimpleNamespace(),  # type: ignore[arg-type]
        agents_registry=SimpleNamespace(),  # type: ignore[arg-type]
        params=params,
    )
    runner = CreateWorkflowRunner(config)
    assert runner._phase_model("generate") == "big-model"
    assert runner._phase_model("validate") == "small"
    assert runner._phase_model("design") == ""


async def test_per_phase_model_reaches_the_turn(tmp_path: Path, processor) -> None:
    app = AppState.create()
    config = dataclasses.replace(
        _workflow_config(app=app, proc=processor, cfg=_base_config(tmp_path)),
        params=CreateWorkflowParams(generate_model="big-model"),
    )
    runner = CreateWorkflowRunner(config)
    runner._workspace_root = lambda: tmp_path  # type: ignore[method-assign]
    runner._cfg.app_state.update_workflow_phase = MagicMock()  # type: ignore[method-assign]

    seen: dict[str, object] = {}

    async def run_turn(_text: str, **kwargs: object) -> None:
        seen[str(kwargs["phase_name"])] = kwargs["model_override"]
        tools = _by_name(kwargs["tools"])
        if "mark_generation_complete" in tools:
            await _call(kwargs["tools"], "mark_generation_complete", "wrote it", "x.py")

    runner._run_turn = run_turn  # type: ignore[method-assign]
    from agenthicc.workflows.create_workflow.state import CreateWorkflowContext

    ctx = CreateWorkflowContext(intent="build it", run_id="run", shared_memory=MagicMock())
    ctx.workflow_name = "doc_review"
    await runner._generate(ctx)
    assert seen == {"generate": "big-model"}


# ── validation against real files ─────────────────────────────────────────────


def test_validation_accepts_a_real_builtin_style_definition(tmp_path: Path) -> None:
    """A file shaped like the real builtin definitions passes validation."""
    directory = tmp_path / ".agenthicc" / "workflows"
    directory.mkdir(parents=True)
    path = directory / "doc_review.py"
    path.write_text(_GENERATED_SOURCE, encoding="utf-8")

    report = validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    assert report.ok, report.render()
    assert report.plugin_names == ("doc_review",)
    assert report.phase_names == ("draft", "review")


def test_validation_agrees_with_the_loader_on_a_broken_file(tmp_path: Path) -> None:
    """Anything the loader silently skips must be a validation error."""
    directory = tmp_path / ".agenthicc" / "workflows"
    directory.mkdir(parents=True)
    path = directory / "doc_review.py"
    path.write_text("import nonexistent_module_xyz\n", encoding="utf-8")

    assert load_python_workflows(path, source="project") == []
    report = validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    assert not report.ok
    assert "ModuleNotFoundError" in report.errors[0]


def test_validation_reimports_after_the_file_is_repaired(tmp_path: Path) -> None:
    directory = tmp_path / ".agenthicc" / "workflows"
    directory.mkdir(parents=True)
    path = directory / "doc_review.py"

    path.write_text(_BROKEN_SOURCE, encoding="utf-8")
    first = validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    assert not first.ok

    path.write_text(_GENERATED_SOURCE, encoding="utf-8")
    second = validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    assert second.ok, second.render()


def test_validation_does_not_leak_the_module_into_sys_modules(tmp_path: Path) -> None:
    import sys

    directory = tmp_path / ".agenthicc" / "workflows"
    directory.mkdir(parents=True)
    path = directory / "doc_review.py"
    path.write_text(_GENERATED_SOURCE, encoding="utf-8")

    before = set(sys.modules)
    validate_workflow_file(str(path), expected_name="doc_review", root=tmp_path)
    leaked = {name for name in set(sys.modules) - before if "doc_review" in name}
    assert leaked == set()
