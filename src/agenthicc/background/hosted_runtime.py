"""Project a detached interactive runtime through the background control plane."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from agenthicc.tools.approval import ApprovalRequest, InteractiveApprovals
from agenthicc.tui.runtime.commands import SendMessageCommand

from .inbox import BackgroundInteractionInbox
from .input_inbox import BackgroundInput, BackgroundInputInbox
from .model import BackgroundSession, ModeApplicationStatus, SessionStatus
from .store import InvalidSessionTransition, SessionNotFound

if TYPE_CHECKING:
    from agenthicc.runners.tui_session import TUISession

    from .store import BackgroundStore


logger = logging.getLogger(__name__)


class HostedBackground:
    """Drive the existing session from inboxes while its terminal is detached."""

    def __init__(
        self, store: BackgroundStore, session: TUISession, close: Callable[[], None]
    ) -> None:
        self._store = store
        self._session = session
        self._close = close
        self._inbox = BackgroundInteractionInbox(store)
        self._input = BackgroundInputInbox(store)
        self._interaction: ApprovalRequest | None = None
        self._awaiting_ack: BackgroundInput | None = None
        self._last_heartbeat = 0.0
        self.lock = asyncio.Lock()

    async def run(self, adopted: BackgroundSession) -> None:
        ctx = self._session._ctx
        try:
            while True:
                try:
                    async with self.lock:
                        record = await asyncio.to_thread(self._store.get, adopted.session_id)
                        if (
                            record.attempt != adopted.attempt
                            or record.lease_token != adopted.lease_token
                        ):
                            return
                        if record.status is SessionStatus.CANCELLING:
                            self._cancel()
                            return
                        if (
                            record.status is SessionStatus.RUNNING
                            and record.mode_application_status is ModeApplicationStatus.PENDING
                        ):
                            await asyncio.to_thread(
                                self._store.record_mode_application,
                                adopted.session_id,
                                requested_mode_name=adopted.requested_mode_name,
                                effective_mode_name=ctx.app_state.active_mode().name,
                                attempt=adopted.attempt,
                                lease_token=adopted.lease_token,
                            )
                        await self.ingest(record)
                        if record.status is SessionStatus.RUNNING and self._settled():
                            self._close()
                            return
                        now = time.monotonic()
                        if now - self._last_heartbeat >= 2:
                            self._last_heartbeat = now
                            conversation = ctx.app_state.conversation
                            handle = self._session._workflow_handle
                            await asyncio.to_thread(
                                self._store.heartbeat,
                                adopted.session_id,
                                lease_token=adopted.lease_token,
                                attempt=adopted.attempt,
                                phase=(handle.current_phase or "") if handle is not None else "",
                                activity=self._activity(),
                                tokens_in=conversation.tokens_in(),
                                tokens_out=conversation.tokens_out(),
                                cost_usd=conversation.cost_usd(),
                            )
                except InvalidSessionTransition:
                    pass
                except OSError as exc:
                    logger.warning("Live control polling will retry (%s)", type(exc).__name__)
                await asyncio.sleep(0.15)
        except SessionNotFound:
            return

    def _cancel(self) -> None:
        session = self._session
        session._cancel_active_task()
        source = session._subagent_notices
        if source is not None:
            for handle in source.registry.handles():
                if handle.pool is None or handle.task.done():
                    continue
                for worker in handle.pool.state.workers:
                    if worker.live:
                        handle.pool.request_stop(
                            worker.worker_id,
                            source="background",
                            reason="user requested session cancellation",
                        )
        self._close()

    def _settled(self) -> bool:
        session = self._session
        task = session._agent_task
        if (
            task is not None
            and not task.done()
            or session._msg_queue.items()
            or session._ctx.app_state.pending_approval() is not None
            or session._workflow_handle is not None
        ):
            return False
        source = session._subagent_notices
        return source is None or (
            not source.has_pending()
            and not any(not handle.task.done() for handle in source.registry.handles())
        )

    def _activity(self) -> str:
        session = self._session
        handle = session._workflow_handle
        if handle is not None and handle.lifecycle in {"paused", "pausing"}:
            return "Workflow explicitly paused"
        conversation = session._ctx.app_state.conversation
        if conversation.phase_subagent_wait():
            return "Workflow waiting for subagents or collection"
        return "Live session running"

    async def ingest(self, record: BackgroundSession) -> None:
        await self._sync_interaction(record)
        accepted = self._awaiting_ack
        if accepted is None:
            accepted = await asyncio.to_thread(
                self._input.claim_next,
                record.session_id,
                owner_attempt=record.attempt,
                lease_token=record.lease_token,
                mark_delivered=False,
            )
            if accepted is None:
                return
            self._session._ctx.app_state.conversation.append_event(
                "background_input_received",
                {"message_id": accepted.message_id, "attempt": record.attempt},
                event_id=f"background-input:{accepted.message_id}",
            )
            await self._session.handle_send(
                SendMessageCommand(
                    command_id=accepted.message_id, text=accepted.text, source="agents-manager"
                )
            )
            self._awaiting_ack = accepted
        await asyncio.to_thread(
            self._input.acknowledge,
            record.session_id,
            accepted.message_id,
            owner_attempt=record.attempt,
            lease_token=record.lease_token,
        )
        self._awaiting_ack = None

    async def _sync_interaction(self, record: BackgroundSession) -> None:
        pending = self._session._ctx.app_state.pending_approval()
        item_id = record.approval_item_id or record.input_item_id
        if item_id:
            if pending is None or pending is not self._interaction:
                await asyncio.to_thread(self._close_interaction, record, item_id)
                self._interaction = None
                return
            answer = await asyncio.to_thread(self._inbox.accepted_answer, item_id)
            service = self._session._ctx.approval_svc
            if answer is None or not isinstance(service, InteractiveApprovals):
                return
            result = self._inbox.answer_response(answer)
            message, mode, scope = (
                result.get("message"),
                result.get("mode"),
                result.get("scope_grant"),
            )
            if service.respond_for_request(
                answer.item.request_id,
                allowed=result.get("allowed") is True,
                remember=result.get("remember") is True,
                remember_all=result.get("remember_all") is True,
                message=message if isinstance(message, str) else "",
                mode=mode if isinstance(mode, str) else None,
                scope_grant=scope if isinstance(scope, str) else None,
            ):
                await asyncio.to_thread(self._inbox.mark_delivered, item_id)
            return
        if pending is not None and record.status is SessionStatus.RUNNING:
            await asyncio.to_thread(self._publish_interaction, record, pending)
            self._interaction = pending

    def _publish_interaction(self, record: BackgroundSession, pending: ApprovalRequest) -> None:
        from .worker import BackgroundApprovalService

        kind: Literal["approval", "question"]
        if pending.kind == "questions":
            kind = "question"
            request_id, target = BackgroundApprovalService._question_target(pending)
            status = SessionStatus.WAITING_INPUT
        else:
            kind = "approval"
            request_id = pending.request_id or pending.tool_use_id
            target = BackgroundApprovalService._approval_target(pending)
            status = SessionStatus.WAITING_APPROVAL
        item = self._inbox.register(
            session_id=record.session_id,
            attempt=record.attempt,
            request_id=request_id,
            kind=kind,
            target=target,
            expires_at=pending.deadline_at,
        )
        self._store.transition(
            record.session_id,
            status,
            expected_status=record.status,
            expected_attempt=record.attempt,
            expected_lease_token=record.lease_token,
            approval_request=pending.tool_name if kind == "approval" else "",
            approval_item_id=item.item_id if kind == "approval" else "",
            input_request=pending.tool_name if kind == "question" else "",
            input_item_id=item.item_id if kind == "question" else "",
        )
        self._inbox.activate(item.item_id)

    def _close_interaction(self, record: BackgroundSession, item_id: str) -> None:
        item = self._inbox.get(item_id)
        if item.status in {"pending", "publishing"}:
            self._inbox.set_terminal(item_id, "stale", "request is no longer pending")
        self._store.transition(
            record.session_id,
            SessionStatus.RUNNING,
            expected_status=record.status,
            expected_attempt=record.attempt,
            expected_lease_token=record.lease_token,
            approval_request="",
            approval_item_id="",
            input_request="",
            input_item_id="",
            latest_activity="Live workflow running",
        )
