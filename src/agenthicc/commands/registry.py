"""UnifiedCommandRegistry — single source of truth for all slash commands (PRD-44, PRD-45)."""
from __future__ import annotations

from typing import Iterator

from .command import Command

__all__ = ["UnifiedCommandRegistry"]


class UnifiedCommandRegistry:
    """Single source of truth for all slash commands.

    Replaces:
    - ``BUILTIN_COMMANDS`` list  (input_bar.py)
    - ``CommandRegistry``        (input_bar.py)
    - ``CommandMenuRegistry``    (menu.py)
    - The ``if first == ...`` dispatch in ``SlashCommandHandler``
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}   # canonical name -> Command
        self._aliases: dict[str, str] = {}         # alias -> canonical name

    # ── write ────────────────────────────────────────────────────────────────

    def register(self, cmd: Command) -> None:
        """Register (or replace) a command and its aliases."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name

    def register_many(self, cmds: list[Command]) -> None:
        for cmd in cmds:
            self.register(cmd)

    def unregister(self, name: str) -> None:
        canonical = self._aliases.pop(name, name)
        cmd = self._commands.pop(canonical, None)
        if cmd:
            for alias in cmd.aliases:
                self._aliases.pop(alias, None)

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, name: str) -> Command | None:
        """Resolve a name or alias to a Command."""
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def all_commands(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def commands_for_group(self, group: str) -> list[Command]:
        return sorted(
            (c for c in self._commands.values() if c.group == group),
            key=lambda c: c.name,
        )

    def groups(self) -> list[str]:
        order = ["Built-in", "Skills", "Plugins", "MCP"]
        seen = {c.group for c in self._commands.values()}
        return [g for g in order if g in seen] + sorted(seen - set(order))

    def matches(self, partial: str) -> list[Command]:
        """Return commands whose name or alias starts with *partial*."""
        result: list[Command] = []
        for cmd in self._commands.values():
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return sorted(result, key=lambda c: c.name)

    # ── PRD-45: source namespacing ────────────────────────────────────────────

    def commands_for_source(self, source_id: str) -> list[Command]:
        """Return all commands registered with the given source_id."""
        return [c for c in self._commands.values() if c.source_id == source_id]

    def unregister_source(self, source_id: str) -> int:
        """Remove all commands with the given source_id.  Returns the count removed."""
        names = [c.name for c in self.commands_for_source(source_id)]
        for name in names:
            self.unregister(name)
        return len(names)

    # ── dunder helpers ────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[Command]:
        return iter(self.all_commands())

    def __len__(self) -> int:
        return len(self._commands)
