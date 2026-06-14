"""Status widget — agent state, active tool, progress, token counts, runtime."""
from __future__ import annotations

import time

from textual.reactive import reactive
from textual.widget import Widget

from agenthicc.tui.messages import (
    AgentRunFinished,
    AgentRunStarted,
    AgentStateChanged,
    TokensUpdated,
    ToolCallComplete,
    ToolCallStarted,
)

__all__ = ["StatusBar"]

_STATE_COLOR: dict[str, str] = {
    "idle":     "dim",
    "thinking": "yellow",
    "running":  "cyan",
    "approval": "yellow",
    "error":    "red",
    "complete": "green",
    "waiting":  "blue",
}


class StatusBar(Widget):
    """Status region: Agent: Running | Tool: Edit | Progress: 3/7 | Tokens: 42k | Runtime: 00:42

    Always visible; content updates reactively as messages arrive.
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    """

    agent_state: reactive[str] = reactive("idle")
    tool: reactive[str] = reactive("")
    progress: reactive[str] = reactive("")
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)
    cost_usd: reactive[float] = reactive(0.0)
    elapsed_secs: reactive[float] = reactive(0.0)
    _run_start: float = 0.0

    # ── rendering ─────────────────────────────────────────────────────────────

    def render(self) -> str:
        color = _STATE_COLOR.get(self.agent_state, "dim")
        state_label = self.agent_state.title() if self.agent_state != "idle" else "Idle"

        parts: list[str] = [f"[{color}]Agent: {state_label}[/{color}]"]

        if self.tool:
            parts.append(f"[dim] │[/dim] [bold]{self.tool}[/bold]")

        if self.progress:
            parts.append(f"[dim] │ Progress:[/dim] {self.progress}")

        tok = self.input_tokens + self.output_tokens
        if tok:
            tok_str = f"{tok / 1000:.0f}k" if tok >= 1000 else str(tok)
            parts.append(f"[dim] │ Tokens:[/dim] {tok_str}")

        if self.cost_usd:
            parts.append(f"[dim] │ ${self.cost_usd:.3f}[/dim]")

        if self.elapsed_secs > 0:
            mins, secs = divmod(int(self.elapsed_secs), 60)
            parts.append(f"[dim] │ Runtime:[/dim] {mins:02d}:{secs:02d}")

        return "".join(parts)

    # ── tick ──────────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        if self.agent_state not in ("idle", "complete") and self._run_start:
            self.elapsed_secs = time.monotonic() - self._run_start

    # ── message handlers ──────────────────────────────────────────────────────

    def on_agent_run_started(self, _: AgentRunStarted) -> None:
        self.agent_state = "thinking"
        self._run_start = time.monotonic()
        self.elapsed_secs = 0.0
        self.tool = ""
        self.progress = ""

    def on_agent_run_finished(self, _: AgentRunFinished) -> None:
        self.agent_state = "idle"
        self.tool = ""
        self.progress = ""

    def on_agent_state_changed(self, event: AgentStateChanged) -> None:
        self.agent_state = event.state
        if event.tool is not None:
            self.tool = event.tool
        if event.progress is not None:
            self.progress = event.progress

    def on_tool_call_started(self, event: ToolCallStarted) -> None:
        self.agent_state = "running"
        self.tool = event.name

    def on_tool_call_complete(self, _: ToolCallComplete) -> None:
        self.agent_state = "thinking"
        self.tool = ""

    def on_tokens_updated(self, event: TokensUpdated) -> None:
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cost_usd += event.cost_usd
