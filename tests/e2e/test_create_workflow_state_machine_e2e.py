"""E2E tests: the create_workflow state machine driven by real agent turns.

These tests do not stub ``_run_turn``.  A ``MockTransport`` returns real
``tool_use`` completions, so the whole chain runs for real: the agent-turn
runner executes the phase-transition tools, the real ``write_file`` tool writes
the workflow into the workspace, the runner imports that file to validate it,
and the workflow registry then discovers the generated plugin.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.fs.agent_tools import read_file, write_file
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.create_workflow import CreateWorkflow, CreateWorkflowRunner
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.e2e


# ── generated workflow sources the fake model "writes" ────────────────────────

_GOOD_SOURCE = '''\
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
"""doc_review — first attempt, with a dangling transition edge."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DocReview(WorkflowPlugin):
    name = "doc_review"
    description = "Draft a document, then review it."
    mode_bindings = []
    phases = [PhaseSpec(name="draft", next="reviewww")]
'''

_TARGET = ".agenthicc/workflows/doc_review.py"


# ── transport plumbing ────────────────────────────────────────────────────────


def _text(index: int, content: str) -> Completion:
    """A plain end_turn completion."""
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _tool_use(index: int, name: str, payload: dict) -> Completion:
    """A tool_use completion invoking exactly one tool."""
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content="",
        tool_calls=[ToolCall(tool_use_id=f"tc-{index}", name=name, input=payload)],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _script(*steps: tuple[str, dict] | str) -> MockTransport:
    """Queue *steps* onto a MockTransport.

    A ``(tool_name, payload)`` tuple becomes a tool_use completion; a plain
    string becomes an end_turn text completion.
    """
    mock = MockTransport()
    for index, step in enumerate(steps):
        if isinstance(step, tuple):
            mock.queue_response(_tool_use(index, step[0], step[1]))
        else:
            mock.queue_response(_text(index, step))
    return mock


_DESIGN_STEPS: tuple[tuple[str, dict] | str, ...] = (
    ("request_design_approval", {"design": "draft → review", "workflow_name": "doc_review"}),
    ("finalize_design", {"design": "draft → review", "workflow_name": "doc_review"}),
    "The design is finalized.",
)


def _generate_steps(source: str) -> tuple[tuple[str, dict] | str, ...]:
    return (
        ("write_file", {"path": _TARGET, "content": source}),
        ("mark_generation_complete", {"summary": "wrote doc_review", "path": _TARGET}),
        "The workflow file is written.",
    )


_APPROVE_STEPS: tuple[tuple[str, dict] | str, ...] = (
    ("approve_workflow", {"summary": "it imports and matches the design"}),
    "Approved.",
)

_REJECT_STEPS: tuple[tuple[str, dict] | str, ...] = (
    ("reject_workflow", {"reason": "the draft phase points at a phase that does not exist"}),
    "Rejected.",
)

_SUMMARY_STEPS: tuple[tuple[str, dict] | str, ...] = (
    "Created doc_review at .agenthicc/workflows/doc_review.py with phases draft → review.",
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app_state():
    return TUIAppState.create()


@pytest.fixture
async def processor(tmp_path):
    kernel_state = AppState.create(
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


def _runner(app_state, processor, mock: MockTransport) -> CreateWorkflowRunner:
    """Build a CreateWorkflowRunner over a real agent runner and real fs tools."""
    cfg = AgenthiccConfig()
    cfg.execution.authoring_max_generation_attempts = 3
    cfg.execution.authoring_max_phase_turns = 8
    config = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,
        agent_runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
        approval_svc=None,  # headless: design approval auto-grants
        cfg=cfg,
        skills={},
        plugin_tools=[write_file, read_file],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=build_agents_registry(),
        params=CreateWorkflow.build_params({}),
    )
    return CreateWorkflowRunner(config)


# ── happy path ────────────────────────────────────────────────────────────────


async def test_e2e_authors_a_workflow_end_to_end(app_state, processor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)

    ctx = await runner.run("create a doc_review workflow that drafts then reviews a document")
    await processor.drain()

    # The state machine ran every phase and finished.
    assert app_state.workflow_run().status == "complete"
    assert app_state.workflow_run().current_phase is None
    assert set(ctx.artifacts) == {"design", "generate", "validate", "summarize"}

    # The design phase captured the approved design and name via tool calls only.
    assert ctx.design == "draft → review"
    assert ctx.workflow_name == "doc_review"

    # The generation phase really wrote the file through the real write tool.
    written = tmp_path / _TARGET
    assert written.exists()
    assert "class DocReview(WorkflowPlugin)" in written.read_text(encoding="utf-8")

    # The validation phase imported it and passed.
    assert ctx.artifacts["validate"].metadata["ok"] is True
    assert ctx.artifacts["validate"].metadata["phase_names"] == ["draft", "review"]
    assert ctx.validation_summary == "it imports and matches the design"
    assert ctx.repair_cycles == 0


async def test_e2e_generated_workflow_is_immediately_loadable(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)
    await runner.run("create a doc_review workflow")
    await processor.drain()

    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "absent",
    )
    plugin_cls = registry.get("doc_review")
    assert plugin_cls is not None
    assert plugin_cls.description == "Draft a document, then review it."
    assert plugin_cls.phase_names() == ["draft", "review"]

    # And the new plugin builds a real runner through the normal factory path.
    built = plugin_cls.build_runner(runner._cfg, None)
    assert built is not None


async def test_e2e_authored_workflow_ships_and_runs_its_own_state_machine(
    app_state, processor, tmp_path, monkeypatch
):
    """The shape create_workflow now demands is authored, loaded, and executed.

    The generated file is the runner example the design phase hands the agent, so
    this asserts the encouraged shape actually works end to end: the loader
    discovers it, `build_runner()` returns its own runner, and that runner's
    typed state machine drives its phases to a terminal state.
    """
    monkeypatch.chdir(tmp_path)
    from agenthicc.workflows.create_workflow.inspection_tools import _RUNNER_EXAMPLE

    target = ".agenthicc/workflows/release_check.py"
    mock = _script(
        (
            "request_design_approval",
            {"design": "plan → verify → report, with a runner", "workflow_name": "release_check"},
        ),
        (
            "finalize_design",
            {"design": "plan → verify → report, with a runner", "workflow_name": "release_check"},
        ),
        "Design finalized.",
        ("write_file", {"path": target, "content": _RUNNER_EXAMPLE}),
        ("mark_generation_complete", {"summary": "wrote release_check", "path": target}),
        "File written.",
        ("approve_workflow", {"summary": "imports, and the runner matches the design"}),
        "Approved.",
        "Created release_check with its own state-machine runner.",
    )
    wf_runner = _runner(app_state, processor, mock)

    ctx = await wf_runner.run("create a release_check workflow with its own runner")
    await processor.drain()

    assert app_state.workflow_run().status == "complete"
    assert ctx.artifacts["validate"].metadata["ok"] is True
    assert ctx.artifacts["validate"].metadata["warnings"] == []

    source = (tmp_path / target).read_text(encoding="utf-8")
    assert "class ReleaseState(Enum)" in source
    assert "while not state.is_terminal" in source
    assert "match state:" in source

    # The registry discovers it and build_runner() returns the file's own runner.
    registry = build_workflow_registry(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "absent",
    )
    plugin_cls = registry.get("release_check")
    assert plugin_cls is not None
    generated_runner = plugin_cls.build_runner(wf_runner._cfg, None)
    assert type(generated_runner).__name__ == "ReleaseCheckRunner"
    assert type(generated_runner).__module__ != CreateWorkflowRunner.__module__

    # And that runner's own state machine really runs, transitioning only on tool calls.
    seen: list[str] = []

    async def drive(_text: str, **kwargs: object) -> None:
        by_name = {getattr(t, "__name__", ""): t for t in kwargs["tools"]}  # type: ignore[union-attr]
        if "submit_release_plan" in by_name:
            seen.append("plan")
            await by_name["submit_release_plan"]("run the test suite")
        elif "release_passed" in by_name:
            seen.append("verify")
            await by_name["release_passed"]("all green")
        else:
            seen.append("report")

    generated_runner._run_turn = drive  # type: ignore[attr-defined]
    result = await generated_runner.run("check the release")

    assert seen == ["plan", "verify", "report"]
    assert getattr(result, "plan", "") == "run the test suite"
    assert getattr(result, "verdict", "") == "all green"
    assert getattr(result, "fail_reason", "") == ""


async def test_e2e_emits_the_full_workflow_event_lifecycle(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)

    queue = processor.subscribe_events()
    try:
        await runner.run("create a doc_review workflow")
        await processor.drain()
    finally:
        processor.unsubscribe_events(queue)

    seen: list[str] = []
    while not queue.empty():
        seen.append(queue.get_nowait().event_type)

    assert "WorkflowRunStarted" in seen
    assert seen.count("WorkflowPhaseStarted") == 4
    assert seen.count("WorkflowPhaseCompleted") == 4
    assert "WorkflowRunCompleted" in seen
    assert seen.index("WorkflowRunStarted") < seen.index("WorkflowRunCompleted")


# ── repair loop ───────────────────────────────────────────────────────────────


async def test_e2e_broken_file_is_rejected_regenerated_and_accepted(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_BROKEN_SOURCE),
        *_REJECT_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    assert ctx.repair_cycles == 1
    assert app_state.workflow_run().status == "complete"
    assert ctx.artifacts["validate"].metadata["ok"] is True
    written = (tmp_path / _TARGET).read_text(encoding="utf-8")
    assert "reviewww" not in written


async def test_e2e_a_wrong_approval_cannot_finish_the_run(
    app_state, processor, tmp_path, monkeypatch
):
    """The agent approves a broken file; the deterministic check overrules it."""
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_BROKEN_SOURCE),
        *_APPROVE_STEPS,  # wrong — the file does not load
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,  # now legitimate
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    assert ctx.repair_cycles == 1
    assert ctx.artifacts["generate"].metadata["repair_cycle"] == 1
    assert app_state.workflow_run().status == "complete"
    assert ctx.artifacts["validate"].metadata["ok"] is True


async def test_e2e_run_fails_when_the_repair_budget_is_exhausted(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    steps: list[tuple[str, dict] | str] = list(_DESIGN_STEPS)
    # attempts = 1 → one generate, one reject, and the second rejection fails.
    for _ in range(4):
        steps.extend(_generate_steps(_BROKEN_SOURCE))
        steps.extend(_REJECT_STEPS)
    mock = _script(*steps)
    runner = _runner(app_state, processor, mock)
    runner._max_repair_cycles = 1

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    assert app_state.workflow_run().status == "failed"
    assert "limit 1" in ctx.fail_reason
    assert ctx.repair_cycles == 2


# ── exit path ─────────────────────────────────────────────────────────────────


async def test_e2e_exits_without_authoring_when_no_workflow_is_wanted(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        ("exit_create_workflow", {"suggestion": "just ask the question directly"}),
        "No new workflow is needed here.",
    )
    runner = _runner(app_state, processor, mock)

    ctx = await runner.run("what is a workflow anyway?")
    await processor.drain()

    assert app_state.workflow_run().status == "exited"
    assert ctx.suggestion == "just ask the question directly"
    assert ctx.artifacts["design"].kind == "exit"
    assert not (tmp_path / ".agenthicc" / "workflows").exists()


# ── failure paths ─────────────────────────────────────────────────────────────


async def test_e2e_design_that_never_finalizes_fails_the_run(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script("I am thinking about it.", "Still thinking.", "Yet more thinking.")
    runner = _runner(app_state, processor, mock)
    runner._max_attempts = 3

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    assert app_state.workflow_run().status == "failed"
    assert "without calling finalize_design()" in ctx.fail_reason
    assert ctx.artifacts == {}


async def test_e2e_generation_without_a_file_is_reported_to_the_agent(
    app_state, processor, tmp_path, monkeypatch
):
    """Marking generation complete without writing anything fails validation.

    The repair budget is set to zero so the *first* validation report is also the
    final one, letting the test assert on exactly what the agent was shown.
    """
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        ("mark_generation_complete", {"summary": "all done", "path": _TARGET}),
        "Marked complete.",
        *_REJECT_STEPS,
    )
    runner = _runner(app_state, processor, mock)
    runner._max_repair_cycles = 0

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    report = ctx.artifacts["validate"].content
    assert "result: FAIL" in report
    assert "No file exists" in report
    assert app_state.workflow_run().status == "failed"
    assert not (tmp_path / _TARGET).exists()


async def test_e2e_writing_outside_the_workspace_is_refused(
    app_state, processor, tmp_path, monkeypatch
):
    """A path outside the workspace root is never imported, even if it is valid."""
    monkeypatch.chdir(tmp_path)
    outside = tmp_path.parent / "escaped_workflow.py"
    outside.write_text(_GOOD_SOURCE, encoding="utf-8")

    mock = _script(
        *_DESIGN_STEPS,
        ("mark_generation_complete", {"summary": "wrote it", "path": str(outside)}),
        "Marked complete.",
        *_REJECT_STEPS,
    )
    runner = _runner(app_state, processor, mock)
    runner._max_repair_cycles = 0

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    report = ctx.artifacts["validate"].content
    assert "outside the workspace root" in report
    assert ctx.artifacts["validate"].metadata["ok"] is False
    assert ctx.artifacts["validate"].metadata["plugin_names"] == []
    assert app_state.workflow_run().status == "failed"


# ── shared context across phases ──────────────────────────────────────────────


async def test_e2e_one_shared_memory_spans_every_phase(app_state, processor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)

    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    assert ctx.shared_memory is not None
    # Four phases of real turns all appended to the same ShortTermMemory, so the
    # generation phase can see the design turn without re-deriving it.
    messages = ctx.shared_memory.messages()
    assert len(messages) > 4
    roles = {message.get("role") for message in messages if isinstance(message, dict)}
    assert "user" in roles


async def test_e2e_artifacts_record_what_each_phase_produced(
    app_state, processor, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    mock = _script(
        *_DESIGN_STEPS,
        *_generate_steps(_GOOD_SOURCE),
        *_APPROVE_STEPS,
        *_SUMMARY_STEPS,
    )
    runner = _runner(app_state, processor, mock)
    ctx = await runner.run("create a doc_review workflow")
    await processor.drain()

    kinds = {phase: artifact.kind for phase, artifact in ctx.artifacts.items()}
    assert kinds == {
        "design": "design",
        "generate": "workflow_file",
        "validate": "validation_report",
        "summarize": "summary",
    }
    assert ctx.artifacts["design"].metadata["workflow_name"] == "doc_review"
    assert Path(str(ctx.artifacts["generate"].metadata["path"])).name == "doc_review.py"
    assert ctx.artifacts["validate"].metadata["errors"] == []
    assert ctx.artifacts["summarize"].metadata["repair_cycles"] == 0
