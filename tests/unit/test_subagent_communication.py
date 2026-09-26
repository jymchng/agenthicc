"""Unit coverage for PRD-197 subagent communication and policy snapshots."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.memory.journal import ConversationJournal
from agenthicc.subagents.communication import (
    AgentMessageBroker,
    CommunicationError,
    make_child_communication_tools,
)
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _policy(mode_name: str = "Safe") -> SubagentExecutionPolicy:
    parent = AppState.create()
    parent.active_mode.set(build_default_registry().get(mode_name))
    policy = SubagentExecutionPolicy.from_app_state(
        parent,
        visible_tool_names=frozenset({"read_file", "write_file"}),
    )
    assert policy is not None
    return policy


@pytest.mark.asyncio
async def test_parent_and_peer_messages_are_ordered_and_acknowledgeable() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-1",
        parent_run_id="run-1",
        pool_id="pool-1",
        policy_revision=_policy().policy_revision,
    )
    broker.register_worker("worker-a")
    broker.register_worker("worker-b")

    first = await broker.send(
        sender="main",
        recipient="worker-a",
        kind="instruction",
        payload={"text": "Inspect the API."},
    )
    second = await broker.send(
        sender="main",
        recipient="worker-a",
        kind="instruction",
        payload={"text": "Report the exact route."},
    )

    messages = await broker.poll("worker-a", limit=10)
    assert [item["message_id"] for item in messages] == [first.message_id, second.message_id]
    assert [item["sequence"] for item in messages] == [1, 2]
    assert (await broker.acknowledge("worker-a", first.message_id))["state"] == "acknowledged"

    broadcast = await broker.send(
        sender="main",
        recipient="pool",
        kind="information",
        payload="All workers should report their findings.",
    )
    assert (await broker.poll("worker-a"))[0]["message_id"] == broadcast.message_id
    assert (await broker.poll("worker-b"))[0]["message_id"] == broadcast.message_id

    with pytest.raises(CommunicationError, match="recipient"):
        await broker.send(
            sender="worker-a",
            recipient="other-pool-worker",
            kind="information",
            payload="cross-pool access",
        )


@pytest.mark.asyncio
async def test_question_round_trip_wakes_only_the_requesting_worker() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-2",
        parent_run_id="run-2",
        pool_id="pool-2",
    )
    broker.register_worker("worker-a")
    broker.register_worker("worker-b")

    question = await broker.open_question(
        sender="worker-a",
        recipient="main",
        question="Which API version should I target?",
        context="The repository contains v1 and v2 clients.",
    )
    assert question["status"] == "pending"
    assert broker.pending_questions(recipient="main")[0]["question"] == (
        "Which API version should I target?"
    )

    waiter = asyncio.create_task(broker.wait_for_answer(str(question["question_id"])))
    await asyncio.sleep(0)
    answered = await broker.answer_question(
        answerer="main",
        question_id=str(question["question_id"]),
        answer="Use v2; the v1 client is deprecated.",
    )
    assert answered["status"] == "answered"
    assert await waiter == "Use v2; the v1 client is deprecated."

    # An idempotent duplicate answer returns terminal state and cannot wake a
    # second waiter or alter the original answer.
    duplicate = await broker.answer_question(
        answerer="main",
        question_id=str(question["question_id"]),
        answer="A different answer",
    )
    assert duplicate["answer"] == "Use v2; the v1 client is deprecated."


@pytest.mark.asyncio
async def test_peer_question_requires_same_pool_membership() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-3",
        parent_run_id="run-3",
        pool_id="pool-3",
    )
    broker.register_worker("worker-a")
    broker.register_worker("worker-b")
    question = await broker.open_question(
        sender="worker-a",
        recipient="worker-b",
        question="What did you find?",
    )
    with pytest.raises(CommunicationError, match="question recipient"):
        await broker.answer_question(
            answerer="worker-a",
            question_id=str(question["question_id"]),
            answer="I cannot answer my own question.",
        )
    result = await broker.answer_question(
        answerer="worker-b",
        question_id=str(question["question_id"]),
        answer="The route is /v2/items.",
    )
    assert result["status"] == "answered"


def test_journal_rehydrates_unconsumed_messages_and_questions(tmp_path) -> None:
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path)

    async def write_records() -> None:
        broker = AgentMessageBroker(
            conversation_id="conversation-4",
            parent_run_id="run-4",
            pool_id="pool-4",
            journal=journal,
        )
        broker.register_worker("worker-a")
        await broker.open_question(
            sender="worker-a",
            recipient="main",
            question="Need the target language.",
        )

    asyncio.run(write_records())
    rehydrated = AgentMessageBroker.rehydrate(
        conversation_id="conversation-4",
        parent_run_id="run-4",
        pool_id="pool-4",
        journal=journal,
        active_workers=frozenset({"worker-a"}),
    )
    pending = rehydrated.pending_questions(recipient="main")
    assert len(pending) == 1
    assert pending[0]["question"] == "Need the target language."


def test_rehydrate_marks_questions_from_missing_workers_orphaned() -> None:
    events = [
        {
            "pool_id": "pool-orphan",
            "event_kind": "agent_pool_member",
            "payload": {"worker_id": "worker-a", "state": "registered"},
        },
        {
            "pool_id": "pool-orphan",
            "event_kind": "agent_message_sent",
            "payload": {
                "message_id": "question-orphan",
                "conversation_id": "conversation-orphan",
                "parent_run_id": "run-orphan",
                "pool_id": "pool-orphan",
                "sender": "worker-a",
                "recipient": "main",
                "kind": "clarification_request",
                "payload": {"question": "Need input."},
                "correlation_id": "question-orphan",
                "sequence": 1,
                "created_at": 1.0,
                "expires_at": 9999999999.0,
                "requires_response": True,
            },
        },
        {
            "pool_id": "pool-orphan",
            "event_kind": "question_wait_started",
            "payload": {"question_id": "question-orphan"},
        },
    ]
    broker = AgentMessageBroker.rehydrate(
        conversation_id="conversation-orphan",
        parent_run_id="run-orphan",
        pool_id="pool-orphan",
        events=events,
    )
    assert broker.pending_questions(recipient="main") == []


@pytest.mark.asyncio
async def test_send_with_same_correlation_and_payload_is_idempotent() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-dedupe",
        parent_run_id="run-dedupe",
        pool_id="pool-dedupe",
    )
    broker.register_worker("worker-a")
    first = await broker.send(
        sender="main",
        recipient="worker-a",
        kind="instruction",
        payload={"text": "Use the stable route."},
        correlation_id="instruction-1",
    )
    duplicate = await broker.send(
        sender="main",
        recipient="worker-a",
        kind="instruction",
        payload={"text": "Use the stable route."},
        correlation_id="instruction-1",
    )
    assert duplicate.message_id == first.message_id
    assert len(await broker.poll("worker-a")) == 1


@pytest.mark.asyncio
async def test_child_communication_tools_are_bound_to_worker() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-5",
        parent_run_id="run-5",
        pool_id="pool-5",
    )
    broker.register_worker("worker-a")
    tools = make_child_communication_tools(broker, "worker-a")
    names = {getattr(tool, "__name__", "") for tool in tools}
    assert {
        "send_parent_message",
        "ask_parent",
        "poll_agent_messages",
        "send_peer_message",
        "ask_peer",
        "answer_peer",
    } <= names

    send_parent = next(tool for tool in tools if tool.__name__ == "send_parent_message")
    result = await send_parent("bounded finding")
    assert result["ok"] is True
    parent_messages = await broker.poll("main")
    assert parent_messages[0]["payload"] == {"text": "bounded finding"}


def test_policy_revision_is_stable_and_mode_specific() -> None:
    safe = _policy("Safe")
    safe_again = _policy("Safe")
    plan = _policy("Plan")
    assert safe.policy_revision == safe_again.policy_revision
    assert safe.policy_revision != plan.policy_revision
    assert "write" in safe.approval_required
    assert "write" in plan.blocked_capabilities
    assert "INHERITED RUNTIME POLICY" in plan.prompt_section()
    assert "PLAN MODE" in plan.system_prompt_suffix
