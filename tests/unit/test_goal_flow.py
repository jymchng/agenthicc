"""Clean-slate tests for the copied goal_flow workflow and goal boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.workflows.goal_flow import GoalFlowWorkflow
from agenthicc.workflows.goal_flow.runner import (
    GoalContext,
    GoalFlowParams,
    GoalFlowRunner,
    GoalState,
    GoalStatus,
    _make_goal_mutation_tools,
    _make_clarify_tools,
    _make_decide_goals_tools,
    _make_implement_tools,
    _make_summarize_tools,
    _make_verify_tools,
)
from agenthicc.workflows.loader import load_builtin_workflows

pytestmark = pytest.mark.unit


def _runner_and_context(
    tmp_path: Path,
) -> tuple[GoalFlowRunner, GoalContext, WorkflowCheckpointStore, SessionConversation]:
    conversation = SessionConversation.open(
        "goal-flow-unit",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    store = WorkflowCheckpointStore("goal-flow-unit", root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="goal-run",
        workflow=GoalFlowWorkflow,
        conversation=conversation,
        intent="implement two goals",
        checkpoint_store=store,
    )
    context = GoalContext(
        intent="implement two goals",
        run_id="goal-run",
        state=GoalState.VERIFY_GOAL,
        phase_iteration=3,
        goals=["first", "second"],
        goal_index=0,
        goal_attempts=[1, 0],
        goal_evidence=["first implementation", ""],
        goal_files=[["one.py"], []],
        shared_memory=conversation.memory,
    )
    handle.attach_context(context)
    handle.update_phase("verify_goal", 3, 3)
    config = SimpleNamespace(
        workflow_handle=handle,
        cfg=AgenthiccConfig(),
        agent_runner=SimpleNamespace(),
        conv_store=SimpleNamespace(append_event=lambda *_args: None),
        params=None,
    )
    runner = GoalFlowRunner(config, None)
    return runner, context, store, conversation


def test_goal_flow_is_a_builtin_and_has_a_checkpoint_codec() -> None:
    assert GoalFlowWorkflow in load_builtin_workflows()
    assert GoalFlowWorkflow.name == "goal_flow"
    assert callable(GoalFlowWorkflow.checkpoint_context_to_payload)
    assert callable(GoalFlowWorkflow.checkpoint_context_from_payload)


@pytest.mark.asyncio
async def test_finalize_goals_accepts_structured_text_items() -> None:
    """Models may return the UI's ``[{"text": ...}]`` goal representation."""
    from agenthicc.workflows.goal_flow.runner import _make_decide_goals_tools

    event = asyncio.Event()
    data: dict[str, list[str]] = {}
    finalize_goals = _make_decide_goals_tools(event, data)[0]

    result = await finalize_goals(
        goals=[
            {"text": " Paginate the contents page at 20 lessons per page "},
        ]
    )

    assert result["ok"] is True
    assert data["goals"] == ["Paginate the contents page at 20 lessons per page"]
    assert event.is_set()


@pytest.mark.asyncio
async def test_finalize_goals_keeps_string_compatibility_and_rejects_bad_items() -> None:
    """The old string form remains valid and malformed objects do not crash."""
    from agenthicc.workflows.goal_flow.runner import _make_decide_goals_tools

    event = asyncio.Event()
    data: dict[str, list[str]] = {}
    finalize_goals = _make_decide_goals_tools(event, data)[0]

    result = await finalize_goals(goals=[" first goal ", "", "second goal"])
    assert result["ok"] is True
    assert data["goals"] == ["first goal", "second goal"]
    assert event.is_set()

    bad_event = asyncio.Event()
    bad_data: dict[str, list[str]] = {}
    bad_finalize_goals = _make_decide_goals_tools(bad_event, bad_data)[0]
    bad_result = await bad_finalize_goals(goals=[{"title": "missing supported text field"}])

    assert bad_result["ok"] is False
    assert "index 0" in str(bad_result["error"])
    assert not bad_event.is_set()


