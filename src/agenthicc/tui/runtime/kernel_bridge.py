"""KernelBridge — translates kernel EventProcessor state changes into
ConversationStore mutations (PRD-66 §2)."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from agenthicc.tui.conversation_store import ConversationStore, AgentState

# ── event handler registry ────────────────────────────────────────────────────
_InjectedHandler = Callable[["KernelBridge", dict], None]
_EVENT_HANDLERS: dict[str, _InjectedHandler] = {}


def register_event_handler(kind: str) -> Callable[[_InjectedHandler], _InjectedHandler]:
    """Decorator: register a handler for inject_event() events of type *kind*."""
    def decorator(fn: _InjectedHandler) -> _InjectedHandler:
        _EVENT_HANDLERS[kind] = fn
        return fn
    return decorator


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
        """Dispatch a stream event to the registered handler for its type."""
        handler = _EVENT_HANDLERS.get(event.get("type", ""))
        if handler is not None:
            handler(self, event)


# ── built-in handlers ─────────────────────────────────────────────────────────

@register_event_handler("agent_state_change")
def _handle_agent_state_change(self: KernelBridge, event: dict) -> None:
    mapping = {
        "idle":     AgentState.IDLE,
        "thinking": AgentState.THINKING,
        "running":  AgentState.RUNNING,
        "complete": AgentState.COMPLETE,
        "error":    AgentState.ERROR,
    }
    state_name = event.get("state", "idle").lower()
    if state_name in mapping:
        self._conv.agent_state.set(mapping[state_name])
    tool = event.get("tool")
    if tool is not None:
        self._conv.active_tool.set(tool)


@register_event_handler("session_summary")
def _handle_session_summary(self: KernelBridge, event: dict) -> None:
    sid   = event.get("session_id", "")
    model = event.get("model_name", "")
    if sid:
        self._conv.session_id.set(sid)
    if model:
        self._conv.model_name.set(model)


@register_event_handler("notification")
def _handle_notification(self: KernelBridge, event: dict) -> None:
    text = event.get("text")
    self._conv.notification.set(text)
    if text:
        asyncio.get_event_loop().call_later(
            3.0,
            lambda: self._conv.notification.set(None),
        )
