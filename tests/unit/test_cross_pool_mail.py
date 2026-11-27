"""COMM-04: a worker reaches the workers of related pools by ``<pool_id>/<worker_id>``.

Pools are real brokers registered in a real registry, with real lineage. Only the
pool's task is a stand-in (a pending or finished future), because routing never
looks inside it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agenthicc.subagents.communication import (
    AgentMessageBroker,
    CommunicationError,
    PoolContinuationRegistry,
    PoolHandle,
    make_child_communication_tools,
)
from agenthicc.subagents.directory import PoolDirectory, worker_address
from agenthicc.subagents.phase import PhaseOwner

pytestmark = pytest.mark.unit

CONVERSATION = "conversation"


class World:
    """Pools in one conversation, each with a broker, a handle and running workers."""

    def __init__(self, *, pair_limit: int = 32) -> None:
        self.registry = PoolContinuationRegistry()
        self.directory = PoolDirectory(self.registry, CONVERSATION, pair_limit=pair_limit)
        self.tasks: dict[str, asyncio.Task[object]] = {}
        self.brokers: dict[str, AgentMessageBroker] = {}

    def pool(
        self,
        pool_id: str,
        workers: tuple[str, ...],
        *,
        lineage: tuple[str, ...] = (),
        conversation: str = CONVERSATION,
        phase_owner: PhaseOwner | None = None,
    ) -> AgentMessageBroker:
        broker = AgentMessageBroker(
            conversation_id=conversation, parent_run_id="run", pool_id=pool_id
        )
        for worker in workers:
            broker.register_worker(worker)
        broker.directory = self.directory
        task: asyncio.Task[object] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
        self.tasks[pool_id] = task
        self.brokers[pool_id] = broker
        self.registry.register(
            PoolHandle(
                pool_id=pool_id,
                conversation_id=conversation,
                broker=broker,
                task=task,
                lineage=lineage,
                phase_owner=phase_owner,
            )
        )
        return broker

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def handle(self, pool_id: str) -> PoolHandle:
        handle = self.registry.get(pool_id)
        assert handle is not None
        return handle


@pytest.fixture
async def world() -> AsyncIterator[World]:
    instance = World()
    yield instance
    await instance.close()


async def _send(
    directory: PoolDirectory,
    sender: tuple[str, str],
    recipient: str,
    text: str = "hello",
    **kw: object,
) -> str:
    envelope = await directory.send(
        sender_pool=sender[0],
        sender_worker=sender[1],
        recipient=recipient,
        message=text,
        **kw,  # type: ignore[arg-type]
    )
    return envelope.message_id


# ── who is related to whom ───────────────────────────────────────────────────


async def test_pools_are_related_by_their_lineage(world: World) -> None:
    world.pool("pool-a", ("a1",))
    world.pool("pool-b", ("b1",))  # a second top-level pool: a sibling of pool-a
    world.pool("pool-c", ("c1",), lineage=("pool-a:a1",))  # started by a1
    world.pool("pool-d", ("d1",), lineage=("pool-a:a1", "pool-c:c1"))  # started by c1
    world.pool("pool-e", ("e1",), lineage=("pool-b:b1",))  # started by b1, in another tree
    relation = lambda own, other: PoolDirectory.relation(world.handle(own), world.handle(other))  # noqa: E731

    assert relation("pool-a", "pool-a") == "same_pool"
    assert relation("pool-a", "pool-b") == "sibling_pool"
    assert relation("pool-a", "pool-c") == "descendant_pool"
    assert relation("pool-c", "pool-a") == "ancestor_pool"
    assert relation("pool-d", "pool-a") == "ancestor_pool"  # a grandchild reaches its grandparent
    assert relation("pool-a", "pool-d") == "descendant_pool"
    assert relation("pool-d", "pool-c") == "ancestor_pool"
    # Workers started by one worker are siblings; another tree's pools are strangers.
    world.pool("pool-f", ("f1",), lineage=("pool-a:a1",))
    assert relation("pool-c", "pool-f") == "sibling_pool"
    assert relation("pool-c", "pool-b") is None
    assert relation("pool-e", "pool-a") is None
    assert relation("pool-e", "pool-c") is None


async def test_another_conversation_or_phase_is_never_related(world: World) -> None:
    world.pool("pool-a", ("a1",))
    world.pool("pool-x", ("x1",), conversation="elsewhere")
    owner = PhaseOwner("conversation", "run", "frame", "build", "invocation")
    world.pool("pool-p", ("p1",), phase_owner=owner)

    assert PoolDirectory.relation(world.handle("pool-a"), world.handle("pool-x")) is None
    assert PoolDirectory.relation(world.handle("pool-a"), world.handle("pool-p")) is None


# ── delivery ─────────────────────────────────────────────────────────────────


async def test_a_message_reaches_a_worker_of_a_sibling_pool_and_the_reply_comes_back(
    world: World,
) -> None:
    world.pool("pool-a", ("a1",))
    broker_b = world.pool("pool-b", ("b1",))

    sent = await _send(
        world.directory, ("pool-a", "a1"), "pool-b/b1", "tests need v2", kind="instruction"
    )

    (mail,) = await broker_b.peek("b1")
    assert mail["message_id"] == sent
    assert mail["sender"] == worker_address("pool-a", "a1")
    assert mail["recipient"] == "b1" and mail["kind"] == "instruction"
    assert mail["payload"] == {"text": "tests need v2"}
    assert await broker_b.undelivered_count("b1") == 1

    # The recipient answers the way it was written to, and the answer carries reply_to.
    reply = await _send(world.directory, ("pool-b", "b1"), "pool-a/a1", "done", reply_to=sent)
    (answer,) = await world.brokers["pool-a"].peek("a1")
    assert answer["message_id"] == reply and answer["reply_to"] == sent
    assert answer["sender"] == "pool-b/b1"

    # It is ordinary mail: acknowledging it retires it.
    await broker_b.acknowledge("b1", sent)
    assert await broker_b.undelivered_count("b1") == 0


async def test_a_grandchild_reaches_its_grandparent_across_two_levels(world: World) -> None:
    broker_a = world.pool("pool-a", ("a1",))
    world.pool("pool-c", ("c1",), lineage=("pool-a:a1",))
    world.pool("pool-d", ("d1",), lineage=("pool-a:a1", "pool-c:c1"))

    await _send(
        world.directory, ("pool-d", "d1"), "pool-a/a1", "which API version?", requires_response=True
    )

    (mail,) = await broker_a.peek("a1")
    assert mail["sender"] == "pool-d/d1" and mail["requires_response"] is True
    # And the grandparent can answer down the same route.
    await _send(world.directory, ("pool-a", "a1"), "pool-d/d1", "v2")
    assert len(await world.brokers["pool-d"].peek("d1")) == 1


# ── refusals name the rule ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("recipient", "rule"),
    [
        ("pool-zzz/w", "unknown_pool"),
        ("pool-e/e1", "unrelated_pool"),
        ("pool-b/ghost", "unknown_worker"),
        ("pool-b/main", "unknown_worker"),
        ("no-slash", "unknown_worker"),
        ("pool-b/", "unknown_worker"),
    ],
)
async def test_a_refused_message_says_which_rule_refused_it(
    world: World, recipient: str, rule: str
) -> None:
    world.pool("pool-a", ("a1",))
    world.pool("pool-b", ("b1",))
    world.pool("pool-e", ("e1",), lineage=("pool-x:x1",))

    with pytest.raises(CommunicationError, match=f"^{rule}"):
        await _send(world.directory, ("pool-a", "a1"), recipient)

    assert await world.brokers["pool-b"].undelivered_count("b1") == 0


async def test_a_finished_pool_refuses_mail(world: World) -> None:
    world.pool("pool-a", ("a1",))
    world.pool("pool-b", ("b1",))
    world.tasks["pool-b"].cancel()
    await asyncio.gather(world.tasks["pool-b"], return_exceptions=True)

    with pytest.raises(CommunicationError, match="^recipient_finished"):
        await _send(world.directory, ("pool-a", "a1"), "pool-b/b1")


async def test_two_workers_that_keep_replying_are_stopped_by_the_pair_limit() -> None:
    world = World(pair_limit=3)
    try:
        world.pool("pool-a", ("a1",))
        world.pool("pool-b", ("b1",))
        for _ in range(3):
            await _send(world.directory, ("pool-a", "a1"), "pool-b/b1")
        with pytest.raises(CommunicationError, match="^rate_limited"):
            await _send(world.directory, ("pool-a", "a1"), "pool-b/b1")
        # The limit is per ordered pair: the other direction is a different pair.
        await _send(world.directory, ("pool-b", "b1"), "pool-a/a1")
    finally:
        await world.close()


async def test_a_full_mailbox_refuses_with_the_brokers_own_limit(world: World) -> None:
    world.pool("pool-a", ("a1",))
    broker_b = AgentMessageBroker(
        conversation_id=CONVERSATION, parent_run_id="run", pool_id="pool-b", max_mailbox_messages=1
    )
    broker_b.register_worker("b1")
    broker_b.directory = world.directory
    task: asyncio.Task[object] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
    world.tasks["pool-b"] = task
    world.registry.register(
        PoolHandle(pool_id="pool-b", conversation_id=CONVERSATION, broker=broker_b, task=task)
    )

    await _send(world.directory, ("pool-a", "a1"), "pool-b/b1")
    with pytest.raises(CommunicationError, match="mailbox is full"):
        await _send(world.directory, ("pool-a", "a1"), "pool-b/b1")


async def test_a_foreign_worker_can_write_to_a_worker_only(world: World) -> None:
    broker_b = world.pool("pool-b", ("b1",))

    for recipient in ("main", "pool"):
        with pytest.raises(CommunicationError, match="worker only"):
            await broker_b.send_from_pool(
                sender="pool-a/a1", recipient=recipient, kind="information", payload="x"
            )
    with pytest.raises(CommunicationError, match="pool-qualified"):
        await broker_b.send_from_pool(sender="a1", recipient="b1", kind="information", payload="x")
    # And it is never a member: broadcasts and questions do not reach it.
    assert "pool-a/a1" not in broker_b.members


# ── the tools a worker has ───────────────────────────────────────────────────


def _tool(tools: list[object], name: str):  # type: ignore[no-untyped-def]
    return next(tool for tool in tools if getattr(tool, "__name__", "") == name)


async def test_a_worker_lists_who_it_can_reach_nearest_first_and_writes_to_them(
    world: World,
) -> None:
    world.pool("pool-a", ("a1", "a2"))
    world.pool("pool-b", ("b1",))
    world.pool("pool-c", ("c1",), lineage=("pool-a:a1",))
    world.pool("pool-e", ("e1",), lineage=("pool-x:x1",))
    tools = make_child_communication_tools(world.brokers["pool-a"], "a1")

    listed = await _tool(tools, "list_reachable_workers")()

    assert [(w["address"], w["relation"]) for w in listed["workers"]] == [
        ("pool-a/a2", "same_pool"),
        ("pool-b/b1", "sibling_pool"),
        ("pool-c/c1", "descendant_pool"),
    ]  # not itself, not the stranger's pool

    send = _tool(tools, "send_peer_message")
    assert (await send("pool-b/b1", "over to you", requires_response=True))["status"] == "sent"
    assert (await send("a2", "same pool, bare id"))["status"] == "sent"  # unchanged
    (mail,) = await world.brokers["pool-b"].peek("b1")
    assert mail["sender"] == "pool-a/a1" and mail["requires_response"] is True
    (local,) = await world.brokers["pool-a"].peek("a2")
    assert local["sender"] == "a1"


async def test_a_pool_nobody_can_see_has_no_cross_pool_route() -> None:
    broker = AgentMessageBroker(conversation_id=CONVERSATION, parent_run_id="run", pool_id="solo")
    broker.register_worker("w1")
    broker.register_worker("w2")
    tools = make_child_communication_tools(broker, "w1")

    listed = await _tool(tools, "list_reachable_workers")()
    assert [w["address"] for w in listed["workers"]] == ["w2"]
    with pytest.raises(CommunicationError, match="^unknown_pool"):
        await _tool(tools, "send_peer_message")("other/x", "hi")


async def test_a_retried_workers_broker_keeps_the_route(world: World) -> None:
    world.pool("pool-a", ("a1",))

    retried = world.brokers["pool-a"].for_retry("a1")

    assert retried.directory is world.directory
