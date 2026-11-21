"""UnifiedCommandRegistry — source of truth for commands and skill triggers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Iterator

from .command import COMMAND_GROUPS, Command

__all__ = ["UnifiedCommandRegistry", "command_rank"]


class UnifiedCommandRegistry:
    """Single source of truth for command and explicit skill records.

    Replaces:
    - ``BUILTIN_COMMANDS`` list  (input_bar.py)
    - ``CommandRegistry``        (input_bar.py)
    - ``CommandMenuRegistry``    (menu.py)
    - The ``if first == ...`` dispatch in ``SlashCommandHandler``
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}  # canonical name -> Command
        self._aliases: dict[str, str] = {}  # alias -> canonical name

    # ── write ────────────────────────────────────────────────────────────────

    def register(self, cmd: Command) -> None:
        """Register (or replace) a command and its aliases."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name

    def register_many(self, cmds: Sequence[Command]) -> None:
        for cmd in cmds:
            self.register(cmd)

    def replace_with(self, other: "UnifiedCommandRegistry") -> None:
        """Replace contents while preserving this registry's identity.

        Session-owned dispatchers and trigger handlers keep a reference to the
        registry, so a reload builds a candidate first and publishes it in
        place only after discovery succeeds.
        """
        if other is self:
            return
        self._commands = dict(other._commands)
        self._aliases = dict(other._aliases)

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
        seen = {c.group for c in self._commands.values()}
        return [g for g in COMMAND_GROUPS if g in seen] + sorted(seen - set(COMMAND_GROUPS))

    def matches(self, partial: str) -> list[Command]:
        """Return commands whose name or alias starts with *partial*."""
        result: list[Command] = []
        for cmd in self._commands.values():
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return sorted(result, key=lambda c: c.name)

    def search(
        self,
        partial: str,
        usage: Mapping[str, tuple[int, float]] | None = None,
    ) -> list[Command]:
        """Commands matching *partial*, best first (TUX-05 §2.2).

        The tiers are: exact name or alias, name prefix, alias prefix, fuzzy
        name (its characters in order), then a description word starting
        with the fragment. Ties go to the command run most often, then most
        recently (*usage*: name → ``(count, last_used)``), then by name.
        """
        ranked: list[tuple[int, int, float, str, Command]] = []
        counts = usage or {}
        for cmd in self._commands.values():
            tier = command_rank(cmd, partial)
            if tier is None:
                continue
            count, last = counts.get(cmd.name, (0, 0.0))
            ranked.append((tier, -count, -last, cmd.name, cmd))
        ranked.sort(key=lambda entry: entry[:4])
        return [entry[4] for entry in ranked]

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


def command_rank(cmd: Command, partial: str) -> int | None:
    """The tier of *cmd* for the typed *partial* (``"/comp"``), or ``None``.

    ``0`` exact name, ``1`` exact alias, ``2`` name prefix, ``3`` alias
    prefix, ``4`` fuzzy name, ``5`` a description word starts with the
    fragment. A lone trigger character matches every command at tier ``2``.
    """
    from agenthicc.mentions.matcher import fuzzy_positions

    query = partial.casefold()
    name = cmd.name.casefold()
    aliases = [alias.casefold() for alias in cmd.aliases]
    if name == query:
        return 0
    if query in aliases:
        return 1
    if name.startswith(query):
        return 2
    if any(alias.startswith(query) for alias in aliases):
        return 3
    fragment = query[1:] if query[:1] in ("/", "$") else query
    if not fragment:
        return 2
    if fuzzy_positions(fragment, name[1:]) is not None:
        return 4
    words = "".join(ch if ch.isalnum() else " " for ch in cmd.description.casefold()).split()
    if any(word.startswith(fragment) for word in words):
        return 5
    return None
