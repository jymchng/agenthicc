"""Command types and CommandBus (PRD-61 §3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast


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

    def dispatch(self, command: Command) -> object:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        return handler(command)

    async def dispatch_async(self, command: Command) -> object:
        import inspect  # noqa: PLC0415

        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValueError(f"No handler for {type(command).__name__}")
        result = handler(command)
        if inspect.isawaitable(result):
            return await result
        return result
