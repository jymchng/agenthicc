"""Kill and reopen a real session at the durable workflow return boundary."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase

from agenthicc.kernel import AppState, root_reducer
from agenthicc.kernel.processor import restore_from_log
from agenthicc.runners.headless import (
    _close_headless_session,
    _recover_headless_return,
    execute_workflow,
)
from agenthicc.runners.tui_session import TUISession, _build_session_context
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.workspace.workspace import Workspace
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.plugin import (
    PhaseOutput,
    PhaseSpec,
    WorkflowContext,
    WorkflowInvocation,
    WorkflowPlugin,
)

from .harness import ScriptedTransport, reply, tool_call

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.runners.workflow_stack import WorkflowDelivery
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig


class _Child(WorkflowPlugin):
    name = "durable_inspection"
    invocation = WorkflowInvocation(agent_callable=True)
    phases = [PhaseSpec("inspect", agent_type="planner")]


class _PausingChildRunner(BaseWorkflowRunner):
    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config

    async def run(self, intent: str) -> WorkflowContext:
        handle = self._config.workflow_handle
        assert handle is not None
        handle.attach_context(
            WorkflowContext(intent, handle.run_id, handle.workflow_name, current_phase="inspect")
        )
        raise RuntimeError("The fixture pauses at a recoverable phase.")

    async def resume(self, context: object) -> WorkflowContext:
        assert isinstance(context, WorkflowContext)
        context.add_output(PhaseOutput("inspect", "reviewer", full_text="Inspection recovered."))
        return context


class _PausingChild(WorkflowPlugin):
    name = "pausing_child"
    invocation = WorkflowInvocation(agent_callable=True)
    phases = [PhaseSpec("inspect", agent_type="planner")]

    @classmethod
    def build_runner(
        cls, config: WorkflowConfig, mode_manager: ModeManager | None
    ) -> BaseWorkflowRunner:
        return _PausingChildRunner(config)


class _WaitingParent(WorkflowPlugin):
    name = "waiting_parent"
    invocation = WorkflowInvocation(allows_child_workflows=True)
    phases = [PhaseSpec("review", agent_type="planner")]


def _child_environment(home: Path) -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    return {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
    }


async def _session(action: str, resume_id: str | None) -> None:
    context = await _build_session_context(resume_id, [], headless=True, mode_name="Yolo")
    script = ScriptedTransport()
    context.agent_runner = AgentRunnerBase(transport=script)
    context.workflow_registry.register(_Child)
    context.workflow_registry.register(_PausingChild)
    context.workflow_registry.register(_WaitingParent)
    processor = context.processor
    task = asyncio.create_task(processor.run())
    try:
        if action == "pause":
            script.expect(
                "enter pausing child",
                lambda request: "enter_workflow" in request.tools,
                tool_call(
                    "enter_workflow",
                    {
                        "workflow": _PausingChild.name,
                        "intent": "pause at first phase",
                    },
                ),
            )
            script.expect(
                "yield parent",
                lambda request: request.contains("handoff_accepted"),
                reply("Waiting."),
            )
            await execute_workflow(context, _WaitingParent.name, "Review the fixture.")
            parent, leaf = context.workflow_stack.frames
            assert parent.status == "suspended" and leaf.status == "paused"
            script.assert_finished()
            await processor.flush_events()
            print(json.dumps({"session": context.session_id, "leaf": leaf.run_id}), flush=True)
            return
        if action == "resume-paused":
            tui = TUISession(context, Mock(spec=Workspace), Mock(spec=UnifiedInputSession))
            tui._restore_paused_workflow()
            await processor.drain()
            parent, leaf = context.app_state.conversation.workflow_stack()
            assert parent.status == "suspended" and leaf.status == "paused"
            assert not script.requests and tui._agent_task is None
            script.expect(
                "continue waiting workflow",
                lambda request: request.contains("[WORKFLOW RETURN]"),
                reply("The fixture was reviewed."),
            )
            assert tui.route(f"/workflow resume {leaf.run_id}")
            async with asyncio.timeout(10):
                while tui._agent_task is not None:
                    await tui._agent_task
            script.assert_finished()
            assert context.workflow_stack.top.frame_id == parent.frame_id
            assert [frame.status for frame in processor.get_state().workflow_frames] == [
                "running",
                "completed",
            ]
            print(
                json.dumps({"resumed": leaf.run_id, "requests": len(script.requests)}), flush=True
            )
            return
        if action in {"packet", "returned", "started"}:

            def mark(key: str) -> None:
                print(
                    json.dumps({"paused_at": action, "session": context.session_id, "key": key}),
                    flush=True,
                )

            if action == "packet":
                bridge = context.workflow_bridge
                record_return = bridge.record_return

                def stop_after_packet(delivery: WorkflowDelivery) -> None:
                    record_return(delivery)
                    mark(delivery.key)
                    ThreadEvent().wait()

                bridge.record_return = stop_after_packet
            elif action == "returned":
                flush = processor.flush_events

                async def pause_after_return() -> None:
                    await flush()
                    for frame in processor.get_state().workflow_frames:
                        if frame.summary_digest and frame.return_key is not None:
                            mark(frame.return_key)
                            await asyncio.Event().wait()

                processor.flush_events = pause_after_return
            else:
                assert context.session_conversation is not None
                journal = context.session_conversation.journal
                journal_class = type(journal)
                turn_started = journal_class.turn_started

                def stop_after_start(
                    self: ConversationJournal,
                    turn_id: str,
                    user_message: str,
                    base_count: int,
                    *,
                    conversation_id: str = "",
                    base_cursor: int | None = None,
                    workflow_run_id: str | None = None,
                ) -> None:
                    turn_started(
                        self,
                        turn_id,
                        user_message,
                        base_count,
                        conversation_id=conversation_id,
                        base_cursor=base_cursor,
                        workflow_run_id=workflow_run_id,
                    )
                    if turn_id.startswith("return:"):
                        mark(turn_id)
                        ThreadEvent().wait()

                journal_class.turn_started = stop_after_start
            script.expect(
                "enter child",
                lambda request: "enter_workflow" in request.tools,
                tool_call(
                    "enter_workflow", {"workflow": _Child.name, "intent": "Inspect the sample."}
                ),
            )
            script.expect(
                "yield to child",
                lambda request: request.contains("handoff_accepted"),
                reply("Waiting."),
            )
            script.expect(
                "inspect",
                lambda request: request.contains("Inspect the sample."),
                reply("Inspection result."),
            )
            await execute_workflow(context, "default", "Inspect the sample in a workflow.")
            raise AssertionError("Return boundary was not reached.")
        if action == "resume":
            script.expect(
                "return to parent",
                lambda request: (
                    request.contains("[WORKFLOW RETURN]") and request.contains("Inspection result.")
                ),
                reply("Received the inspection."),
            )
        recovered = await _recover_headless_return(context)
        assert (recovered is not None) == (action == "resume")
        script.assert_finished()
        assert len(script.requests) == (1 if action == "resume" else 0)
        await processor.flush_events()
        replayed = await restore_from_log(
            processor.get_state().settings.event_log_path, AppState.create(), root_reducer
        )
        assert replayed.workflow_frames == processor.get_state().workflow_frames
        print(json.dumps({"requests": len(script.requests)}), flush=True)
    finally:
        await _close_headless_session(context, task, None)


@pytest.mark.e2e
@pytest.mark.skipif(sys.platform == "win32", reason="Exercises abrupt POSIX process termination")
@pytest.mark.parametrize("boundary", ["packet", "returned", "started"])
async def test_restart_delivers_one_parent_turn_after_durable_return(
    tmp_path: Path, boundary: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "agenthicc.toml").write_text(
        "[skills]\ninstall_default_skills = false\n", encoding="utf-8"
    )
    environment = _child_environment(home)

    async def launch(action: str, session_id: str = "") -> Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.e2e.self_extension.test_s07_durable_return",
            action,
            session_id,
            cwd=project,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    child = await launch(boundary)
    try:
        assert child.stdout is not None
        async with asyncio.timeout(30):
            while line := await child.stdout.readline():
                if line.startswith(b'{"paused_at"'):
                    mark = json.loads(line)
                    break
            else:
                _, error = await child.communicate()
                pytest.fail(error.decode())
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()

    for action, expected in (("resume", 1), ("inspect", 0)):
        child = await launch(action, mark["session"])
        try:
            output, error = await asyncio.wait_for(child.communicate(), 30)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert child.returncode == 0, error.decode()
        assert f'"requests": {expected}' in output.decode()

    session_path = home / ".agenthicc" / "sessions" / mark["session"]
    journals = list(session_path.rglob("conversation-journal.jsonl"))
    assert len(journals) == 1, journals
    rows = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
    assert (
        sum(row.get("kind") == "turn_started" and row.get("turn_id") == mark["key"] for row in rows)
        == 1
    )


@pytest.mark.e2e
async def test_restart_restores_a_paused_child_and_resumes_its_suspended_caller(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "agenthicc.toml").write_text(
        "[skills]\ninstall_default_skills = false\n", encoding="utf-8"
    )
    packet: dict[str, str] = {}
    for action in ("pause", "resume-paused"):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.e2e.self_extension.test_s07_durable_return",
            action,
            packet.get("session", ""),
            cwd=project,
            env=_child_environment(home),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await asyncio.wait_for(child.communicate(), 30)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert child.returncode == 0, error.decode() + output.decode()
        record = json.loads(next(line for line in output.splitlines() if line.startswith(b"{")))
        if action == "pause":
            packet = record
        else:
            assert record == {"resumed": packet["leaf"], "requests": 1}


if __name__ == "__main__":
    asyncio.run(_session(sys.argv[1], sys.argv[2] or None))
