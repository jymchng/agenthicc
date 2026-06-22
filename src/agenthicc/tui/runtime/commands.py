"""Command types and CommandBus (PRD-61 §3)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable


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


@dataclass(frozen=True)
class InterruptAgentCommand(Command):
    pass


# ── CommandBus ────────────────────────────────────────────────────────────────

class CommandBus:
    """One handler per command type. Commands represent executable intent."""

    def __init__(self) -> None:
        self._handlers: dict[type, Callable] = {}

    def register(self, command_type: type, handler: Callable) -> None:
        self._handlers[command_type] = handler

    def dispatch(self, command: Command) -> object:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        return handler(command)

    async def dispatch_async(self, command: Command) -> object:
        import inspect    # noqa: PLC0415
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        if inspect.iscoroutinefunction(handler):
            return await handler(command)
        return handler(command)
