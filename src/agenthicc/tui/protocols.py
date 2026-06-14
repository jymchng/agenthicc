"""Protocols that describe the behavioural contracts of every TUI component.

Using ``typing.Protocol`` (structural subtyping) means:

* Components can be swapped for test doubles without inheriting from a base class.
* The contracts are self-documenting: the Protocol name and method signatures
  describe exactly what each component must be capable of.
* Static type checkers (mypy / pyright) can verify implementations without
  runtime coupling.

Every concrete state class and rendering component in this package satisfies
at least one of the protocols defined here.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


# ── Observable ────────────────────────────────────────────────────────────────

@runtime_checkable
class Observable(Protocol):
    """A component whose state can be watched for changes."""

    def on_change(self, cb: Callable[[], None]) -> None:
        """Register *cb* to be called whenever the component's state changes."""
        ...

    def _notify(self) -> None:
        """Fire all registered callbacks."""
        ...


# ── Renderable state ──────────────────────────────────────────────────────────

@runtime_checkable
class RenderableState(Observable, Protocol):
    """A state object that can render itself as a Rich markup string.

    *cols* (terminal width) is passed in by the caller so every component
    receives the same value for a given frame — no component ever reads
    terminal width independently.
    """

    def height(self, cols: int) -> int:
        """Number of terminal rows this component occupies at *cols* width.

        Called by :class:`LivePanel` before rendering so it can calculate the
        total panel height and detect overflow before writing to the screen.
        """
        ...

    def render(self, cols: int) -> str:
        """Return a Rich markup string that fits within *cols* columns."""
        ...


# ── Live panel component ──────────────────────────────────────────────────────

@runtime_checkable
class LiveComponent(Protocol):
    """A component backed by a Rich Live block."""

    def start(self) -> None:
        """Activate the Rich Live block (begin redrawing on state changes)."""
        ...

    def stop(self) -> None:
        """Deactivate the Rich Live block and release the terminal."""
        ...


# ── Transcript printer ────────────────────────────────────────────────────────

@runtime_checkable
class TranscriptPrinter(Protocol):
    """A component that appends events to the terminal scroll buffer."""

    def print_user(self, text: str) -> None:
        """Print a user message block."""
        ...

    def print_assistant_header(self, model_short: str) -> None:
        """Print the assistant turn header."""
        ...

    def print_tool_complete(
        self,
        name: str,
        success: bool,
        ms: float | None,
        diff: str | None,
    ) -> None:
        """Print a completed tool-call row."""
        ...

    def print_error(self, message: str, detail: str = "") -> None:
        """Print an error block."""
        ...

    def flush_from_model(self) -> None:
        """Flush any pending lines from the underlying TranscriptModel."""
        ...


# ── Status state ──────────────────────────────────────────────────────────────

@runtime_checkable
class StatusState(RenderableState, Protocol):
    """Reactive state for the agent status bar.  Always 1 row."""

    @property
    def state(self) -> str: ...
    @state.setter
    def state(self, v: str) -> None: ...

    @property
    def tool(self) -> str: ...
    @tool.setter
    def tool(self, v: str) -> None: ...

    def add_tokens(self, inp: int, out: int, cost: float) -> None: ...
    def start_run(self) -> None: ...
    def finish_run(self) -> None: ...
    def tick(self) -> None: ...


# ── Footer state ──────────────────────────────────────────────────────────────

@runtime_checkable
class FooterStateProtocol(RenderableState, Protocol):
    """Reactive state for the context-sensitive footer."""

    @property
    def mode(self) -> str: ...
    @mode.setter
    def mode(self, v: str) -> None: ...

    def notify_text(self, text: str | None) -> None: ...


# ── Input bar state ───────────────────────────────────────────────────────────

@runtime_checkable
class InputBarStateProtocol(Observable, Protocol):
    """Reactive state for the ❯ prompt line."""

    def update(
        self,
        buf: list[str],
        cursor: int,
        paste_condensed: bool,
        paste_label: str,
    ) -> None: ...

    def clear(self) -> None: ...
    def render_prompt(self, cols: int = 80) -> str: ...


# ── Spinner state ─────────────────────────────────────────────────────────────

@runtime_checkable
class SpinnerStateProtocol(Observable, Protocol):
    """Reactive state for the tool-call spinner panel."""

    def add_call(self, tool_use_id: str, name: str, args: dict) -> None: ...

    def complete_call(
        self,
        tool_use_id: str,
        success: bool,
        ms: float | None,
        diff: str | None,
    ) -> None: ...

    def set_streaming_text(self, text: str) -> None: ...
    def tick(self) -> None: ...
    def clear(self) -> None: ...
    def render_calls(self, cols: int = 80) -> list[str]: ...


# ── Root TUI ──────────────────────────────────────────────────────────────────

@runtime_checkable
class TUIRenderer(Protocol):
    """Root TUI class — the public interface consumed by tui_session.py."""

    async def run(self, on_input: Any) -> None: ...
    def on_intent_submitted(self) -> None: ...
    def on_model_call_complete(
        self, input_tokens: int, output_tokens: int, cost_usd: float
    ) -> None: ...
    def on_agent_run_complete(self) -> None: ...
    def _flush_new_lines(self) -> None: ...