def test_completed_goal_checkpoint_advances_cursor_and_is_idempotent(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)

        checkpoint = store.load("goal-run")
        assert checkpoint is not None
        assert checkpoint.reason == "goal_1_completed"
        assert checkpoint.status == "running"
        assert checkpoint.current_phase == "implement_goal"
        assert checkpoint.phase_index == 2
        assert checkpoint.context["kind"] == "CustomContext"
        fields = checkpoint.context["fields"]
        assert isinstance(fields, dict)
        assert fields["state"] == "IMPLEMENT_GOAL"
        assert fields["completed_goal_indices"] == [0]
        assert fields["goal_checkpoint_revisions"] == [checkpoint.revision]
        assert context.completed_goal_indices == [0]
        assert context.goal_checkpoint_revisions == [checkpoint.revision]

        revision = checkpoint.revision
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)
        assert store.load("goal-run").revision == revision  # type: ignore[union-attr]

        context.goal_index = 1
        runner._checkpoint_completed_goal(context, 1, GoalState.SUMMARIZE)
        latest = store.load("goal-run")
        assert latest is not None
        assert latest.reason == "goal_2_completed"
        assert latest.context["fields"]["completed_goal_indices"] == [0, 1]  # type: ignore[index]
        assert latest.context["fields"]["goal_checkpoint_revisions"] == [2, 3]  # type: ignore[index]
    finally:
        conversation.close()


def test_goal_checkpoint_codec_restores_completion_audit_and_memory(tmp_path: Path) -> None:
    runner, context, _store, conversation = _runner_and_context(tmp_path)
    try:
        context.completed_goal_indices = [0, 1]
        context.goal_checkpoint_revisions = [2, 5]
        payload = GoalFlowWorkflow.checkpoint_context_to_payload(context)
        memory = object()
        restored = GoalFlowWorkflow.checkpoint_context_from_payload(payload, memory)

        assert restored.state is GoalState.VERIFY_GOAL
        assert restored.goals == ["first", "second"]
        assert restored.completed_goal_indices == [0, 1]
        assert restored.goal_checkpoint_revisions == [2, 5]
        assert restored.shared_memory is memory
    finally:
        conversation.close()


def test_completed_goal_checkpoint_rehydrates_through_run_handle(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        context.goal_index = 1
        runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)
        checkpoint = store.load("goal-run")
        assert checkpoint is not None

        restored = WorkflowRunHandle.from_checkpoint(
            checkpoint,
            workflow=GoalFlowWorkflow,
            conversation=conversation,
            checkpoint_store=store,
        )

        assert isinstance(restored.context, GoalContext)
        assert restored.context.state is GoalState.IMPLEMENT_GOAL
        assert restored.context.goal_index == 1
        assert restored.context.completed_goal_indices == [0]
        assert restored.context.goal_checkpoint_revisions == [checkpoint.revision]
        assert restored.context.shared_memory is conversation.memory
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_goal_transition_tools_reject_invalid_payloads() -> None:
    """Every phase tool fails closed without changing its transition event."""
    clarify_event, clarify_data = asyncio.Event(), {}
    clarify = _make_clarify_tools(clarify_event, clarify_data)[0]
    result = await clarify(notes=" ")
    assert result["ok"] is False
    assert not clarify_event.is_set()

    decide_event, decide_data = asyncio.Event(), {}
    decide = _make_decide_goals_tools(decide_event, decide_data)[0]
    assert (await decide(goals="not-a-list"))["ok"] is False  # type: ignore[arg-type]
    assert (await decide(goals=[1, {"title": "unsupported"}]))["ok"] is False  # type: ignore[list-item]
    assert (await decide(goals=["", {"text": "  "}]))["ok"] is False  # type: ignore[list-item]
    assert not decide_event.is_set()

    implement_event, implement_data = asyncio.Event(), {}
    implement = _make_implement_tools(implement_event, implement_data)[0]
    result = await implement(summary=" ", files=[])
    assert result["ok"] is False
    assert not implement_event.is_set()

    verify_event, verify_data = asyncio.Event(), {}
    verify = _make_verify_tools(verify_event, verify_data)[0]
    result = await verify(satisfied=False, evidence=" ")
    assert result["ok"] is False
    assert not verify_event.is_set()

    summarize_event, summarize_data = asyncio.Event(), {}
    summarize = _make_summarize_tools(summarize_event, summarize_data)[0]
    result = await summarize(summary=" ", files=[])
    assert result["ok"] is False
    assert not summarize_event.is_set()


