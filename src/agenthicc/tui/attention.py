"""Event-driven cross-session attention routing for the reactive TUI."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agenthicc.background.attention import (
    _BackgroundAttentionSnapshot,
    _load_attention_snapshot,
)

if TYPE_CHECKING:
    from rich.console import Console

    from agenthicc.background.agents_service import AgentsService
    from agenthicc.background.settings import BackgroundSettings
    from agenthicc.tui.conversation_store import AppState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AttentionNotice:
    kind: Literal["needs", "finished", "failed"]
    session_id: str
    item_id: str = ""
    workspace: str = "workspace"


class AgentAttentionRouter:
    """Project registry and inbox changes into badges and coalesced TUI toasts."""

    def __init__(
        self,
        service: AgentsService,
        settings: BackgroundSettings,
        app_state: AppState,
        *,
        set_terminal_title: Callable[[str], None] | None = None,
        send_terminal_notification: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._service = service
        self._settings = settings
        self._app_state = app_state
        self._set_terminal_title = set_terminal_title
        self._send_terminal_notification = send_terminal_notification
        self._monotonic = monotonic
        self._now = now
        self._wake = asyncio.Event()
        self._notices: list[_AttentionNotice] = []
        self._coalesce_deadline: float | None = None
        self._toast_deadline: float | None = None
        self._hint_deadline: float | None = None
        self._notification_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Watch the durable registry and inbox; rendering never polls them."""
        from agenthicc.background.watch import FileWatcher

        await self._service.prepare_inbox_async()
        watcher = FileWatcher(fallback_interval_s=0.25, safety_interval_s=2.0)
        inbox_path = self._service.inbox.path
        watcher.watch("registry", self._service.store.events_path)
        watcher.watch("inbox", inbox_path)
        watcher.watch("inbox-wal", Path(f"{inbox_path}-wal"))
        watcher.watch("inbox-shm", Path(f"{inbox_path}-shm"))
        conversation = self._app_state.conversation
        unsubscribers = [
            self._app_state.modal_open.subscribe(self._wake.set),
            conversation.background_dnd.subscribe(self._wake.set),
            conversation.terminal_focused.subscribe(self._wake.set),
        ]
        try:
            previous = await _load_attention_snapshot(self._service)
            self._publish_counts(previous)
            conversation.agents_hint.set(True)
            self._hint_deadline = self._monotonic() + 10.0
            while True:
                now = self._monotonic()
                timeout = self._next_timeout(now)
                watcher_task = asyncio.create_task(watcher.wait(timeout))
                wake_task = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait(
                        (watcher_task, wake_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (watcher_task, wake_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(watcher_task, wake_task, return_exceptions=True)
                changed = (
                    watcher_task.result()
                    if watcher_task.done() and not watcher_task.cancelled()
                    else {}
                )
                if wake_task.done() and not wake_task.cancelled():
                    self._wake.clear()
                if conversation.background_dnd():
                    self._discard_notices()
                if changed:
                    current = await _load_attention_snapshot(self._service)
                    self._observe(previous, current)
                    self._publish_counts(current)
                    previous = current
                now = self._monotonic()
                self._flush_if_ready(now)
                self._expire_toast(now)
                self._expire_hint(now)
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()
            watcher.close()
            for notification_task in self._notification_tasks:
                notification_task.cancel()
            await asyncio.gather(*self._notification_tasks, return_exceptions=True)

    def _next_timeout(self, now: float) -> float | None:
        conversation = self._app_state.conversation
        deadlines = [
            deadline
            for deadline in (self._toast_deadline, self._hint_deadline)
            if deadline is not None
        ]
        if (
            not conversation.background_dnd()
            and not self._app_state.modal_open()
            and self._coalesce_deadline is not None
        ):
            deadlines.append(self._coalesce_deadline)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _publish_counts(self, snapshot: _BackgroundAttentionSnapshot) -> None:
        from agenthicc.background.model import SessionStatus

        conversation = self._app_state.conversation
        conversation.background_needs_you.set(snapshot.needs_you_count)
        conversation.background_running.set(snapshot.running_count)
        oldest = snapshot.oldest_item
        if oldest is not None:
            conversation.agents_target.set(oldest.session_id)
            conversation.agents_item_target.set(oldest.item_id)
        else:
            waiting = [
                session
                for session in snapshot.sessions
                if session.status
                in {
                    SessionStatus.WAITING_APPROVAL,
                    SessionStatus.WAITING_INPUT,
                    SessionStatus.ORPHANED,
                }
            ]
            target = min(
                waiting,
                key=lambda session: (
                    session.state_changed_at or session.last_active,
                    session.session_id,
                ),
                default=None,
            )
            conversation.agents_target.set(target.session_id if target is not None else None)
            conversation.agents_item_target.set(None)
        if self._set_terminal_title is not None:
            parts = ["Agenthicc"]
            if snapshot.needs_you_count:
                parts.append(f"{snapshot.needs_you_count} need you")
            if snapshot.running_count:
                parts.append(f"{snapshot.running_count} running")
            self._set_terminal_title(" · ".join(parts))

    def _observe(
        self,
        previous: _BackgroundAttentionSnapshot,
        current: _BackgroundAttentionSnapshot,
    ) -> None:
        from agenthicc.background.model import SessionStatus

        old_sessions = {session.session_id: session for session in previous.sessions}
        needs_notified: set[str] = set()
        for session in current.sessions:
            old = old_sessions.get(session.session_id)
            before = old.status if old is not None else None
            after = session.status
            kind: Literal["needs", "finished", "failed"] | None = None
            if after in {
                SessionStatus.WAITING_APPROVAL,
                SessionStatus.WAITING_INPUT,
            } and before not in {
                SessionStatus.WAITING_APPROVAL,
                SessionStatus.WAITING_INPUT,
            }:
                kind = "needs"
                needs_notified.add(session.session_id)
            elif after is SessionStatus.ORPHANED and before is not SessionStatus.ORPHANED:
                kind = "failed"
            elif after is SessionStatus.COMPLETED and before is not SessionStatus.COMPLETED:
                kind = "finished"
            elif after is SessionStatus.FAILED and before is not SessionStatus.FAILED:
                kind = "failed"
            if kind is not None:
                self._queue_notice(kind, session.session_id, session.cwd)

        new_items = current.pending_item_ids - previous.pending_item_ids
        if new_items and not needs_notified:
            oldest = current.oldest_item
            if oldest is not None:
                for _item_id in new_items:
                    self._queue_notice(
                        "needs",
                        oldest.session_id,
                        oldest.project_root,
                        item_id=oldest.item_id,
                    )
        if needs_notified or new_items:
            self._app_state.conversation.agents_hint.set(True)
            self._hint_deadline = self._monotonic() + 10.0

    def _queue_notice(
        self,
        kind: Literal["needs", "finished", "failed"],
        session_id: str,
        cwd: str,
        *,
        item_id: str = "",
    ) -> None:
        if not self._notices:
            self._coalesce_deadline = self._monotonic() + 2.0
        self._notices.append(
            _AttentionNotice(
                kind=kind,
                session_id=session_id,
                item_id=item_id,
                workspace=Path(cwd.rstrip("/\\")).name or "workspace",
            )
        )

    def _flush_if_ready(self, now: float) -> None:
        conversation = self._app_state.conversation
        if not self._notices or self._coalesce_deadline is None or now < self._coalesce_deadline:
            return
        if conversation.background_dnd():
            self._discard_notices()
            return
        if self._app_state.modal_open():
            return
        quiet = self._settings.notifications.quiet_hours
        if quiet is not None and quiet.active_at(self._now()):
            self._discard_notices()
            return

        notices, self._notices = self._notices, []
        self._coalesce_deadline = None
        counts = {
            "needs": sum(item.kind == "needs" for item in notices),
            "finished": sum(item.kind == "finished" for item in notices),
            "failed": sum(item.kind == "failed" for item in notices),
        }
        if len(notices) == 1:
            notice = notices[0]
            message = {
                "needs": f"◆ {notice.workspace} needs your input",
                "finished": f"✓ {notice.workspace} finished",
                "failed": f"✗ {notice.workspace} failed",
            }[notice.kind]
        else:
            parts: list[str] = []
            if counts["finished"]:
                parts.append(f"✓ {counts['finished']} agents finished")
            if counts["failed"]:
                parts.append(f"✗ {counts['failed']} failed")
            if counts["needs"]:
                parts.append(f"◆ {counts['needs']} need you")
            message = " · ".join(parts)

        target = next((notice for notice in notices if notice.kind == "needs"), notices[0])
        conversation.background_toast.set(message)
        conversation.background_toast_kind.set(target.kind)
        conversation.agents_toast_target.set(target.session_id)
        if target.item_id:
            conversation.agents_item_target.set(target.item_id)
        self._toast_deadline = now + 8.0

        if (
            self._settings.notifications.local_enabled
            and self._settings.notifications.local_backend in {"auto", "desktop", "terminal"}
            and not conversation.terminal_focused()
        ):
            summary = message.replace("✓ ", "").replace("✗ ", "").replace("◆ ", "")
            payload: dict[str, object] = {
                "workspace": "agents",
                "summary": summary[:240],
            }
            task = asyncio.create_task(
                self._send_system_notification(payload),
                name="background-attention-system-notification",
            )
            self._notification_tasks.add(task)
            task.add_done_callback(self._notification_tasks.discard)

    async def _send_system_notification(self, payload: dict[str, object]) -> None:
        from agenthicc.background.notifications import _send_local_notification

        backend = self._settings.notifications.local_backend
        summary = str(payload.get("summary", ""))
        try:
            if backend == "terminal":
                if self._send_terminal_notification is not None:
                    self._send_terminal_notification(summary)
                return
            result = await asyncio.to_thread(_send_local_notification, payload, "desktop")
            if (
                result == "local_notifier_unavailable"
                and backend == "auto"
                and self._send_terminal_notification is not None
            ):
                self._send_terminal_notification(summary)
        except Exception:  # noqa: BLE001 - system notifications cannot stop the TUI
            log.debug("background attention system notification failed", exc_info=True)

    def _discard_notices(self) -> None:
        self._notices.clear()
        self._coalesce_deadline = None
        self._toast_deadline = None
        self._hint_deadline = None
        conversation = self._app_state.conversation
        conversation.background_toast.set(None)
        conversation.background_toast_kind.set(None)
        conversation.agents_toast_target.set(None)
        conversation.agents_hint.set(False)

    def _expire_toast(self, now: float) -> None:
        if self._toast_deadline is None or now < self._toast_deadline:
            return
        self._toast_deadline = None
        conversation = self._app_state.conversation
        conversation.background_toast.set(None)
        conversation.background_toast_kind.set(None)
        conversation.agents_toast_target.set(None)

    def _expire_hint(self, now: float) -> None:
        if self._hint_deadline is None or now < self._hint_deadline:
            return
        self._hint_deadline = None
        self._app_state.conversation.agents_hint.set(False)


def _emit_terminal_notification(console: Console, summary: str) -> None:
    """Write a supported notification OSC, never visible text, to the TUI stream."""
    terminal = os.environ.get("TERM", "").casefold()
    if not console.is_terminal or terminal in {"", "dumb"}:
        return
    safe_summary = "".join(
        character
        for character in summary[:240]
        if ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
    )
    if safe_summary:
        console.file.write(f"\x1b]9;{safe_summary}\x07")
        console.file.flush()
