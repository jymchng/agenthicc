"""KernelBridge — translates kernel EventProcessor state changes into
ConversationStore mutations (PRD-66 §2)."""
from __future__ import annotations

import asyncio
from typing import Any

from agenthicc.tui.conversation_store import ConversationStore, AgentState


class KernelBridge:
    """Subscribes to the kernel EventProcessor and syncs state to ConversationStore.

    The kernel uses an append-only event log with immutable AppState.
    This bridge subscribes to the kernel's subscriber queue and maps
    relevant changes into ConversationStore signal updates.
    """

    def __init__(
        self,
        processor: Any,           # agenthicc.kernel.EventProcessor
        conversation: ConversationStore,
    ) -> None:
        self._proc  = processor
        self._conv  = conversation
        self._task: asyncio.Task | None = None
        self._prev:  Any = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="kernel-bridge")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._proc.subscribe(queue)
        try:
            while True:
                new_state = await queue.get()
                self._on_state(new_state)
        except asyncio.CancelledError:
            pass
        finally:
            self._proc.unsubscribe(queue)

    def _on_state(self, state: Any) -> None:
        prev = self._prev
        self._prev = state

        # ── Session / model ────────────────────────────────────────────────────
        session_id = getattr(state, "session_id", "")
        if session_id and (
            prev is None
            or getattr(prev, "session_id", "") != session_id
        ):
            self._conv.session_id.set(session_id)

        settings = getattr(state, "settings", None)
        if settings:
            model = getattr(settings, "model", "")
            prev_settings = getattr(prev, "settings", None) if prev else None
            prev_model = getattr(prev_settings, "model", "") if prev_settings else ""
            if model and model != prev_model:
                self._conv.model_name.set(model)

        # ── Notification ───────────────────────────────────────────────────────
        notification = getattr(state, "notification", None)
        prev_notif   = getattr(prev, "notification", None) if prev else None
        if notification and notification != prev_notif:
            self._conv.notification.set(notification)
            asyncio.get_event_loop().call_later(
                3.0,
                lambda: self._conv.notification.set(None),
            )

    def inject_event(self, event: dict) -> None:
        """Manually inject a stream event (used by the legacy agent_turn.py bridge)."""
        kind = event.get("type", "")

        if kind == "agent_state_change":
            state_name = event.get("state", "idle").lower()
            mapping = {
                "idle":     AgentState.IDLE,
                "thinking": AgentState.THINKING,
                "running":  AgentState.RUNNING,
                "complete": AgentState.COMPLETE,
                "error":    AgentState.ERROR,
            }
            if state_name in mapping:
                self._conv.agent_state.set(mapping[state_name])
            tool = event.get("tool")
            if tool is not None:
                self._conv.active_tool.set(tool)

        elif kind == "session_summary":
            sid   = event.get("session_id", "")
            model = event.get("model_name", "")
            if sid:
                self._conv.session_id.set(sid)
            if model:
                self._conv.model_name.set(model)

        elif kind == "notification":
            text = event.get("text")
            self._conv.notification.set(text)
            if text:
                asyncio.get_event_loop().call_later(
                    3.0,
                    lambda: self._conv.notification.set(None),
                )
