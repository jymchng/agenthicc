"""Ownership and committed collection for pools admitted by workflow phases."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agenthicc.tools.approval import FailClosedGate

if TYPE_CHECKING:
    from lauren_ai._memory import ToolExchange
    from lauren_ai._tools import ToolResult
    from lauren_ai._tools._hooks import ToolCallContext

    from agenthicc.memory.journal import ConversationJournal

TERMINAL_POOL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
POOL_TOOLS = frozenset({"spawn_subagents", "collect_subagent_results"})


@dataclass(frozen=True, slots=True)
class PhaseOwner:
    conversation_id: str
    run_id: str
    frame_id: str
    phase: str
    invocation: str

    def record(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "frame_id": self.frame_id,
            "phase": self.phase,
            "invocation": self.invocation,
        }


def phase_pool_matches(record: Mapping[str, object], owner: PhaseOwner | None) -> bool:
    return record.get("phase_owner") == (owner.record() if owner is not None else None)


@dataclass(slots=True)
class PhasePools:
    """Guard a provider batch using the owning phase's durable dependencies."""

    owner: PhaseOwner
    journal: ConversationJournal
    transitions: frozenset[str]
    available: bool = False
    batch_has_pool_call: bool = False
    transition_started: bool = False

    def outstanding(self) -> dict[str, dict[str, object]]:
        collected = self.journal.collected_phase_pools(self.owner.invocation)
        return {
            pool_id: record
            for record in self.journal.fold_subagent_pool_lifecycle()
            if phase_pool_matches(record, self.owner)
            and isinstance(pool_id := record.get("pool_id"), str)
            and pool_id not in collected
        }

    def receipts(self, exchange: ToolExchange, results: Sequence[ToolResult]) -> dict[str, str]:
        pool_calls = {call.tool_use_id for call in exchange.calls if call.name in POOL_TOOLS}
        if not pool_calls:
            return {}
        outstanding = self.outstanding()
        found: dict[str, str] = {}
        for result in results:
            if result.tool_use_id not in pool_calls or not isinstance(result.content, str):
                continue
            try:
                payload: object = json.loads(result.content)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            pool_id = payload.get("pool_id")
            status = payload.get("status")
            if (
                isinstance(pool_id, str)
                and isinstance(status, str)
                and status in TERMINAL_POOL_STATES
                and outstanding.get(pool_id, {}).get("status") in TERMINAL_POOL_STATES
            ):
                found[pool_id] = self.owner.invocation
        return found


_phase_pools: ContextVar[PhasePools | None] = ContextVar("agenthicc_phase_pools", default=None)


def current_phase_pools() -> PhasePools | None:
    return _phase_pools.get()


def current_phase_owner() -> PhaseOwner | None:
    scope = current_phase_pools()
    return scope.owner if scope is not None and scope.available else None


@contextmanager
def bind_phase_pools(scope: PhasePools | None) -> Iterator[None]:
    token = _phase_pools.set(scope)
    try:
        yield
    finally:
        _phase_pools.reset(token)


class PhasePoolGate(FailClosedGate):
    """Refuse transitions before approval or a phase callback changes state."""

    def __init__(self, scope: PhasePools) -> None:
        self._scope = scope

    async def decide(self, ctx: ToolCallContext) -> object:
        from lauren_ai._tools._hooks import BeforeToolHookDecision

        if ctx.tool_name == "spawn_subagents" and self._scope.transition_started:
            return self.refuse(
                ctx,
                {
                    "ok": False,
                    "error_code": "phase_transition_started",
                    "error": "The phase transition is already scheduled. No pool was started.",
                },
            )
        if ctx.tool_name in self._scope.transitions and (
            (self._scope.available and self._scope.batch_has_pool_call) or self._scope.outstanding()
        ):
            return self.refuse(
                ctx,
                {
                    "ok": False,
                    "error_code": "subagents_pending",
                    "error": (
                        "Collect all terminal subagent pools in an earlier tool batch "
                        "before retrying the workflow transition."
                    ),
                },
            )
        return BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result: object, ctx: ToolCallContext) -> object:
        if (
            ctx.tool_name in self._scope.transitions
            and ctx.tool_name != "request_plan_approval"
            and isinstance(result, dict)
            and (result.get("ok") is True or result.get("status") == "handoff_accepted")
        ):
            self._scope.transition_started = True
        return await super().after_tool_call(result, ctx)