def _runner_without_handle() -> GoalFlowRunner:
    return GoalFlowRunner(
        SimpleNamespace(
            workflow_handle=None,
            session_memory=object(),
            cfg=AgenthiccConfig(),
            agent_runner=SimpleNamespace(),
        ),
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "state", "kwargs"),
    [
        ("_clarify", GoalState.CLARIFY, {}),
        ("_decide_goals", GoalState.DECIDE_GOALS, {}),
        (
            "_implement_goal",
            GoalState.IMPLEMENT_GOAL,
            {"goals": ["goal"], "goal_attempts": [0], "goal_evidence": [""], "goal_files": [[]]},
        ),
        (
            "_verify_goal",
            GoalState.VERIFY_GOAL,
            {
                "goals": ["goal"],
                "goal_attempts": [1],
                "goal_evidence": ["implemented"],
                "goal_files": [[]],
            },
        ),
        (
            "_summarize",
            GoalState.SUMMARIZE,
            {"goals": ["goal"], "goal_attempts": [1]},
        ),
    ],
)
async def test_goal_phase_fails_after_bounded_missing_transition(
    method_name: str,
    state: GoalState,
    kwargs: dict[str, object],
) -> None:
    runner = _runner_without_handle()
    context = GoalContext(intent="intent", state=state, **kwargs)  # type: ignore[arg-type]

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    result = await getattr(runner, method_name)(context, object())

    assert result is GoalState.FAILED
    assert context.fail_reason


@pytest.mark.asyncio
async def test_verify_false_loops_back_to_implementation() -> None:
    runner = _runner_without_handle()
    context = GoalContext(
        intent="intent",
        state=GoalState.VERIFY_GOAL,
        goals=["goal"],
        goal_attempts=[1],
        goal_evidence=["implementation"],
        goal_files=[[]],
    )

    async def reject_verification(**kwargs: object) -> None:
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        await tools[0](satisfied=False, evidence="The test still fails.")

    runner.run_phase = reject_verification  # type: ignore[method-assign]
    assert await runner._verify_goal(context, object()) is GoalState.IMPLEMENT_GOAL
    assert context.goal_evidence == ["The test still fails."]


def test_checkpoint_completion_without_handle_remains_in_memory() -> None:
    runner = _runner_without_handle()
    context = GoalContext(intent="intent")

    runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)

    assert context.completed_goal_indices == [0]
    assert context.state is GoalState.IMPLEMENT_GOAL


def test_checkpoint_completion_requires_a_supported_codec(tmp_path: Path) -> None:
    runner, context, _store, conversation = _runner_and_context(tmp_path)
    try:
        assert runner._cfg.workflow_handle is not None
        runner._cfg.workflow_handle.checkpoint_supported = False
        with pytest.raises(RuntimeError, match="codec is unavailable"):
            runner._checkpoint_completed_goal(context, 0, GoalState.IMPLEMENT_GOAL)
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_resume_rehydrates_memory_and_rejects_foreign_context() -> None:
    runner = _runner_without_handle()
    with pytest.raises(TypeError, match="GoalContext"):
        await runner.resume(object())

    memory = object()
    context = GoalContext(intent="intent", state=GoalState.COMPLETE)
    runner._cfg.session_memory = memory
    restored = await runner.resume(context)
    assert restored is context
    assert restored.shared_memory is memory


def test_goal_checkpoint_codec_rejects_unknown_state_and_tolerates_bad_scalars() -> None:
    with pytest.raises(ValueError, match="unknown goal_flow state"):
        GoalFlowWorkflow.checkpoint_context_from_payload({"state": "missing"})

    restored = GoalFlowWorkflow.checkpoint_context_from_payload(
        {
            "state": "CLARIFY",
            "phase_iteration": "not-an-int",
            "goal_index": True,
            "goal_attempts": ["3", "bad", True, 4],
            "goal_files": [["one.py"], "not-a-list"],
            "goals": ("not-a-list",),
        }
    )
    assert restored.phase_iteration == 0
    assert restored.goal_index == 0
    assert restored.goal_attempts == [3, 0, 0, 4]
    assert restored.goal_files == [["one.py"], []]
    assert restored.goals == []


