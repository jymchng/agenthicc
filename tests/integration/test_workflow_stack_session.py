"""Foreground workflow calls, returns, and paused child recovery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from rich.console import Console

from agenthicc.plugins.authored_types import (
    AuthoredRun,
    PublishedArtifact,
    StagedArtifact,
    content_digest,
)
from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners import tui_session
from agenthicc.runners.headless import _close_headless_session, execute_workflow
from agenthicc.runners.tui_session import TUISession
from agenthicc.runners.workflow_stack import WorkflowCall
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime import session_log
from agenthicc.tui.workspace.workspace import Workspace
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.authoring.verdict import ValidationReport
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec, WorkflowContext, WorkflowPlugin
from agenthicc.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

pytestmark = pytest.mark.integration


class ChildState(Enum):
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ChildContext(WorkflowContext):
    state: ChildState = ChildState.COMPLETE
    authored_run: AuthoredRun | None = None


class ChildRunner(BaseWorkflowRunner):
    def __init__(self, config: WorkflowConfig, authored_run: AuthoredRun | None = None) -> None:
        self._config = config
        self._authored_run = authored_run

    async def run(self, intent: str) -> ChildContext:
        handle = self._config.workflow_handle
        assert handle is not None
        result = ChildContext(
            intent,
            handle.run_id,
            handle.workflow_name,
            current_phase="inspect",
            state=ChildState.FAILED if intent == "pause at first phase" else ChildState.COMPLETE,
            authored_run=self._authored_run,
        )
        result.add_output(
            PhaseOutput("inspect", "reviewer", full_text="The fixture was inspected.")
        )
        handle.attach_context(result)
        return result

    async def resume(self, context: object) -> ChildContext:
        assert isinstance(context, ChildContext)
        return replace(context, state=ChildState.COMPLETE)


class ChildWorkflow(WorkflowPlugin):
    name = "inspect_fixture"
    phases = [PhaseSpec("inspect", allowed_capabilities=frozenset({ToolCapability.READ}))]

    @classmethod
    def build_runner(
        cls, config: WorkflowConfig, mode_manager: ModeManager | None
    ) -> BaseWorkflowRunner:
        return ChildRunner(config)


@pytest.fixture
async def stack_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[TUISession, SessionContext, MockTransport]]:
    project = tmp_path / "project"
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    project.mkdir()
    sessions.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSION_INDEX", sessions / "index.json")
    config = project / "agenthicc.toml"
    config.write_text("[skills]\ninstall_default_skills = false\n", encoding="utf-8")
    trust = ExtensionTrust(store_path=home / "trust.json", trusted_roots=())
    with override_extension_trust(trust):
        context = await tui_session._build_session_context(
            None, [], config_path=str(config), headless=True, mode_name="Safe"
        )
        transport = MockTransport()
        context.agent_runner = AgentRunnerBase(transport=transport)
        context.workflow_registry.register(ChildWorkflow)
        workspace = Mock(spec=Workspace)
        workspace.overlays = Mock()
        session = TUISession(context, workspace, Mock(spec=UnifiedInputSession))
        processor_task = asyncio.create_task(context.processor.run(), name="workflow-stack-test")
        try:
            yield session, context, transport
        finally:
            if context.startup is not None:
                await context.startup.close()
            await _close_headless_session(context, processor_task, None)


def _reply(transport: MockTransport, message: str) -> None:
    transport.queue_response(
        Completion(
            id=message,
            model="mock-model",
            content=message,
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )
    )


async def _settle(session: TUISession) -> None:
    async with asyncio.timeout(10):
        while session._agent_task is not None:
            await session._agent_task


async def test_workflow_call_returns_to_direct_agent(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    _reply(transport, "Received the result.")
    _reply(transport, "Continuing normally.")
    assert session.route("/workflow call inspect_fixture inspect the fixture")
    await _settle(session)
    assert context.workflow_stack.depth == 0
    assert context.workflow_stack.top.workflow_name == "default"
    assert len(transport.calls) == 1
    returned = repr(transport.calls[0].messages)
    assert returned.count("[WORKFLOW RETURN]") == 1
    assert "The fixture was inspected." in returned

    context.app_state.conversation.queue.append("Continue normally")
    session.advance()
    await _settle(session)
    assert len(transport.calls) == 2
    assert "Continue normally" in repr(transport.calls[1].messages)


async def test_headless_workflow_call_returns_with_the_same_frame_identity(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    _session, context, transport = stack_session
    child = context.workflow_stack.push(WorkflowCall(ChildWorkflow, "inspect the fixture", "user"))
    _reply(transport, "Received the headless result.")

    result = await execute_workflow(context, child.workflow_name, child.intent)

    assert result.status == "complete", result
    assert result.run_id == child.run_id
    assert context.workflow_stack.depth == 0
    assert len(transport.calls) == 1
    assert repr(transport.calls[0].messages).count("[WORKFLOW RETURN]") == 1


async def test_catalog_command_matches_the_model_and_survives_an_unchanged_reload(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    output = StringIO()
    context.console = Console(file=output, width=200, force_terminal=False)
    _reply(transport, "Reviewed the project.")
    await execute_workflow(context, "default", "Review the project.")
    catalog = context.capability_catalog.current(session._make_workflow_tools())
    assert catalog and catalog in str(transport.calls[0].system)
    assert session.route("/catalog")
    assert output.getvalue().strip() == catalog
    await context.processor.drain()
    assert session.route("/workflow stack")
    assert context.workflow_stack.base.frame_id in output.getvalue()
    assert context.workflow_stack.top.workflow_name == "default"

    await context.reload.reload(frozenset({"workflows"}), reason="command")
    _reply(transport, "Reviewed it again.")
    await execute_workflow(context, "default", "Review the project.")
    assert context.capability_catalog.current(session._make_workflow_tools()) == catalog
    assert catalog in str(transport.calls[1].system)


async def test_plan_catalog_lists_only_eligible_read_only_workflows(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    from agenthicc.workflows.plugin import WorkflowInvocation

    session, context, _transport = stack_session

    class ReadOnly(ChildWorkflow):
        name = "readonly_fixture"
        invocation = WorkflowInvocation(agent_callable=True)

    class Mutating(ChildWorkflow):
        name = "mutating_fixture"
        invocation = WorkflowInvocation(agent_callable=True)
        phases = [PhaseSpec("edit", allowed_capabilities=frozenset({ToolCapability.WRITE}))]

    context.workflow_registry.register(ReadOnly)
    context.workflow_registry.register(Mutating)
    context.mode_manager.set_by_name("Plan")
    session._select_base_workflow("default")
    output = StringIO()
    context.console = Console(file=output, width=200, force_terminal=False)
    assert session.route("/catalog")
    assert "readonly_fixture" in output.getvalue()
    assert "mutating_fixture" not in output.getvalue()


async def test_reload_report_shows_path_code_digest_and_no_file_contents(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    from agenthicc.tui.workspace.overlays.registry_list import RegistryMessageOverlay

    session, context, _transport = stack_session
    tools = Path(".agenthicc/tools")
    tools.mkdir(parents=True)
    source = tools / "report_refusal.py"
    source.write_text("raise RuntimeError('private fixture text')\n", encoding="utf-8")
    report = await context.reload.reload(frozenset({"tools"}), reason="command")
    diagnostic = next(item for item in report.diagnostics if item.code == "untrusted")
    assert diagnostic.digest
    assert session.route("/reload report")
    overlay = session._workspace.overlays.show.call_args.args[0]
    assert isinstance(overlay, RegistryMessageOverlay)
    output = StringIO()
    Console(file=output, width=200, force_terminal=False).print(overlay.render())
    assert source.name in output.getvalue()
    assert "untrusted" in output.getvalue()
    assert diagnostic.digest[:12] in output.getvalue()
    assert "private fixture text" not in output.getvalue()


async def test_authored_return_loads_skill_before_the_parent_request(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    store = context.authored.store()
    stage = store.stage_directory("authored-child", "skill") / "release-notes"
    stage.mkdir()
    markdown = b"---\nname: Release Notes\ndescription: Prepare release notes\n---\nRead changes.\n"
    (stage / "SKILL.md").write_bytes(markdown)
    path = ".agenthicc/skills/release-notes/SKILL.md"
    digest = content_digest({path: markdown})
    artifact = PublishedArtifact("skill", "release-notes", (path,), digest, "staged")
    run = AuthoredRun(
        "authored-child",
        context.session_id,
        "offline",
        (StagedArtifact(artifact, ValidationReport(digest, ())),),
    )

    from agenthicc.workflows.plugin import WorkflowInvocation

    class AuthoredChild(ChildWorkflow):
        name = "authored_child"
        invocation = WorkflowInvocation(publishes=("skill",))

        @classmethod
        def build_runner(
            cls, config: WorkflowConfig, mode_manager: ModeManager | None
        ) -> BaseWorkflowRunner:
            return ChildRunner(config, authored_run=run)

    context.workflow_registry.register(AuthoredChild)
    transport.queue_tool_use("list_skills", {"query": "release"})
    _reply(transport, "Read the published inventory.")
    assert session.route("/workflow call authored_child prepare the skill")
    await _settle(session)

    assert context.workflow_stack.depth == 0
    assert "release-notes" in context.reload.skills.current().value
    assert len(transport.calls) == 2
    returned = repr(transport.calls[0].messages)
    assert "release-notes" in returned and "loaded" in returned
    assert "model_invocable" in repr(transport.calls[1].messages)


async def test_paused_frame_pins_its_definition_and_rejects_changed_topology(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
) -> None:
    session, context, transport = stack_session
    assert session.route("/workflow call inspect_fixture pause at first phase")
    await _settle(session)
    frame = context.workflow_stack.top

    class ChangedChild(ChildWorkflow):
        phases = [PhaseSpec("different")]

    changed = WorkflowRegistry()
    changed.register(ChangedChild)
    context.workflow_registry.replace_with(changed)
    assert session.route("/workflow resume")
    assert context.workflow_stack.top == frame
    assert "PLUGIN_INCOMPATIBLE" in str(context.app_state.conversation.notification())
    assert session._agent_task is None

    context.workflow_registry.replace_with(WorkflowRegistry())
    assert context.workflow_stack.top.definition is ChildWorkflow
    _reply(transport, "Resumed the pinned definition.")
    assert session.route("/workflow resume")
    await _settle(session)
    assert context.workflow_stack.depth == 0
    assert len(transport.calls) == 1


@pytest.mark.parametrize("resolution", ["resume", "return --abandon"])
async def test_failed_child_keeps_selection_locked_until_operator_action(
    stack_session: tuple[TUISession, SessionContext, MockTransport], resolution: str
) -> None:
    session, context, transport = stack_session
    assert session.route("/workflow call inspect_fixture pause at first phase")
    await _settle(session)
    stack = context.workflow_stack
    assert stack.depth == 1
    assert stack.base.status == "suspended"
    assert stack.top.status == "paused"
    assert transport.calls == []

    assert session.route("/workflow code_plan")
    assert stack.top.workflow_name == "inspect_fixture"
    assert "Finish or abandon" in str(context.app_state.conversation.notification())

    _reply(transport, "Handled outcome.")
    assert session.route(f"/workflow {resolution}")
    await _settle(session)
    assert stack.depth == 0
    assert stack.top.workflow_name == "default"
    assert len(transport.calls) == 1
    status = "abandoned" if resolution.endswith("--abandon") else "completed"
    assert f"Status: {status}" in repr(transport.calls[0].messages)
