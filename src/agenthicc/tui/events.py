"""TUIEventAdapter — kernel Event -> TranscriptModel bridge (PRD-06).

The adapter consumes :class:`agenthicc.kernel.Event` objects, either fed
directly via :meth:`TUIEventAdapter.apply`, drained from an
``asyncio.Queue`` via :meth:`consume`, or tailed from a running
:class:`~agenthicc.kernel.EventProcessor` via :meth:`subscribe_to` +
:meth:`sync`. Payload fields are mapped defensively with ``.get()`` so
malformed or partial events never raise.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .transcript import ToolCallState, TranscriptModel

__all__ = ["TUIEventAdapter"]


class TUIEventAdapter:
    """Mutates a :class:`TranscriptModel` in response to kernel events."""

    def __init__(self, model: TranscriptModel) -> None:
        self.model = model
        self._processor: Any | None = None
        self._cursor = 0
        self._handlers = {
            "UIUpdate": self._on_ui_update,
            "ApplicationLog": self._on_application_log,
            "AgentSpawnRequest": self._on_agent_spawn,
            "WorkflowNodeStatusChanged": self._on_node_status,
            "ToolCallStarted": self._on_tool_started,
            "ToolCallComplete": self._on_tool_complete,
            "UIAdUpdate": self._on_ad_update,
        }

    # ── event ingestion ──────────────────────────────────────────────────

    def apply(self, event: Any) -> None:
        """Apply a single kernel Event. Unknown event types are ignored."""
        event_type = getattr(event, "event_type", None)
        handler = self._handlers.get(event_type)
        if handler is None:
            return
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            payload = {}
        handler(event, payload)

    def subscribe_to(self, processor: Any) -> None:
        """Attach to an EventProcessor; call :meth:`sync` to catch up."""
        self._processor = processor
        self._cursor = 0

    def sync(self) -> int:
        """Apply all processor events not yet seen. Returns count applied."""
        if self._processor is None:
            return 0
        log = self._processor.event_log
        new = log[self._cursor :]
        self._cursor = len(log)
        for event in new:
            self.apply(event)
        return len(new)

    async def consume(self, queue: asyncio.Queue) -> None:
        """Drain events from *queue* until a ``None`` sentinel is received."""
        while True:
            event = await queue.get()
            if event is None:
                break
            self.apply(event)

    # ── per-event handlers ───────────────────────────────────────────────

    def _agent_id(self, event: Any, payload: dict) -> str:
        return (
            payload.get("agent_id")
            or getattr(event, "source_agent_id", None)
            or "system"
        )

    def _on_ui_update(self, event: Any, payload: dict) -> None:
        text = (
            payload.get("text")
            or payload.get("message")
            or payload.get("content")
            or str(payload.get("type", ""))
        )
        if text:
            self.model.append_line(self._agent_id(event, payload), str(text))

    def _on_application_log(self, event: Any, payload: dict) -> None:
        level = str(payload.get("level", "info")).upper()
        message = payload.get("message", "")
        self.model.append_line(
            self._agent_id(event, payload), f"[{level}] {message}"
        )

    def _on_agent_spawn(self, event: Any, payload: dict) -> None:
        agent_id = payload.get("agent_id", "unknown")
        self.model.append_turn(
            agent_id=agent_id,
            agent_name=payload.get("agent_type") or agent_id,
            timestamp=getattr(event, "timestamp", None),
        )

    def _on_node_status(self, event: Any, payload: dict) -> None:
        node_id = payload.get("node_id", "?")
        status = payload.get("status", "?")
        line = f"[node] {node_id} → {status}"
        error = payload.get("error")
        if error:
            line += f" ({error})"
        self.model.append_line(self._agent_id(event, payload), line)

    def _on_tool_started(self, event: Any, payload: dict) -> None:
        tool_use_id = (
            payload.get("tool_use_id")
            or payload.get("tool_call_id")
            or getattr(event, "tool_call_id", None)
        )
        if not tool_use_id:
            return
        self.model.add_tool_call(
            agent_id=self._agent_id(event, payload),
            tool_use_id=tool_use_id,
            name=payload.get("name") or payload.get("tool_name") or "tool",
            state=ToolCallState.RUNNING,
        )

    def _on_tool_complete(self, event: Any, payload: dict) -> None:
        tool_use_id = (
            payload.get("tool_use_id")
            or payload.get("tool_call_id")
            or getattr(event, "tool_call_id", None)
        )
        if not tool_use_id:
            return
        error = payload.get("error")
        success = payload.get("success", error is None)
        self.model.update_tool_call(
            tool_use_id,
            state=ToolCallState.SUCCESS if success else ToolCallState.FAILURE,
            duration_ms=payload.get("duration_ms"),
            error=error,
        )

    def _on_ad_update(self, event: Any, payload: dict) -> None:
        from agenthicc.ads import AdRecord
        ad = AdRecord(
            ad_id=payload.get("ad_id", ""),
            text=payload.get("text", ""),
            cta_url=payload.get("cta_url", ""),
        )
        self.model.set_current_ad(ad)
