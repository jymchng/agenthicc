"""Completion data types for the TUI input system (PRD-57 §6.8).

The command types in this module are retained for backwards compatibility.
The canonical command definitions and registry live in
``agenthicc.commands``; this module adapts the old names to that implementation
so completion and dispatch cannot drift apart.
``agenthicc.tui.input_bar`` is a backward-compatibility re-export of this module.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agenthicc.commands.builtins import BUILTIN_COMMANDS as _CANONICAL_BUILTIN_COMMANDS
from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry

__all__ = [
    "AtMentionCompleter",
    "BUILTIN_COMMANDS",
    "CommandRegistry",
    "CommandSpec",
    "SlashCommandCompleter",
    "SkillCompleter",
    "_entry_meta",
    "build_default_registry",
]

# ── slash-command registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSpec:
    """Legacy metadata-only specification for a custom completion entry.

    New commands should use :class:`agenthicc.commands.Command`.  The class is
    kept so callers that only contributed completion metadata continue to work.
    Built-in commands are not defined with this class.
    """

    name: str  # e.g. "/status"
    description: str  # e.g. "Show running agents and their tasks"
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""  # e.g. "[provider] [model]"  (PRD-37)
    group: str = "Built-in"  # "Built-in" | "Skills" | "Plugins" | "MCP"  (PRD-38)


# Keep the legacy export as an alias, rather than maintaining a second list of
# built-in definitions.  The objects retain their handlers and menu factories,
# which also makes this compatibility registry suitable for dispatch callers.
BUILTIN_COMMANDS: list[Command] = _CANONICAL_BUILTIN_COMMANDS

_SLASH_RE = re.compile(r"(?:^|\s)(\/\S*)$")


class SlashCommandCompleter:
    """Matches /commands from a registered list.

    Returns :class:`CommandSpec` matches for a partial ``/``-prefixed token.
    """

    def __init__(
        self,
        commands: list[Command | CommandSpec] | None = None,
    ) -> None:
        self._commands: list[Command | CommandSpec] = list(commands or BUILTIN_COMMANDS)

    def add(self, spec: Command | CommandSpec) -> None:
        self._commands.append(spec)

    def matches(self, partial: str) -> list[Command | CommandSpec]:
        """Return all commands whose name or alias starts with *partial*."""
        result: list[Command | CommandSpec] = []
        for cmd in self._commands:
            if _is_skill_entry(cmd):
                continue
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return result

    def get_match_for_line(self, line: str) -> list[Command | CommandSpec]:
        """Extract the trailing /token from *line* and return matches."""
        m = _SLASH_RE.search(line)
        if m is None:
            return []
        return self.matches(m.group(1))


_DOLLAR_RE = re.compile(r"(?:^|\n)(\$\S*)$")


class SkillCompleter(SlashCommandCompleter):
    """Matches dollar-prefixed skills from the canonical command registry."""

    def matches(self, partial: str) -> list[Command | CommandSpec]:
        result: list[Command | CommandSpec] = []
        for cmd in self._commands:
            if not _is_skill_entry(cmd):
                continue
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return result

    def get_match_for_line(self, line: str) -> list[Command | CommandSpec]:
        match = _DOLLAR_RE.search(line)
        if match is None:
            return []
        return self.matches(match.group(1))


def _is_skill_entry(cmd: Command | CommandSpec) -> bool:
    """Return whether a completion entry belongs to the skill namespace."""
    return (
        getattr(cmd, "is_skill", False)
        or cmd.group == "Skills"
        or getattr(cmd, "source_id", "").startswith("skill:")
    )


class CommandRegistry(UnifiedCommandRegistry):
    """Backward-compatible adapter for the unified slash-command registry.

    ``CommandSpec`` inputs are promoted to metadata-only ``Command`` objects;
    canonical ``Command`` instances are stored unchanged.
    """

    def register(self, spec: Command | CommandSpec) -> None:
        command = spec if isinstance(spec, Command) else _command_from_spec(spec)
        super().register(command)

    def register_many(self, specs: Sequence[Command | CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)


def _command_from_spec(spec: CommandSpec) -> Command:
    """Promote a legacy completion spec to a unified command."""
    return Command(
        name=spec.name,
        description=spec.description,
        group=spec.group,
        aliases=spec.aliases,
        argument_hint=spec.argument_hint,
    )


def build_default_registry() -> CommandRegistry:
    """Create the legacy registry adapter pre-loaded with canonical commands."""
    reg = CommandRegistry()
    reg.register_many(list(_CANONICAL_BUILTIN_COMMANDS))
    return reg


# ── @-mention file helper ─────────────────────────────────────────────────────


def _entry_meta(entry: Path) -> str:
    """Return a short metadata string (file size or "dir")."""
    try:
        if entry.is_dir():
            return "dir"
        kb = entry.stat().st_size / 1024
        if kb < 1:
            return f"{entry.stat().st_size} B"
        if kb < 1024:
            return f"{kb:.0f} KB"
        return f"{kb / 1024:.1f} MB"
    except OSError:
        return ""


class AtMentionCompleter:
    """Returns file/directory completions for @-mention fragments.

    Used by :mod:`agenthicc.tui.mention_input` for the inline dropdown.
    """

    def __init__(
        self,
        base_path: str | Path = ".",
        recent_urls: list[str] | None = None,
    ) -> None:
        self._base = Path(base_path).resolve()
        self._recent_urls: list[str] = recent_urls or []

    def completions(self, fragment: str) -> list[tuple[str, str]]:
        """Return [(display_path, meta), ...] matching *fragment*.

        Suitable for the inline dropdown in :func:`~mention_input.read_line_with_mention`.
        """
        if fragment.startswith("http"):
            results: list[tuple[str, str]] = []
            for url in self._recent_urls:
                if url.startswith(fragment):
                    results.append((url, "url"))
            return results

        if "/" in fragment:
            dir_part, file_prefix = fragment.rsplit("/", 1)
            search_dir = self._base / dir_part
        else:
            dir_part = ""
            file_prefix = fragment
            search_dir = self._base

        if not search_dir.is_dir():
            return []

        path_results: list[tuple[str, str]] = []
        try:
            for entry in sorted(
                search_dir.iterdir(),
                key=lambda e: (not e.is_dir(), e.name),
            ):
                if entry.name.startswith("."):
                    continue
                if not entry.name.startswith(file_prefix):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display = (
                    f"{dir_part}/{entry.name}{suffix}" if dir_part else f"{entry.name}{suffix}"
                )
                path_results.append((display, _entry_meta(entry)))
        except PermissionError:
            pass
        return path_results
