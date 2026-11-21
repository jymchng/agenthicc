"""Command types and CommandBus (PRD-61 §3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar, cast


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Base command ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Command:
    command_id: str = field(default_factory=_new_id)


# ── Commands ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendMessageCommand(Command):
    text: str = ""
    source: str = "user"


@dataclass(frozen=True)
class InterruptAgentCommand(Command):
    source: Literal["keyboard", "escape", "ctrl_c"] = "keyboard"
    disposition: Literal["cancel", "pause"] = "cancel"


@dataclass(frozen=True)
class OpenAgentsHubCommand(Command):
    """Open the agents hub over the conversation (AGX-05 §5.2).

    ``tab`` is a ``--tab`` name (``running``, ``needs-you`` …); empty keeps the
    hub's default. ``run_id`` filters the Running tab to one goal run.
    """

    tab: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class BackgroundSubagentsCommand(Command):
    """Detach the active subagent pool from its foreground tool call (Ctrl+B)."""


@dataclass(frozen=True)
class OpenSubagentsCommand(Command):
    """Move focus to the subagent panel (Ctrl+O, ``/subagents``; AGX-08 §5.2)."""


@dataclass(frozen=True)
class CycleAgentCommand(Command):
    """Talk to the next primary agent from the next turn (Tab; AGX-09 §5.3)."""


@dataclass(frozen=True)
class OpenPaletteCommand(Command):
    """Open the command palette over the conversation (Ctrl+P; TUX-05 §2.4)."""


@dataclass(frozen=True)
class OpenTranscriptCommand(Command):
    """Open the transcript view on the last tool call (Ctrl+T; TUX-09 §2.1)."""


@dataclass(frozen=True)
class ScrollPaneCommand(Command):
    """Scroll the fullscreen layout's transcript pane (TUX-09 §2.3)."""

    action: Literal[
        "page_up", "page_down", "line_up", "line_down", "wheel_up", "wheel_down", "follow"
    ] = "page_up"


@dataclass(frozen=True)
class OpenShortcutsCommand(Command):
    """Open the page of every key (``?`` on an empty prompt, ``/shortcuts``; TUX-10 §2.6)."""


C = TypeVar("C", bound=Command)


# ── CommandBus ────────────────────────────────────────────────────────────────


class CommandBus:
    """One handler per command type. Commands represent executable intent."""

    def __init__(self) -> None:
        self._handlers: dict[type[Command], Callable[[Command], object | Awaitable[object]]] = {}

    def register(
        self,
        command_type: type[C],
        handler: Callable[[C], object | Awaitable[object]],
    ) -> None:
        self._handlers[command_type] = cast(
            Callable[[Command], object | Awaitable[object]], handler
        )

    def handles(self, command_type: type[Command]) -> bool:
        """Whether a handler is registered for *command_type*."""
        return command_type in self._handlers

    def dispatch(self, command: Command) -> object:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        return handler(command)

    async def dispatch_async(self, command: Command) -> object:
        import inspect

        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        result = handler(command)
        if inspect.isawaitable(result):
            return await result
        return result
