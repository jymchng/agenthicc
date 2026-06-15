"""AgentRuntime — handles SendMessageCommand, drives the streaming turn (PRD-61 §5)."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Callable

from agenthicc.tui.conversation_store import ConversationStore
from agenthicc.tui.runtime.commands import SendMessageCommand, InterruptAgentCommand
from agenthicc.tui.runtime.domain_events import (
    EventBus, AgentStarted, AgentCompleted, AgentFailed, AgentInterrupted,
    ToolStarted, ToolCompleted, TextFinalized, TokensAccounted, ThinkingStepEvent,
    FileModifiedEvent,
)
from agenthicc.tui.runtime.tasks import TaskManager, TaskHandle
from agenthicc.tui.runtime.mode_manager import ModeManager


def _fmt_args(args: dict) -> str:
    """Format tool args for display."""
    if not args:
        return ""
    from rich.markup import escape as _e  # noqa: PLC0415
    items = list(args.items())
    if len(items) == 1:
        return f"[dim]({_e(repr(items[0][1])[:60])})[/dim]"
    return "[dim](" + ", ".join(
        f"{_e(k)}={_e(repr(v)[:25])}" for k, v in items[:3]
    ) + ")[/dim]"


class AgentRuntime:
    """Handles SendMessageCommand. Drives the streaming agent turn lifecycle."""

    def __init__(
        self,
        conversation: ConversationStore,
        event_bus: EventBus,
        task_manager: TaskManager,
        agent_fn: Callable | None = None,
        mode_manager: ModeManager | None = None,
    ) -> None:
        self._conv         = conversation
        self._bus          = event_bus
        self._tasks        = task_manager
        self._agent_fn     = agent_fn      # async callable that returns an async generator
        self._mode_manager = mode_manager
        self._current:     TaskHandle | None = None

    def set_agent_fn(self, fn: Callable) -> None:
        """Set the agent function (dependency injection for the LLM runner)."""
        self._agent_fn = fn

    async def handle_send_message(self, cmd: SendMessageCommand) -> None:
        """Entry point for SendMessageCommand."""
        turn_id = str(uuid.uuid4())
        self._current = self._tasks.spawn(
            f"agent-{turn_id[:8]}",
            self._run_turn(turn_id, cmd.text),
        )

    def handle_interrupt(self, _cmd: InterruptAgentCommand) -> None:
        """Cancel the running agent task."""
        if self._current:
            self._current.cancel()

    # ── internal turn lifecycle ───────────────────────────────────────────────

    async def _run_turn(self, turn_id: str, text: str) -> None:
        agent_name = self._conv.model_name() or "assistant"
        self._conv.begin_turn(agent_name, turn_id)

        # Emit turn_start event → ScrollBufferAppender prints the ● header
        self._conv.append_event("turn_start", {
            "turn_id":    turn_id,
            "agent_name": agent_name,
            "timestamp":  time.time(),
        })

        self._bus.publish(AgentStarted(turn_id=turn_id, model=agent_name))

        try:
            if self._agent_fn is None:
                raise RuntimeError("No agent function configured")

            mode_suffix = ""
            if self._mode_manager:
                mode_suffix = self._mode_manager.active.system_prompt_suffix

            async for event in self._agent_fn(text, mode_suffix=mode_suffix):
                await self._handle_stream_event(turn_id, event)

            self._bus.publish(AgentCompleted(turn_id=turn_id))
            self._conv.end_turn()

        except asyncio.CancelledError:
            self._bus.publish(AgentInterrupted(turn_id=turn_id))
            self._conv.end_turn()

        except Exception as exc:
            self._bus.publish(AgentFailed(turn_id=turn_id, error=str(exc)))
            self._conv.fail_turn(str(exc))

    async def _handle_stream_event(self, turn_id: str, event: dict) -> None:
        """Translate a stream event dict into ConversationStore mutations."""
        kind = event.get("type", "")

        match kind:
            case "tool_start":
                name = event.get("name", "")
                args = event.get("args", {})
                self._conv.set_tool(name)
                self._bus.publish(ToolStarted(
                    tool_use_id=event.get("tool_use_id", ""),
                    name=name,
                    args=args,
                ))

            case "tool_complete":
                name         = event.get("name", "")
                args         = event.get("args", {})
                success      = event.get("success", True)
                duration_ms  = event.get("duration_ms")
                output_lines = event.get("output_lines", [])

                self._conv.clear_tool()
                args_str = _fmt_args(args)
                dur_str  = f"  [dim]{duration_ms:.0f}ms[/dim]" if duration_ms else ""

                self._conv.append_event("tool_complete", {
                    "tool_use_id":  event.get("tool_use_id", ""),
                    "name":         name,
                    "success":      success,
                    "args_str":     args_str,
                    "dur_str":      dur_str,
                    "output_lines": output_lines,
                })
                self._bus.publish(ToolCompleted(
                    tool_use_id=event.get("tool_use_id", ""),
                    name=name,
                    success=success,
                    duration_ms=duration_ms,
                    output_lines=tuple(output_lines),
                    args_str=args_str,
                ))

            case "text_finalized":
                text = event.get("text", "").strip()
                if text:
                    self._conv.append_event("text", {"text": text})
                    self._bus.publish(TextFinalized(turn_id=turn_id, full_text=text))

            case "text_chunk":
                pass  # status bar animation handles; no scroll buffer update yet

            case "thinking_step":
                step = event.get("step", "")
                done = event.get("done", False)
                self._conv.append_event("thinking_step", {"step": step, "done": done})
                self._bus.publish(ThinkingStepEvent(step=step, done=done))

            case "file_modified":
                path = event.get("path", "")
                self._conv.append_event("file_modified", {"path": path})
                self._bus.publish(FileModifiedEvent(path=path))

            case "tokens":
                inp  = event.get("input_tokens", 0)
                out  = event.get("output_tokens", 0)
                cost = event.get("cost_usd", 0.0)
                self._conv.add_tokens(inp, out, cost)
                self._bus.publish(TokensAccounted(
                    input_tokens=inp,
                    output_tokens=out,
                    cost_usd=cost,
                ))

            case "error":
                msg    = event.get("message", "Unknown error")
                detail = event.get("detail", "")
                self._conv.append_event("error", {"message": msg, "detail": detail})
