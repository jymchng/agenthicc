"""The agents hub over a running conversation (AGX-05 §5.2).

``/agents``, Ctrl+G or ← on an empty prompt shows the same
:class:`~agenthicc.tui.workspace.agents_hub.hub.AgentsHub` the shell opens,
in the region the composer normally occupies. The session keeps reading keys
and drawing frames; the hub's engine runs as a task beside the conversation,
so a streaming turn continues underneath. Esc closes the overlay and the
conversation is exactly as it was.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.agents_hub.hub import AgentsHub, HubResult
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType

__all__ = ["AgentsHubOverlay"]


class AgentsHubOverlay(Overlay):
    """An overlay that shows and drives an :class:`AgentsHub`."""

    name = "agents"

    def __init__(
        self,
        hub: AgentsHub,
        *,
        on_close: Callable[[], None],
        on_result: Callable[[HubResult], None],
        request_redraw: Callable[[], None],
    ) -> None:
        self.hub = hub
        self._on_close = on_close
        self._on_result = on_result
        self._request_redraw = request_redraw
        self._width = 80
        self._height = 12
        self._task: asyncio.Task[None] | None = None

    def on_mount(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self.hub.serve(self._request_redraw, self._finish), name="agenthicc-hub-overlay"
            )

    def on_unmount(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            # The hub's engine stops in serve()'s cleanup; durable work goes on.
            task.cancel()

    def set_viewport(self, width: int, height: int) -> None:
        self._width = max(20, width)
        self._height = max(4, height)

    def render(self) -> RenderableType:
        return self.hub.render(self._width, self._height)

    def handle_key(self, key: Key, ch: str) -> bool:
        result = self.hub.handle_key(key, ch)
        if result is not None:
            self._finish(result)
        return True

    def _finish(self, result: HubResult) -> None:
        if result.action == "exit":
            self._on_close()
        else:
            self._on_result(result)