def test_goal_checkpoint_codec_migrates_legacy_cursor_to_a_stable_active_id() -> None:
    restored = GoalFlowWorkflow.checkpoint_context_from_payload(
        {
            "state": "IMPLEMENT_GOAL",
            "intent": "legacy run",
            "run_id": "legacy-run",
            "goals": ["already done", "current", "later"],
            "goal_index": 1,
            "completed_goal_indices": [0],
            "goal_attempts": [1, 2, 0],
            "goal_evidence": ["verified", "implemented", ""],
            "goal_files": [["done.py"], ["current.py"], []],
        }
    )

    assert restored.goal_list_revision == 0
    assert restored.goal_records[0].status is GoalStatus.VERIFIED
    assert restored.goal_records[1].status is GoalStatus.ACTIVE
    assert restored.active_goal_id == restored.goal_records[1].goal_id
    assert restored.goal_index == 1
    assert restored.goal_records[0].goal_id.startswith("legacy-0-")
    assert restored.goal_records[1].goal_id.startswith("legacy-1-")


def test_goal_checkpoint_codec_rejects_invalid_dynamic_records_and_receipts() -> None:
    base = {
        "state": "IMPLEMENT_GOAL",
        "goal_index": 0,
        "active_goal_id": "goal-1",
        "goal_records": [
            {
                "goal_id": "goal-1",
                "text": "current",
                "status": "active",
                "attempts": 0,
                "files": [],
            }
        ],
    }
    with pytest.raises(ValueError, match="created_revision"):
        GoalFlowWorkflow.checkpoint_context_from_payload(
            {**base, "goal_records": [{**base["goal_records"][0], "created_revision": -1}]}  # type: ignore[index]
        )
    with pytest.raises(ValueError, match="receipts must be a list"):
        GoalFlowWorkflow.checkpoint_context_from_payload(
            {**base, "goal_mutation_receipts": "not-a-list"}
        )
    with pytest.raises(ValueError, match="increasing positive"):
        GoalFlowWorkflow.checkpoint_context_from_payload(
            {
                **base,
                "goal_list_revision": 2,
                "goal_mutation_receipts": [
                    {
                        "revision": 1,
                        "operation": "append",
                        "goal_id": "goal-1",
                        "index": 1,
                        "phase": "IMPLEMENT_GOAL",
                        "active_goal_id": "goal-1",
                    },
                    {
                        "revision": 1,
                        "operation": "insert",
                        "goal_id": "goal-1",
                        "index": 1,
                        "phase": "IMPLEMENT_GOAL",
                        "active_goal_id": "goal-1",
                    },
                ],
            }
        )


def test_goal_params_map_all_phase_model_overrides() -> None:
    params = GoalFlowParams(
        clarify_model="clarify",
        decide_goals_model="decide",
        implement_model="implement",
        verify_model="verify",
        summarize_model="summarize",
    )
    assert params.get_phase_models() == {
        "clarify": "clarify",
        "decide_goals": "decide",
        "implement_goal": "implement",
        "verify_goal": "verify",
        "summarize": "summarize",
    }


