"""Footer widget — context-sensitive keyboard shortcuts driven by InteractionMode."""
from __future__ import annotations

from textual.reactive import reactive
from textual.widget import Widget

from agenthicc.tui.messages import AgentStateChanged, InteractionMode

__all__ = ["Footer"]

_HINTS: dict[str, str] = {
    InteractionMode.IDLE.value:              "Enter Submit  Ctrl+J Newline  /cmd  @Mention  Ctrl+L Clear  F1 Help  Esc Menu",
    InteractionMode.BUSY.value:              "Ctrl+C Interrupt  Ctrl+Z Background  Esc Menu",
    InteractionMode.RUNNING.value:           "Ctrl+C Interrupt  Ctrl+Z Background  Esc Hide Logs",
    InteractionMode.AWAITING_APPROVAL.value: "Y Approve  N Reject  A Approve All  Esc Cancel",
    InteractionMode.ERROR.value:             "R Retry  L View Logs  Esc Dismiss",
    InteractionMode.COMPLETE.value:          "Enter New Task  F2 History  Ctrl+L Clear  Esc Menu",
}


def _format_hints(raw: str) -> str:
    parts = [h.strip() for h in raw.split("  ") if h.strip()]
    segments: list[str] = []
    for p in parts:
        words = p.split()
        if len(words) >= 2:
            segments.append(f"[bold]{words[0]}[/bold] [dim]{' '.join(words[1:])}[/dim]")
        else:
            segments.append(f"[dim]{p}[/dim]")
    return "  [dim]│[/dim]  ".join(segments)


class Footer(Widget):
    """One-row footer with context-sensitive keyboard hints.

    Subscribes to :class:`~agenthicc.tui.messages.AgentStateChanged` to switch
    between hint sets matching the current :class:`~agenthicc.tui.messages.InteractionMode`.
    """

    DEFAULT_CSS = """
    Footer {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
    }
    """

    mode: reactive[str] = reactive(InteractionMode.IDLE.value)

    def render(self) -> str:
        raw = _HINTS.get(self.mode, _HINTS[InteractionMode.IDLE.value])
        return _format_hints(raw)

    def on_agent_state_changed(self, event: AgentStateChanged) -> None:
        # Map agent state string directly to InteractionMode value
        self.mode = event.state
