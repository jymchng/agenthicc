"""TUIEventAdapter — kernel EventProcessor → TranscriptModel bridge."""
from __future__ import annotations

import asyncio
from typing import Any

from .transcript import ToolCallState, TranscriptModel

__all__ = ["TUIEventAdapter"]


class TUIEventAdapter:
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

    def apply(self, event: Any) -> None:
        event_type = getattr(event, "event_type", None)
        handler = self._handlers.get(event_type)
        if handler is None:
            return
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            payload = {}
        handler(event, payload)

    def subscribe_to(self, processor: Any) -> None:
        self._processor = processor
        self._cursor = 0

    def sync(self) -> int:
        if self._processor is None:
            return 0
        log = self._processor.event_log
        new = log[self._cursor:]
        self._cursor = len(log)
        for event in new:
            self.apply(event)
        return len(new)

    async def consume(self, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            if event is None:
                break
            self.apply(event)

    def _agent_id(self, event: Any, payload: dict) -> str:
        return (
            payload.get("agent_id")
            or getattr(event, "source_agent_id", None)
            or "system"
        )

    def _on_ui_update(self, event: Any, payload: dict) -> None:
        content = payload.get("content") or payload.get("text") or payload.get("message", "")
        agent_id = self._agent_id(event, payload)
        if content:
            self.model.append_line(agent_id, str(content))

    def _on_application_log(self, event: Any, payload: dict) -> None:
        level = str(payload.get("level", "INFO")).upper()
        message = payload.get("message", "")
        agent_id = self._agent_id(event, payload)
        self.model.append_line(agent_id, f"{level}: {message}")

    def _on_agent_spawn(self, event: Any, payload: dict) -> None:
        agent_id = payload.get("agent_id") or self._agent_id(event, payload)
        # Fall back to agent_id when agent_type is absent (not "Agent" default)
        agent_name = payload.get("agent_type") or agent_id
        import time  # noqa: PLC0415
        self.model.append_turn(agent_id, agent_name, time.time())

    def _on_node_status(self, event: Any, payload: dict) -> None:
        agent_id = self._agent_id(event, payload)
        status = payload.get("status", "")
        node_id = payload.get("node_id", "")
        error = payload.get("error")
        if status and node_id:
            if error:
                self.model.append_line(agent_id, f"node {node_id}: {status}: {error}")
            else:
                self.model.append_line(agent_id, f"node {node_id}: {status}")

    def _on_tool_started(self, event: Any, payload: dict) -> None:
        tool_name = payload.get("tool_name") or payload.get("name") or ""
        tool_use_id = payload.get("tool_use_id") or ""
        if not tool_use_id:
            return  # silently skip if no tool_use_id
        agent_id = payload.get("agent_id") or self._agent_id(event, payload)
        self.model.add_tool_call(agent_id, tool_use_id, tool_name, state=ToolCallState.RUNNING)

    def _on_tool_complete(self, event: Any, payload: dict) -> None:
        tool_use_id = payload.get("tool_use_id") or payload.get("tool_call_id") or ""
        if not tool_use_id:
            return
        error = payload.get("error")
        success = payload.get("success")
        # Determine state: if error present and success not explicitly True → FAILURE
        if error is not None or success is False:
            state = ToolCallState.FAILURE
        elif success is True:
            state = ToolCallState.SUCCESS
        else:
            state = ToolCallState.SUCCESS  # default
        duration_ms = float(payload.get("duration_ms", 0.0))
        kwargs: dict = {"state": state, "duration_ms": duration_ms}
        if error is not None:
            kwargs["error"] = str(error)
        self.model.update_tool_call(tool_use_id, **kwargs)

    def _on_ad_update(self, event: Any, payload: dict) -> None:
        try:
            from agenthicc.ads import AdRecord  # noqa: PLC0415
            ad = AdRecord(
                ad_id=payload.get("ad_id", ""),
                text=payload.get("text", ""),
                cta_url=payload.get("cta_url", ""),
            )
            self.model.set_current_ad(ad)
        except Exception:  # noqa: BLE001
            pass
