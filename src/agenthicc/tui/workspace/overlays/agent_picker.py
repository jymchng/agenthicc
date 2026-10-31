"""``/agent``: choose the primary agent the session talks to (AGX-09 §5.3)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from agenthicc.agents.definitions import AgentSpec
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["AgentPickerOverlay"]


class AgentPickerOverlay(Overlay):
    """The primary agents, with the current one selected; Enter chooses."""

    name = "agent-picker"

    def __init__(
        self,
        agents: Sequence[AgentSpec],
        *,
        current: str,
        default: str,
        choose: Callable[[str], object],
        close: Callable[[], None],
    ) -> None:
        self._agents = tuple(agents)
        self._current = current
        self._default = default
        self._choose = choose
        self._close = close
        names = [spec.name for spec in self._agents]
        self._index = names.index(current) if current in names else 0
        self._height = 12

    def set_viewport(self, width: int, height: int) -> None:
        self._height = max(4, height)

    def render(self) -> RenderableType:
        from rich.console import Group
        from rich.markup import escape
        from rich.text import Text

        lines = [
            Text.from_markup(
                "  [bold]Agents[/bold] [dim]· the next turn talks to the one you choose[/dim]"
            )
        ]
        room = max(1, self._height - 2)
        start = min(max(0, self._index - room // 2), max(0, len(self._agents) - room))
        for index, spec in enumerate(self._agents[start : start + room], start):
            marks = (" ★" if spec.name == self._default else "") + (
                " (current)" if spec.name == self._current else ""
            )
            pointer = "▸" if index == self._index else " "
            line = f" {pointer} [bold]{escape(spec.name)}[/bold]{marks}  [dim]{escape(spec.description)}[/dim]"
            lines.append(Text.from_markup(line, style="reverse" if index == self._index else ""))
        if not self._agents:
            lines.append(Text("  No primary agents are defined.", style="dim"))
        lines.append(Text("  ↑↓ select  Enter choose  Esc cancel", style="dim"))
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if key in (Key.UP, Key.DOWN) and self._agents:
            step = -1 if key is Key.UP else 1
            self._index = (self._index + step) % len(self._agents)
        elif key is Key.ENTER and self._agents:
            self._choose(self._agents[self._index].name)
            self._close()
        elif key is Key.ESC:
            self._close()
        return True
