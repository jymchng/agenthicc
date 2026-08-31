"""End-to-end goal_flow coverage with real agent turns and durable checkpoints."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.goal_flow import GoalFlowWorkflow
from agenthicc.workflows.goal_flow.runner import GoalState, GoalStatus

pytestmark = pytest.mark.e2e


def _text(index: int, content: str) -> Completion:
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _tool_use(index: int, name: str, payload: dict[str, object]) -> Completion:
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content="",
        tool_calls=[ToolCall(tool_use_id=f"tc-{index}", name=name, input=payload)],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _script(*steps: tuple[str, dict[str, object]] | str) -> MockTransport:
    transport = MockTransport()
    for index, step in enumerate(steps):
        if isinstance(step, tuple):
            transport.queue_response(_tool_use(index, step[0], step[1]))
        else:
            transport.queue_response(_text(index, step))
    return transport


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
    await asyncio.sleep(0)
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_goal_flow_real_turns_checkpoint_each_verified_goal(tmp_path, processor) -> None:
    """Tool-only transitions drive the real flow and save one boundary per goal."""
    conversation = SessionConversation.open(
        "goal-flow-e2e",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore("goal-flow-e2e", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="goal-e2e-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="implement two goals",
            checkpoint_store=store,
        )
        transport = _script(
            (
                "complete_clarification",
                {"notes": "The requirements are concrete and testable."},
            ),
            "Clarification recorded.",
            (
                "finalize_goals",
                {
                    "goals": [
                        {"text": "first goal"},
                        {"text": "second goal"},
                    ]
                },
            ),
            "Goals recorded.",
            (
                "goal_implemented",
                {"summary": "Implemented first goal.", "files": ["one.py"]},
            ),
            "First implementation recorded.",
            (
                "verify_goal",
                {"satisfied": True, "evidence": "First goal tests passed."},
            ),
            "First goal verified.",
            (
                "goal_implemented",
                {"summary": "Implemented second goal.", "files": ["two.py"]},
            ),
            "Second implementation recorded.",
            (
                "verify_goal",
                {"satisfied": True, "evidence": "Second goal tests passed."},
            ),
            "Second goal verified.",
            (
                "complete_workflow",
                {
                    "summary": "Both goals were implemented and verified.",
                    "files": ["one.py", "two.py"],
                },
            ),
            "Workflow complete.",
        )
        app = TUIAppState.create()
        checkpoint_reasons: list[str] = []
        original_save = handle.save_checkpoint

        def save_checkpoint(*, reason: str = ""):
            checkpoint_reasons.append(reason)
            return original_save(reason=reason)

        handle.save_checkpoint = save_checkpoint  # type: ignore[method-assign]
        config = WorkflowConfig(
            conv_store=app.conversation,
            app_state=app,
            processor=processor,
            agent_runner=AgentRunnerBase(transport=transport, signals=SignalBus()),
            approval_svc=None,
            cfg=AgenthiccConfig(),
            skills={},
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=MagicMock(),
            session_memory=conversation.memory,
            conversation_id=conversation.conversation_id,
            workflow_handle=handle,
        )

        from agenthicc.workflows.goal_flow.runner import GoalFlowRunner

        result = await GoalFlowRunner(config, None).run("implement two goals")

        assert result.state is GoalState.COMPLETE
        assert result.completed_goal_indices == [0, 1]
        assert len(result.goal_checkpoint_revisions) == 2
        first = store.load("goal-e2e-run")
        assert first is not None
        assert checkpoint_reasons.count("goal_1_completed") == 1
        assert checkpoint_reasons.count("goal_2_completed") == 1
        assert first.context["fields"]["completed_goal_indices"] == [0, 1]  # type: ignore[index]
        assert len(conversation.messages) >= 1
        assert transport.calls
    finally:
        conversation.close()


async def test_goal_flow_dynamic_mutations_resume_with_stable_goal_identity(
    tmp_path: Path,
    processor,
) -> None:
    """Provider-facing mutation tools schedule all discovered work once."""
    conversation = SessionConversation.open(
        "goal-flow-dynamic-e2e",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore("goal-flow-dynamic-e2e", root=tmp_path / "checkpoints")
        handle = WorkflowRunHandle.create(
            run_id="goal-dynamic-e2e-run",
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            intent="implement and verify discovered work",
            checkpoint_store=store,
        )
        transport = _script(
            ("complete_clarification", {"notes": "The work is concrete."}),
            "Clarification recorded.",
            ("finalize_goals", {"goals": ["first goal", "second goal"]}),
            "Goals recorded.",
            ("append_goal", {"goal": "follow-up goal"}),
            "The follow-up is queued; continue the active goal.",
            ("goal_implemented", {"summary": "Implemented first goal.", "files": ["one.py"]}),
            "Implementation recorded.",
            ("insert_goal", {"index": 0, "goal": "prerequisite goal"}),
            "The prerequisite is queued; continue verifying the active goal.",
            ("verify_goal", {"satisfied": True, "evidence": "First goal tests passed."}),
            "First goal verified.",
            ("goal_implemented", {"summary": "Implemented prerequisite.", "files": []}),
            "Prerequisite implementation recorded.",
            ("verify_goal", {"satisfied": True, "evidence": "Prerequisite tests passed."}),
            "Prerequisite verified.",
            ("goal_implemented", {"summary": "Implemented second goal.", "files": ["two.py"]}),
            "Second implementation recorded.",
            ("verify_goal", {"satisfied": True, "evidence": "Second goal tests passed."}),
            "Second goal verified.",
            ("goal_implemented", {"summary": "Implemented follow-up.", "files": []}),
            "Follow-up implementation recorded.",
            ("verify_goal", {"satisfied": True, "evidence": "Follow-up tests passed."}),
            "Follow-up verified.",
            (
                "complete_workflow",
                {"summary": "All original and discovered goals are complete.", "files": []},
            ),
            "Workflow complete.",
        )
        app = TUIAppState.create()
        config = WorkflowConfig(
            conv_store=app.conversation,
            app_state=app,
            processor=processor,
            agent_runner=AgentRunnerBase(transport=transport, signals=SignalBus()),
            approval_svc=None,
            cfg=AgenthiccConfig(),
            skills={},
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=MagicMock(),
            session_memory=conversation.memory,
            conversation_id=conversation.conversation_id,
            workflow_handle=handle,
        )

        from agenthicc.workflows.goal_flow.runner import GoalFlowRunner

        result = await GoalFlowRunner(config, None).run("implement and verify discovered work")

        assert result.state is GoalState.COMPLETE
        assert [record.text for record in result.goal_records] == [
            "prerequisite goal",
            "first goal",
            "second goal",
            "follow-up goal",
        ]
        assert all(record.status is GoalStatus.VERIFIED for record in result.goal_records)
        assert len({record.goal_id for record in result.goal_records}) == 4
        assert result.goal_list_revision == 2
        call_dump = str(transport.calls)
        assert "append_goal" in call_dump
        assert "insert_goal" in call_dump

        checkpoint = store.load("goal-dynamic-e2e-run")
        assert checkpoint is not None
        fields = checkpoint.context["fields"]
        assert isinstance(fields, dict)
        assert fields["goal_list_revision"] == 2
        assert fields["active_goal_id"] == ""  # type: ignore[index]
        assert all(
            record["status"] == "verified"
            for record in fields["goal_records"]  # type: ignore[index]
        )
    finally:
        conversation.close()
