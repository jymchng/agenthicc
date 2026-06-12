"""Pure transcript rendering model for the Agenthicc TUI (PRD-06).

Everything in this module is plain Python with no terminal dependencies, so
it is fully testable headless. The :class:`TranscriptModel` holds the
labeled-block transcript state and renders it to a list of plain strings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Any

__all__ = [
    "SPINNER_FRAMES",
    "AgentTurnEntry",
    "ToolCallEntry",
    "ToolCallState",
    "TranscriptModel",
    "diff_lines",
]

#: Braille spinner cycle (PRD-06 §5.2).
SPINNER_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

SEPARATOR = ""  # no separator line between turns; blank line provides spacing


class ToolCallState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


_STATE_SYMBOLS = {
    ToolCallState.PENDING: ".",
    ToolCallState.SUCCESS: "✓",
    ToolCallState.FAILURE: "✗",
}


@dataclass
class ToolCallEntry:
    tool_use_id: str
    name: str
    args: dict = field(default_factory=dict)
    output_lines: list[str] = field(default_factory=list)
    expanded: bool = False
    state: ToolCallState = ToolCallState.PENDING
    duration_ms: float | None = None
    error: str | None = None
    spinner_frame: int = 0

    @property
    def symbol(self) -> str:
        if self.state is ToolCallState.RUNNING:
            return SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]
        return _STATE_SYMBOLS[self.state]

    def render(self) -> str:
        """Render tool call as ToolName(args) with optional output preview."""
        # Build call signature: ToolName(key=val, ...)
        args_str = ", ".join(
            f"{k}={repr(v)[:40]}" for k, v in (self.args or {}).items()
        )
        call_line = f"  [bold]{self.name}[/bold][dim]({args_str})[/dim]"

        if self.state is ToolCallState.RUNNING:
            frame = SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]
            return f"{call_line}  {frame}"

        if self.state is ToolCallState.SUCCESS:
            dur = f"  [dim]{self.duration_ms:.0f}ms[/dim]" if self.duration_ms else ""
            parts = [f"{call_line}  [green]✓[/green]{dur}"]
            if self.output_lines:
                preview = self.output_lines if self.expanded else self.output_lines[:2]
                for ln in preview:
                    parts.append(f"    [dim]{ln[:120]}[/dim]")
                if not self.expanded and len(self.output_lines) > 2:
                    extra = len(self.output_lines) - 2
                    short_id = self.tool_use_id[:8]
                    parts.append(f"    [dim](+{extra} more — /expand {short_id})[/dim]")
            return "\n".join(parts)

        if self.state is ToolCallState.PENDING:
            return f"{call_line}  [dim].[/dim]"

        # FAILURE
        err = f": {self.error}" if self.error else ""
        return f"{call_line}  [red]✗[/red][dim]{err}[/dim]"


@dataclass
class AgentTurnEntry:
    agent_id: str
    agent_name: str
    timestamp: float = field(default_factory=time.time)
    lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    cost_usd: float | None = None
    tokens: int | None = None

    def header(self) -> str:
        hhmmss = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        return f"[bold cyan]●[/] [bold]{self.agent_name}[/]  [dim]{hhmmss}[/dim]"

    def footer(self) -> str | None:
        if self.cost_usd is None and self.tokens is None:
            return None
        parts = []
        if self.tokens is not None:
            parts.append(f"tokens: {self.tokens:,}")
        if self.cost_usd is not None:
            parts.append(f"cost: ${self.cost_usd:.3f}")
        return "  → " + "  ".join(parts)


class TranscriptModel:
    """Mutable transcript state: an ordered list of agent turns."""

    def __init__(self) -> None:
        self.turns: list[AgentTurnEntry] = []
        self._tool_index: dict[str, ToolCallEntry] = {}
        self._current_ad: Any = None

    # ── mutation ─────────────────────────────────────────────────────────

    def append_turn(
        self,
        agent_id: str,
        agent_name: str | None = None,
        timestamp: float | None = None,
    ) -> AgentTurnEntry:
        turn = AgentTurnEntry(
            agent_id=agent_id,
            agent_name=agent_name or agent_id,
            timestamp=timestamp if timestamp is not None else time.time(),
        )
        self.turns.append(turn)
        return turn

    def _turn_for(self, agent_id: str) -> AgentTurnEntry:
        for turn in reversed(self.turns):
            if turn.agent_id == agent_id:
                return turn
        return self.append_turn(agent_id)

    def append_line(self, agent_id: str, text: str) -> None:
        """Append a line of model/log text to the latest turn of *agent_id*."""
        self._turn_for(agent_id).lines.append(text)

    def add_tool_call(
        self,
        agent_id: str,
        tool_use_id: str,
        name: str,
        args: dict | None = None,
        state: ToolCallState = ToolCallState.RUNNING,
    ) -> ToolCallEntry:
        entry = ToolCallEntry(
            tool_use_id=tool_use_id,
            name=name,
            args=args or {},
            state=state,
        )
        self._turn_for(agent_id).tool_calls.append(entry)
        self._tool_index[tool_use_id] = entry
        return entry

    def finish_tool_call(
        self,
        tool_use_id: str,
        success: bool = True,
        duration_ms: float | None = None,
        error: str | None = None,
        output: Any = None,
    ) -> ToolCallEntry | None:
        """Mark a tool call complete and optionally record its output."""
        entry = self._tool_index.get(tool_use_id)
        if entry is None:
            return None
        entry.state = ToolCallState.SUCCESS if success else ToolCallState.FAILURE
        if duration_ms is not None:
            entry.duration_ms = duration_ms
        if error is not None:
            entry.error = str(error)
        if output is not None:
            raw = str(output) if not isinstance(output, str) else output
            entry.output_lines = raw.splitlines()
        return entry

    def update_tool_call(
        self,
        tool_use_id: str,
        state: ToolCallState | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
        spinner_frame: int | None = None,
    ) -> ToolCallEntry | None:
        entry = self._tool_index.get(tool_use_id)
        if entry is None:
            return None
        if state is not None:
            entry.state = state
        if duration_ms is not None:
            entry.duration_ms = duration_ms
        if error is not None:
            entry.error = error
        if spinner_frame is not None:
            entry.spinner_frame = spinner_frame
        return entry

    def advance_spinner(self) -> None:
        """Advance the spinner frame on every RUNNING tool call."""
        for entry in self._tool_index.values():
            if entry.state is ToolCallState.RUNNING:
                entry.spinner_frame = (entry.spinner_frame + 1) % len(SPINNER_FRAMES)

    # ── derived state ────────────────────────────────────────────────────

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns if t.cost_usd is not None)

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self.turns if t.tokens is not None)

    def has_running_tools(self) -> bool:
        return any(
            e.state is ToolCallState.RUNNING for e in self._tool_index.values()
        )

    # ── rendering ────────────────────────────────────────────────────────

    def render(self) -> list[str]:
        """Render the transcript to plain-text lines (PRD-06 §5.1)."""
        out: list[str] = []
        for i, turn in enumerate(self.turns):
            if i > 0:
                out.append(SEPARATOR)
            out.append(turn.header())
            # Tool calls rendered FIRST — they happened before the prose reply
            for tc in turn.tool_calls:
                out.append(tc.render())
            # Prose lines — markdown lines carry the "\x00md\x00" sentinel prefix
            for line in turn.lines:
                out.append(line)  # sentinel already embedded by _run_agent_turn
            footer = turn.footer()
            if footer is not None:
                out.append(footer)
        return out


    # -- ad panel ------------------------------------------------------------

    def set_current_ad(self, ad: Any) -> None:
        """Set (or clear) the current advertisement record."""
        self._current_ad = ad

    def current_ad(self) -> Any:
        """Return the current advertisement record, or None."""
        return self._current_ad

    def render_ad_panel(self) -> str | None:
        """Return a one-line ad string for the renderer, or None."""
        if self._current_ad is None:
            return None
        text = self._current_ad.text[:120]
        url = getattr(self._current_ad, "cta_url", "")
        return f"[Sponsored] {text}" + (f"  {url}" if url else "")


def diff_lines(old: list[str], new: list[str]) -> list[tuple[str, str]]:
    """Minimal line diff between two renders.

    Returns ``[(op, line), ...]`` where op is ``"keep"``, ``"add"`` or
    ``"remove"``, in document order.
    """
    result: list[tuple[str, str]] = []
    matcher = SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            result.extend(("keep", line) for line in old[i1:i2])
        elif tag == "delete":
            result.extend(("remove", line) for line in old[i1:i2])
        elif tag == "insert":
            result.extend(("add", line) for line in new[j1:j2])
        else:  # replace
            result.extend(("remove", line) for line in old[i1:i2])
            result.extend(("add", line) for line in new[j1:j2])
    return result
