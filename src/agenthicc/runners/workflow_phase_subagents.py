"""Keep phase-owned subagent continuation inside the workflow's turn task."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import TYPE_CHECKING

from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.agent_turn_context import (
    bind_turn_notices,
    current_background_task_owner,
    current_turn_input_subscriber,
)
from agenthicc.runners.prompt_contract import tool_name
from agenthicc.subagents.delivery import (
    JournalNoticeSource,
    PendingNotice,
    WAKE_TURN_PREFIX,
    deliver_notices,
)
from agenthicc.subagents.phase import PhaseOwner, PhasePools, bind_phase_pools
from agenthicc.workflows.plugin import is_phase_transition_tool

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory

    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.workflows.config import WorkflowConfig

_RECONCILE_S = 0.25
PhaseTurn = Callable[[str, str | None], Awaitable[None]]
_shared_parallel_memory = ContextVar("phase_shared_parallel_memory", default=False)


@contextmanager
def bind_shared_parallel_memory() -> Iterator[None]:
    token = _shared_parallel_memory.set(True)
    try:
        yield
    finally:
        _shared_parallel_memory.reset(token)


class PhaseSubagentHost:
    """Bridge a durable phase scope to the TUI's FIFO input and activity hints."""

    def __init__(
        self,
        notices: JournalNoticeSource,
        conversation: ConversationStore,
        can_deliver: Callable[[], bool],
    ) -> None:
        self.notices = notices
        self.conversation = conversation
        self.can_deliver = can_deliver
        self.active_source: JournalNoticeSource | None = None
        self.scope: PhasePools | None = None
        self._changed = asyncio.Event()

    def hint(self) -> None:
        self._changed.set()

    async def run(
        self,
        scope: PhasePools,
        handle: WorkflowRunHandle,
        memory: JournaledShortTermMemory,
        run_turn: PhaseTurn,
        text: str,
        claim_input: Callable[[], str | None] | None,
    ) -> None:
        source = JournalNoticeSource(self.notices.journal, self.notices.registry, scope.owner)
        self.active_source = source
        self.scope = scope
        status = self.conversation.phase_subagent_wait

        def can_deliver() -> bool:
            return (
                self.can_deliver()
                and handle.phase_invocation == scope.owner.invocation
                and not handle.is_pause_requested()
            )

        def pending() -> tuple[PendingNotice, ...]:
            return source.claim() if can_deliver() else ()

        try:
            with (
                bind_phase_pools(scope),
                bind_turn_notices(source, current_turn_input_subscriber(), can_deliver=can_deliver),
            ):
                deliver_notices(pending(), memory, self.conversation)
                await run_turn(text, None)
                while scope.outstanding():
                    status.set("waiting for phase subagents")
                    self._changed.clear()
                    notices = pending()
                    if notices:
                        status.set(
                            "phase subagent results ready"
                            if any(notice.kind == "worker" for notice in notices)
                            else ""
                        )
                        deliver_notices(notices, memory, self.conversation)
                    queued = claim_input() if claim_input is not None and can_deliver() else None
                    if queued is not None:
                        if not notices:
                            status.set("")
                        await run_turn(queued, None)
                    elif notices:
                        ids = "\n".join(notice.message_id for notice in notices)
                        key = WAKE_TURN_PREFIX + sha256(ids.encode()).hexdigest()
                        await run_turn(notices[0].text, key)
                    else:
                        try:
                            await asyncio.wait_for(self._changed.wait(), timeout=_RECONCILE_S)
                        except TimeoutError:
                            pass
        finally:
            self.scope = None
            self.active_source = None
            status.set("")


async def run_phase_turn(
    config: WorkflowConfig,
    phase: str,
    memory: ShortTermMemory | None,
    transition_tools: Iterable[object],
    run_turn: PhaseTurn,
    text: str,
) -> None:
    """Register a scoped provider turn when its host can resume and gate it."""
    host = config.phase_subagents
    handle = config.workflow_handle
    if (
        host is None
        or handle is None
        or not handle.phase_invocation
        or not handle.current_phase
        or (phase and phase != handle.current_phase)
    ):
        await run_turn(text, None)
        return

    owner = PhaseOwner(
        config.conversation_id,
        handle.run_id,
        handle.frame_id,
        handle.current_phase,
        handle.phase_invocation,
    )
    transitions = frozenset(
        tool_name(tool) for tool in transition_tools if is_phase_transition_tool(tool)
    ) | {"enter_workflow"}
    scope = PhasePools(owner, host.notices.journal, transitions)
    if (
        not handle.checkpoint_supported
        or _shared_parallel_memory.get()
        or not isinstance(memory, JournaledShortTermMemory)
        or memory.journal is not host.notices.journal
        or current_background_task_owner() is None
    ):
        if scope.outstanding():
            raise RuntimeError(
                "Restore the phase's supervised conversation route before resuming its pools."
            )
        await run_turn(text, None)
        return
    await host.run(scope, handle, memory, run_turn, text, config.next_queued_message)
