"""Textual Message subclasses and shared enums for inter-widget communication (PRD-55).

All messages are posted through the Textual message bus; no direct widget-to-widget
Python calls allowed (mirrors the kernel's tool-only agent communication rule).
"""
from __future__ import annotations

from enum import Enum

from textual.message import Message


class InteractionMode(Enum):
    """Current UI interaction mode — drives footer hints and input gating."""
    IDLE = "idle"
    BUSY = "thinking"
    RUNNING = "running"
    AWAITING_APPROVAL = "approval"
    ERROR = "error"
    COMPLETE = "complete"

from agenthicc.tui.trigger import MatchItem

__all__ = [
    "AgentRunFinished",
    "AgentRunStarted",
    "AgentStateChanged",
    "ApprovalDecided",
    "ApprovalRequired",
    "ConsolePrint",
    "ErrorOccurred",
    "FileModified",
    "GitStatusUpdated",
    "InputSubmitted",
    "ModeCycled",
    "PendingQueueUpdated",
    "ThinkingStep",
    "ToolCallComplete",
    "ToolCallStarted",
    "TokensUpdated",
    "TranscriptAppend",
    "TranscriptUpdated",
    "TriggerActivated",
    "TriggerCancelled",
    "TriggerSelected",
    "UserMessagePosted",
]


class InputSubmitted(Message):
    """Posted by InputPanel when the user confirms their input (Enter key)."""

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value


class TriggerActivated(Message):
    """Posted by InputPanel when a trigger character is typed (@, /)."""

    def __init__(self, char: str, fragment: str) -> None:
        super().__init__()
        self.char = char
        self.fragment = fragment


class TriggerSelected(Message):
    """Posted by TriggerMenu when the user selects a match item."""

    def __init__(self, item: MatchItem) -> None:
        super().__init__()
        self.item = item


class TriggerCancelled(Message):
    """Posted by TriggerMenu when the user cancels (Esc)."""


class TranscriptUpdated(Message):
    """Signal-only message: TranscriptModel has new lines to render."""


class ConsolePrint(Message):
    """Posted by ConsoleShim.print(); handled by TranscriptView to display markup."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.markup = markup


class ModeCycled(Message):
    """Posted by InputPanel when the user cycles the input mode (Shift+Tab)."""

    def __init__(self, new_name: str, new_badge: str) -> None:
        super().__init__()
        self.new_name = new_name
        self.new_badge = new_badge


class AgentRunStarted(Message):
    """Posted when an agent turn begins running."""

    def __init__(self, agent_id: str, model_short: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.model_short = model_short


class AgentRunFinished(Message):
    """Posted when an agent turn completes (success or failure)."""


class ToolCallStarted(Message):
    """Posted when the agent invokes a tool."""

    def __init__(self, tool_use_id: str, name: str, args: dict) -> None:
        super().__init__()
        self.tool_use_id = tool_use_id
        self.name = name
        self.args = args


class ToolCallComplete(Message):
    """Posted when a tool call finishes."""

    def __init__(
        self,
        tool_use_id: str,
        success: bool,
        duration_ms: float | None,
        error: str | None,
        diff: str | None,
    ) -> None:
        super().__init__()
        self.tool_use_id = tool_use_id
        self.success = success
        self.duration_ms = duration_ms
        self.error = error
        self.diff = diff


class TokensUpdated(Message):
    """Posted after each LLM turn completes with updated token counts."""

    def __init__(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        super().__init__()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd


class PendingQueueUpdated(Message):
    """Posted when the pending-message queue count changes during agent streaming."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count


# ── Typed transcript events ───────────────────────────────────────────────────

class UserMessagePosted(Message):
    """User submitted a prompt — append to transcript as a User block."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TranscriptAppend(Message):
    """Append a pre-formatted Rich markup string directly to TranscriptView."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.markup = markup


class ThinkingStep(Message):
    """A single step in the agent thinking process."""

    def __init__(self, step: str, done: bool = False) -> None:
        super().__init__()
        self.step = step
        self.done = done


class FileModified(Message):
    """Agent edited a file."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path


class ApprovalRequired(Message):
    """Agent needs user approval before proceeding."""

    def __init__(self, prompt: str, command: str) -> None:
        super().__init__()
        self.prompt = prompt
        self.command = command


class ApprovalDecided(Message):
    """User approved or rejected an action."""

    def __init__(self, approved: bool) -> None:
        super().__init__()
        self.approved = approved


class ErrorOccurred(Message):
    """An error should be displayed in the transcript."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__()
        self.message = message
        self.detail = detail


class AgentStateChanged(Message):
    """Agent operational state changed."""

    def __init__(
        self,
        state: str,
        tool: str | None = None,
        progress: str | None = None,
    ) -> None:
        super().__init__()
        self.state = state       # "idle" | "thinking" | "running" | "approval" | "error" | "complete"
        self.tool = tool
        self.progress = progress


class GitStatusUpdated(Message):
    """Git branch / clean status updated."""

    def __init__(self, branch: str, is_clean: bool) -> None:
        super().__init__()
        self.branch = branch
        self.is_clean = is_clean