@pytest.mark.asyncio
async def test_append_goal_is_durable_and_does_not_change_active_goal(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        context.state = GoalState.IMPLEMENT_GOAL
        active_id = context.active_goal_id
        result = await runner._append_goal(context, "third goal")

        assert result["ok"] is True
        assert result["operation"] == "append"
        assert result["index"] == 2
        assert result["goal_count"] == 3
        assert result["goal_list_revision"] == 1
        assert context.active_goal_id == active_id
        assert context.goal_index == 0
        assert [record.text for record in context.goal_records] == [
            "first",
            "second",
            "third goal",
        ]
        assert context.goal_records[2].status is GoalStatus.PENDING
        checkpoint = store.load("goal-run")
        assert checkpoint is not None
        fields = checkpoint.context["fields"]
        assert isinstance(fields, dict)
        assert fields["goal_list_revision"] == 1
        assert fields["goal_records"][2]["text"] == "third goal"  # type: ignore[index]
        assert fields["goal_mutation_receipts"][0]["operation"] == "append"  # type: ignore[index]
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_insert_goal_preserves_active_identity_when_index_shifts(tmp_path: Path) -> None:
    runner, context, _store, conversation = _runner_and_context(tmp_path)
    try:
        context.state = GoalState.VERIFY_GOAL
        active_id = context.active_goal_id
        result = await runner._insert_goal(context, 0, "prerequisite")

        assert result["ok"] is True
        assert result["operation"] == "insert"
        assert result["index"] == 0
        assert [record.text for record in context.goal_records] == [
            "prerequisite",
            "first",
            "second",
        ]
        assert context.active_goal_id == active_id
        assert context.goal_index == 1
        assert context.goal_records[1].text == "first"
        assert context.goal_records[1].status is GoalStatus.ACTIVE
        assert context.first_pending() is not None
        assert context.first_pending()[1].text == "prerequisite"  # type: ignore[index]
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_goal_mutation_rejects_invalid_input_without_side_effects(tmp_path: Path) -> None:
    runner, context, store, conversation = _runner_and_context(tmp_path)
    try:
        context.state = GoalState.IMPLEMENT_GOAL
        before = context.goal_snapshot()
        assert (await runner._append_goal(context, "  "))["error_code"] == "goal_text_empty"
        assert (await runner._insert_goal(context, -1, "bad"))["error_code"] == "goal_index_invalid"
        assert (await runner._insert_goal(context, 99, "bad"))["error_code"] == "goal_index_invalid"
        assert context.goal_snapshot() == before
        checkpoint = store.load("goal-run")
        assert checkpoint is not None
        assert checkpoint.revision == 1
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_goal_mutation_rolls_back_when_checkpoint_fails(tmp_path: Path) -> None:
    runner, context, _store, conversation = _runner_and_context(tmp_path)
    try:
        context.state = GoalState.IMPLEMENT_GOAL
        before = context.goal_snapshot()
        handle = runner._cfg.workflow_handle
        assert handle is not None

        def fail_checkpoint(*, reason: str = ""):
            raise OSError("disk full")

        handle.save_checkpoint = fail_checkpoint  # type: ignore[method-assign]
        result = await runner._append_goal(context, "must not persist")

        assert result["ok"] is False
        assert result["error_code"] == "goal_checkpoint_unavailable"
        assert context.goal_snapshot() == before
    finally:
        conversation.close()


@pytest.mark.asyncio
async def test_goal_mutation_tools_have_minimal_schemas_and_no_transition_event() -> None:
    append_called: list[str] = []
    insert_called: list[tuple[int, str]] = []

    async def append(goal: str) -> dict[str, object]:
        append_called.append(goal)
        return {"ok": True}

    async def insert(index: int, goal: str) -> dict[str, object]:
        insert_called.append((index, goal))
        return {"ok": True}

    event = asyncio.Event()
    tools = _make_goal_mutation_tools(append, insert)
    assert [tool.__name__ for tool in tools] == ["append_goal", "insert_goal"]
    assert await tools[0](goal="new") == {"ok": True}
    assert await tools[1](index=1, goal="middle") == {"ok": True}
    assert append_called == ["new"]
    assert insert_called == [(1, "middle")]
    assert not event.is_set()

    from lauren_ai._tools import TOOL_META

    for tool in tools:
        metadata = getattr(tool, TOOL_META)
        schema = metadata.parameters["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) <= {"goal", "index"}
    assert tools[0].__name__ == "append_goal"
    assert tools[1].__name__ == "insert_goal"
    assert "index" not in getattr(tools[0], TOOL_META).parameters["input_schema"]["properties"]
    assert (
        getattr(tools[1], TOOL_META).parameters["input_schema"]["properties"]["index"]["minimum"]
        == 0
    )


@pytest.mark.asyncio
async def test_complete_workflow_rejects_pending_goal_without_setting_transition() -> None:
    event = asyncio.Event()
    data: dict[str, object] = {}
    complete = _make_summarize_tools(event, data, can_complete=lambda: False)[0]

    result = await complete(summary="premature", files=[])

    assert result["ok"] is False
    assert result["error_code"] == "pending_goals"
    assert not event.is_set()
    assert data == {}
