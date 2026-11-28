"""Which workers a worker may message across pools, and the route to them (COMM-04).

A worker used to reach only the workers of its own pool. A pool's workers are
registered with that pool's broker alone, so two pools running side by side, or a
worker and the pool it spawned, had no route to each other except the parent
relaying the words.

A worker is now addressed ``<pool_id>/<worker_id>``. A bare worker id still means
"in my pool". The directory decides whether two pools are related, refuses
anything else with a named reason, and delivers into the *recipient's* broker,
which stays the authority on its own mailbox, size limits and acknowledgement. A
message is untrusted context: it never carries a capability, an approval or a
policy across a pool boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from agenthicc.subagents.communication import CommunicationError

if TYPE_CHECKING:
    from agenthicc.subagents.communication import (
        AgentMessageEnvelope,
        PoolContinuationRegistry,
        PoolHandle,
    )

__all__ = [
    "DEFAULT_PAIR_LIMIT",
    "MAX_REACHABLE_WORKERS",
    "PoolDirectory",
    "worker_address",
]

#: Messages one worker may send another, across pools, before further ones are
#: refused. Two workers that keep replying to each other reach it and stop.
DEFAULT_PAIR_LIMIT: Final = 32
#: Workers one ``list_reachable_workers`` call returns.
MAX_REACHABLE_WORKERS: Final = 64


def worker_address(pool_id: str, worker_id: str) -> str:
    """The cross-pool address of a worker."""
    return f"{pool_id}/{worker_id}"


def _spawned_by(child: PoolHandle, pool_id: str) -> bool:
    """Whether a worker of *pool_id* started *child* or one of its ancestors.

    A pool's ``lineage`` lists the spawning workers, root first, as
    ``<pool_id>:<task_id>``.
    """
    prefix = f"{pool_id}:"
    return any(owner.startswith(prefix) for owner in child.lineage)


class PoolDirectory:
    """The pools of one conversation a worker may reach, and the delivery to them."""

    def __init__(
        self,
        registry: PoolContinuationRegistry,
        conversation_id: str,
        *,
        pair_limit: int = DEFAULT_PAIR_LIMIT,
    ) -> None:
        if pair_limit < 1:
            raise CommunicationError("the pair limit must be positive")
        self._registry = registry
        self.conversation_id = conversation_id
        self.pair_limit = pair_limit
        self._sent: dict[tuple[str, str], int] = {}

    @staticmethod
    def relation(own: PoolHandle, other: PoolHandle) -> str | None:
        """How *other*'s pool relates to *own*'s, or ``None`` when it is not reachable.

        ``same_pool``; ``sibling_pool`` (started by the same worker, or both at the
        top level); ``ancestor_pool`` (*other* started *own*, directly or not);
        ``descendant_pool`` (*own* started *other*). Pools of another conversation
        or another workflow phase are never related.
        """
        if own.conversation_id != other.conversation_id or own.phase_owner != other.phase_owner:
            return None
        if own.pool_id == other.pool_id:
            return "same_pool"
        if own.lineage == other.lineage:
            return "sibling_pool"
        if _spawned_by(own, other.pool_id):
            return "ancestor_pool"
        if _spawned_by(other, own.pool_id):
            return "descendant_pool"
        return None

    def reachable(self, pool_id: str, worker_id: str) -> list[dict[str, object]]:
        """The running workers *worker_id* of *pool_id* may message, nearest pools first."""
        own = self._registry.get(pool_id)
        if own is None or own.conversation_id != self.conversation_id:
            return []
        rows: list[tuple[int, dict[str, object]]] = []
        order = {"same_pool": 0, "sibling_pool": 1, "descendant_pool": 2, "ancestor_pool": 3}
        for handle in self._registry.handles():
            relation = self.relation(own, handle)
            if relation is None or handle.task.done():
                continue
            labels = (
                {worker.worker_id: worker.label for worker in handle.pool.state.workers}
                if handle.pool is not None
                else {}
            )
            for member in sorted(handle.broker.members - {"main"}):
                if handle.pool_id == pool_id and member == worker_id:
                    continue
                rows.append(
                    (
                        order[relation],
                        {
                            "address": worker_address(handle.pool_id, member),
                            "pool_id": handle.pool_id,
                            "worker_id": member,
                            "label": labels.get(member, ""),
                            "relation": relation,
                        },
                    )
                )
        rows.sort(key=lambda row: row[0])
        return [row for _, row in rows[:MAX_REACHABLE_WORKERS]]

    async def send(
        self,
        *,
        sender_pool: str,
        sender_worker: str,
        recipient: str,
        message: str,
        kind: str = "information",
        requires_response: bool = False,
        reply_to: str = "",
    ) -> AgentMessageEnvelope:
        """Deliver *message* from one worker to the worker at *recipient* in a related pool.

        Raises :class:`CommunicationError` whose text begins with the rule that
        refused it: ``unknown_pool``, ``unrelated_pool``, ``recipient_finished``,
        ``unknown_worker`` or ``rate_limited`` (the broker adds ``mailbox is full``
        and its size limits).
        """
        pool_id, separator, worker_id = recipient.partition("/")
        if not separator or not pool_id or not worker_id:
            raise CommunicationError(
                "unknown_worker: a cross-pool address is <pool_id>/<worker_id>; "
                "list_reachable_workers shows the ones you may use"
            )
        own = self._registry.get(sender_pool)
        other = self._registry.get(pool_id)
        if (
            own is None
            or other is None
            or other.conversation_id != self.conversation_id
            or own.conversation_id != self.conversation_id
        ):
            raise CommunicationError(f"unknown_pool: no pool {pool_id!r} in this conversation")
        if self.relation(own, other) is None:
            raise CommunicationError(
                f"unrelated_pool: pool {pool_id!r} is not the same pool, a sibling, an ancestor "
                "or a descendant of yours"
            )
        if other.task.done():
            raise CommunicationError(f"recipient_finished: pool {pool_id!r} has finished")
        if worker_id not in other.broker.members or worker_id == "main":
            raise CommunicationError(
                f"unknown_worker: {worker_id!r} is not a running worker of pool {pool_id!r}"
            )
        sender = worker_address(sender_pool, sender_worker)
        pair = (sender, recipient)
        if self._sent.get(pair, 0) >= self.pair_limit:
            raise CommunicationError(
                f"rate_limited: at most {self.pair_limit} messages from {sender} to {recipient}"
            )
        envelope = await other.broker.send_from_pool(
            sender=sender,
            recipient=worker_id,
            kind=kind,
            payload=message,
            requires_response=requires_response,
            reply_to=reply_to,
        )
        self._sent[pair] = self._sent.get(pair, 0) + 1
        return envelope
