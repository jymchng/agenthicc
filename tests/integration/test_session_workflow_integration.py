"""Integration coverage for one session conversation across workflow runs."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.validation import validate_workflow_file
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec, WorkflowPlugin

pytestmark = pytest.mark.integration


@pytest.fixture
async def processor(tmp_path: Path):
    kernel = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    processor = EventProcessor(initial_state=kernel, persist=False)
    task = asyncio.create_task(processor.run())
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _config(
    app: TUIAppState,
    processor: EventProcessor,
    conversation: SessionConversation,
    handle: WorkflowRunHandle,
) -> WorkflowConfig:
    cfg = AgenthiccConfig()
    return WorkflowConfig(
        conv_store=app.conversation,
        app_state=app,
        processor=processor,
        agent_runner=SimpleNamespace(
            _transport=SimpleNamespace(_config=SimpleNamespace(model="test-model"))
        ),  # type: ignore[arg-type]
        approval_svc=None,
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=MagicMock(),
        agents_registry=MagicMock(),
        session_memory=conversation.memory,
        conversation_id=conversation.conversation_id,
        workflow_handle=handle,
    )


async def test_workflow_runner_uses_session_memory_and_persists_terminal_checkpoint(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    class Workflow(WorkflowPlugin):
        name = "integration_workflow"
        phases = [PhaseSpec(name="inspect")]

    conversation = SessionConversation.open(
        "session-integration",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    conversation.memory.add_user("Earlier direct chat")
    conversation.memory.add_assistant({"role": "assistant", "content": "Earlier answer"})
    store = WorkflowCheckpointStore("session-integration", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="workflow-run",
        workflow=Workflow,
        conversation=conversation,
        intent="Inspect the project",
        checkpoint_store=store,
    )
    app = TUIAppState.create()
    runner = WorkflowRunner(Workflow, _config(app, processor, conversation, handle))

    async def fake_phase(
        spec: PhaseSpec,
        intent: str,
        context: object,
    ) -> PhaseOutput:
        assert runner._shared_memory is conversation.memory
        assert runner._cfg.conversation_id == "session-integration"
        return PhaseOutput(phase_name=spec.name, role="auto", full_text="inspected")

    runner._run_phase = fake_phase  # type: ignore[method-assign]
    context = await runner.run("Inspect the project")

    assert runner._shared_memory is conversation.memory
    assert context.current_phase == "inspect"
    assert handle.lifecycle == "complete"
    checkpoint = store.load("workflow-run")
    assert checkpoint is not None
    assert checkpoint.conversation_id == conversation.conversation_id
    assert checkpoint.conversation_cursor == conversation.cursor
    assert len(conversation.messages) == 2
    conversation.close()


async def test_resume_reuses_checkpoint_context_and_current_phase(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    class Workflow(WorkflowPlugin):
        name = "resume_integration"
        phases = [PhaseSpec(name="plan"), PhaseSpec(name="execute")]

    conversation = SessionConversation.open(
        "session-resume",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("session-resume", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="resume-run",
        workflow=Workflow,
        conversation=conversation,
        intent="resume me",
        checkpoint_store=store,
    )
    from agenthicc.workflows.plugin import WorkflowContext

    context = WorkflowContext(
        intent="resume me",
        run_id="resume-run",
        workflow_name=Workflow.name,
        current_phase="execute",
        phase_iteration=2,
    )
    handle.attach_context(context)
    handle.request_pause()
    handle.mark_paused()
    checkpoint = handle.save_checkpoint(reason="escape")
    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=Workflow,
        conversation=conversation,
        checkpoint_store=store,
    )
    assert isinstance(restored.context, WorkflowContext)

    app = TUIAppState.create()
    config = _config(app, processor, conversation, restored)
    runner = WorkflowRunner(Workflow, config)
    seen: list[str] = []

    async def fake_loop(intent, current_context, wf_run, run_id, start_phase):
        seen.append(start_phase)

    runner._run_phase_loop = fake_loop  # type: ignore[method-assign]
    await runner.resume(restored.context)
    assert seen == ["execute"]
    assert runner._shared_memory is conversation.memory
    conversation.close()


async def test_generated_runner_example_is_checkpointable_after_reload(
    tmp_path: Path,
    processor: EventProcessor,
) -> None:
    """The public authoring template survives validation and process-style restore."""
    inspection = make_inspection_tools()
    show_example = next(
        tool for tool in inspection if getattr(tool, "__name__", "") == "show_example_workflow"
    )
    assert callable(show_example)
    result = await show_example()
    source = result["source"]
    assert isinstance(source, str)

    workflow_path = tmp_path / ".agenthicc" / "workflows" / "release_check.py"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(source, encoding="utf-8")
    report = validate_workflow_file(
        str(workflow_path), expected_name="release_check", root=tmp_path
    )
    assert report.ok, report.render()

    spec = importlib.util.spec_from_file_location("generated_release_check", workflow_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    plugin = module.ReleaseCheckWorkflow
    memory = object()
    context = module.ReleaseContext(
        intent="check the release",
        run_id="generated-run",
        plan="run tests",
        state=module.ReleaseState.VERIFY,
        phase_iteration=3,
        shared_memory=memory,
    )

    conversation = SessionConversation.open(
        "generated-session",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("generated-session", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="generated-run",
        workflow=plugin,
        conversation=conversation,
        intent=context.intent,
        checkpoint_store=store,
    )
    handle.attach_context(context)
    handle.request_pause()
    handle.mark_paused()
    checkpoint = handle.save_checkpoint(reason="escape")

    restored = WorkflowRunHandle.from_checkpoint(
        checkpoint,
        workflow=plugin,
        conversation=conversation,
        checkpoint_store=store,
    )
    assert restored.context.state is module.ReleaseState.VERIFY
    assert restored.context.phase_iteration == 3
    assert restored.context.shared_memory is conversation.memory
    conversation.close()
