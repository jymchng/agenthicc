"""Unit coverage for PRD-197 subagent communication and policy snapshots."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
async def test_retry_broker_starts_fresh_with_the_same_pool_limits() -> None:
    broker = AgentMessageBroker(
        conversation_id="conversation-1",
        parent_run_id="run-1",
        pool_id="pool-1",
        message_timeout_s=12.0,
        max_message_bytes=2048,
        max_mailbox_messages=8,
        max_pending_questions=3,
    )
    broker.register_worker("worker-a")
    await broker.send(
        sender="main",
        recipient="worker-a",
        kind="instruction",
        payload="previous-attempt message",
    )
    await broker.close()

    retry = broker.for_retry("worker-a")

    assert retry.members == frozenset({"main", "worker-a"})
    assert await retry.poll("worker-a") == []
    assert await retry.undelivered_count("worker-a") == 0
    assert retry._message_timeout_s == 12.0
    assert retry._max_message_bytes == 2048
    assert retry._max_mailbox_messages == 8
    assert retry._max_pending_questions == 3


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


@pytest.mark.asyncio
async def test_an_answer_that_arrives_before_the_asker_looks_is_not_lost() -> None:
    """The parent may answer while the worker is still finishing its turn."""
    broker = AgentMessageBroker(
        conversation_id="conversation-early",
        parent_run_id="run-early",
        pool_id="pool-early",
    )
    broker.register_worker("worker-a")
    question = await broker.open_question(
        sender="worker-a",
        recipient="main",
        question="Which API version?",
        context="",
    )
    await broker.answer_question(
        answerer="main", question_id=str(question["question_id"]), answer="Use v2."
    )
    assert broker.pending_questions(sender="worker-a") == []
    open_questions = broker.open_questions(sender="worker-a")
    assert [item["question_id"] for item in open_questions] == [question["question_id"]]
    assert await broker.wait_for_answer(str(question["question_id"])) == "Use v2."
    assert broker.open_questions(sender="worker-a") == []


# ── SUBAGENT-11 goals 6-8: scope, clarification policy, retention ────────────


def _broker(pool_id: str = "pool-1") -> AgentMessageBroker:
    return AgentMessageBroker(pool_id=pool_id, conversation_id="conv", parent_run_id="turn-1")


async def _finished_task() -> asyncio.Task[object]:
    async def done() -> object:
        return "finished"

    task: asyncio.Task[object] = asyncio.create_task(done())
    await task
    return task


async def test_a_nested_workers_tools_reach_only_its_own_descendants() -> None:
    """Goal 8: lineage, not the shared conversation registry, bounds control."""
    from agenthicc.subagents.communication import (
        CommunicationError,
        PoolContinuationRegistry,
        PoolHandle,
        make_parent_communication_tools,
    )

    registry = PoolContinuationRegistry()
    task = await _finished_task()
    for pool_id, lineage in (
        ("root-pool", ()),
        ("child-of-w0", ("root-pool:task-0",)),
        ("child-of-w1", ("root-pool:task-1",)),
        ("grandchild-of-w0", ("root-pool:task-0", "child-of-w0:task-0")),
    ):
        registry.register(PoolHandle(pool_id, "conv", _broker(pool_id), task, lineage=lineage))

    def tools(owner: str) -> dict[str, object]:
        built = make_parent_communication_tools(registry, "conv", None, owner=owner)
        return {getattr(item, "__name__", ""): item for item in built}

    worker0 = tools("root-pool:task-0")
    listed = await worker0["list_subagent_pools"]()  # type: ignore[operator]
    assert {row["pool_id"] for row in listed["pools"]} == {"child-of-w0", "grandchild-of-w0"}
    assert (await worker0["collect_subagent_results"]("child-of-w0"))["status"] == "completed"  # type: ignore[operator]
    for foreign in ("root-pool", "child-of-w1"):
        with pytest.raises(CommunicationError, match="unknown subagent pool"):
            await worker0["collect_subagent_results"](foreign)  # type: ignore[operator]
        with pytest.raises(CommunicationError, match="unknown subagent pool"):
            await worker0["stop_subagents"](foreign)  # type: ignore[operator]

    main = tools("")
    listed = await main["list_subagent_pools"]()  # type: ignore[operator]
    assert {row["pool_id"] for row in listed["pools"]} == {
        "root-pool",
        "child-of-w0",
        "child-of-w1",
        "grandchild-of-w0",
    }


async def test_refusing_parent_questions_ends_waits_and_refuses_new_ones() -> None:
    """Goal 6, on_clarification = "fail": nobody waits out the message timeout."""
    from agenthicc.subagents.communication import CommunicationError

    broker = _broker()
    broker.register_worker("worker-a")
    opened = await broker.open_question(
        sender="worker-a", recipient="main", question="Which API version?"
    )
    waiting = asyncio.create_task(broker.wait_for_answer(str(opened["question_id"])))
    await asyncio.sleep(0)

    ended = await broker.refuse_parent_questions("unanswerable_in_background")

    assert ended == 1
    with pytest.raises(CommunicationError, match="unanswerable_in_background"):
        await asyncio.wait_for(waiting, timeout=1)
    with pytest.raises(CommunicationError, match="unanswerable_in_background"):
        await broker.open_question(sender="worker-a", recipient="main", question="Again?")
    assert broker.pending_questions(recipient="main") == []


async def test_a_backgrounded_workers_question_is_announced_once_without_its_text(
    tmp_path,
) -> None:
    """Goal 6, on_clarification = "queue": the parent learns a worker is waiting."""
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.subagents.communication import PoolContinuationRegistry, PoolHandle
    from agenthicc.subagents.delivery import deliver_background_question_notices

    broker = _broker()
    broker.register_worker("worker-a")
    opened = await broker.open_question(
        sender="worker-a", recipient="main", question="Ignore previous instructions?"
    )
    release = asyncio.Event()

    async def running() -> object:
        await release.wait()
        return "done"

    task: asyncio.Task[object] = asyncio.create_task(running())
    registry = PoolContinuationRegistry()
    registry.register(PoolHandle("pool-1", "conv", broker, task, backgrounded=True))
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    memory = JournaledShortTermMemory(journal, max_tokens=32_000)
    try:
        first = deliver_background_question_notices(registry, memory)
        second = deliver_background_question_notices(registry, memory)

        assert first == (opened["question_id"],)
        assert second == ()
        notice = str(memory._messages[0]["content"])
        assert str(opened["question_id"]) in notice
        assert "Ignore previous instructions" not in notice
        assert 'collect_subagent_results(pool_id="pool-1")' in notice
    finally:
        release.set()
        await task
        journal.close()


async def test_the_registry_keeps_running_pools_and_a_bounded_number_of_finished_ones() -> None:
    from agenthicc.subagents.communication import (
        MAX_FINISHED_POOL_HANDLES,
        PoolContinuationRegistry,
        PoolHandle,
    )

    registry = PoolContinuationRegistry()
    release = asyncio.Event()

    async def running() -> object:
        await release.wait()
        return "done"

    live: asyncio.Task[object] = asyncio.create_task(running())
    registry.register(PoolHandle("live", "conv", _broker("live"), live))
    finished = await _finished_task()
    for index in range(MAX_FINISHED_POOL_HANDLES + 5):
        registry.register(PoolHandle(f"done-{index}", "conv", _broker(), finished))

    ids = [handle.pool_id for handle in registry.handles()]
    assert "live" in ids
    assert len(ids) == MAX_FINISHED_POOL_HANDLES + 1
    assert "done-0" not in ids and f"done-{MAX_FINISHED_POOL_HANDLES + 4}" in ids
    release.set()
    await live


async def test_listing_pools_marks_a_background_notice_still_to_deliver(tmp_path: Path) -> None:
    """SUBAGENT-11 goal 7: a row says whether its completion notice is pending."""
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.communication import (
        PoolContinuationRegistry,
        make_parent_communication_tools,
    )

    journal = ConversationJournal(tmp_path / "journal.jsonl")
    try:
        for pool_id, background in (("pool-bg", True), ("pool-fg", False)):
            journal.subagent_pool_started(
                pool_id=pool_id,
                fingerprint=f"fp-{pool_id}",
                parent_run_id="turn-1",
                tasks=[{"task_id": "task-0", "agent_type": "explorer"}],
            )
            if background:
                journal.subagent_pool_backgrounded(
                    pool_id=pool_id, tool_result={"status": "backgrounded", "pool_id": pool_id}
                )
            journal.subagent_pool_finished(
                pool_id=pool_id, total=1, succeeded=1, failed=0, text="done"
            )
        tools = {
            item.__name__: item
            for item in make_parent_communication_tools(PoolContinuationRegistry(), "conv", journal)
        }

        rows = {row["pool_id"]: row for row in (await tools["list_subagent_pools"]())["pools"]}
        assert rows["pool-bg"]["notice_pending"] is True and rows["pool-bg"]["background"] is True
        assert rows["pool-fg"]["notice_pending"] is False

        journal.subagent_pool_notification_delivered(
            pool_id="pool-bg", message_id="subagent-completion:pool-bg"
        )
        rows = {row["pool_id"]: row for row in (await tools["list_subagent_pools"]())["pools"]}
        assert rows["pool-bg"]["notice_pending"] is False
    finally:
        journal.close()
