# src/agenthicc/tui/input_bar.py
"""Slash-command and @-file mention types used by the TUI (PRD-10).

No prompt_toolkit dependency.  The actual interactive input loop lives in
:mod:`agenthicc.tui.mention_input`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "AtMentionCompleter",
    "BUILTIN_COMMANDS",
    "CommandRegistry",
    "CommandSpec",
    "SlashCommandCompleter",
    "_entry_meta",
    "build_default_registry",
]

# ── slash-command registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSpec:
    """Specification for a slash command shown in the completion menu."""

    name: str           # e.g. "/status"
    description: str    # e.g. "Show running agents and their tasks"
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""      # e.g. "[provider] [model]"  (PRD-37)
    group: str = "Built-in"      # "Built-in" | "Skills" | "Plugins" | "MCP"  (PRD-38)


BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status",   "Show running agents and their tasks"),
    CommandSpec("/model",    "Show or switch LLM provider/model",
                argument_hint="[provider] [model]"),
    CommandSpec("/models",   "List all available LLM providers"),
    CommandSpec("/skills",   "List available skills"),
    CommandSpec("/expand",   "Expand tool output or @mention",
                argument_hint="[tool-id-or-@path]"),
    CommandSpec("/history",  "Browse the event log"),
    CommandSpec("/help",     "List available commands"),
    CommandSpec("/cancel",   "Cancel the currently running intent"),
    CommandSpec("/clear",    "Clear the transcript display"),
    CommandSpec("/mcp",      "Show MCP server status",
                argument_hint="[connect <url> [transport]]", group="MCP"),
]

_SLASH_RE = re.compile(r"(?:^|\s)(\/\S*)$")


class SlashCommandCompleter:
    """Matches /commands from a registered list.

    Returns :class:`CommandSpec` matches for a partial ``/``-prefixed token.
    """

    def __init__(self, commands: list[CommandSpec] | None = None) -> None:
        self._commands: list[CommandSpec] = list(commands or BUILTIN_COMMANDS)

    def add(self, spec: CommandSpec) -> None:
        self._commands.append(spec)

    def matches(self, partial: str) -> list[CommandSpec]:
        """Return all commands whose name or alias starts with *partial*."""
        result: list[CommandSpec] = []
        for cmd in self._commands:
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return result

    def get_match_for_line(self, line: str) -> list[CommandSpec]:
        """Extract the trailing /token from *line* and return matches."""
        m = _SLASH_RE.search(line)
        if m is None:
            return []
        return self.matches(m.group(1))


class CommandRegistry:
    """Centralised registry of slash commands (PRD-38)."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> None:
        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def register_many(self, specs: list[CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> None:
        canonical = self._aliases.pop(name, name)
        spec = self._commands.pop(canonical, None)
        if spec:
            for alias in spec.aliases:
                self._aliases.pop(alias, None)

    def get(self, name: str) -> CommandSpec | None:
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def all_commands(self) -> list[CommandSpec]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def commands_for_group(self, group: str) -> list[CommandSpec]:
        return sorted((c for c in self._commands.values() if c.group == group), key=lambda c: c.name)

    def groups(self) -> list[str]:
        order = ["Built-in", "Skills", "Plugins", "MCP"]
        seen = {c.group for c in self._commands.values()}
        return [g for g in order if g in seen] + sorted(seen - set(order))

    def matches(self, partial: str) -> list[CommandSpec]:
        result = []
        for cmd in self._commands.values():
            for candidate in (cmd.name,) + cmd.aliases:
                if candidate.startswith(partial):
                    result.append(cmd)
                    break
        return sorted(result, key=lambda c: c.name)

    def __len__(self) -> int:
        return len(self._commands)


def build_default_registry() -> CommandRegistry:
    """Create a CommandRegistry pre-loaded with BUILTIN_COMMANDS."""
    reg = CommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
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
            results = []
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

        results: list[tuple[str, str]] = []
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
                    f"{dir_part}/{entry.name}{suffix}" if dir_part
                    else f"{entry.name}{suffix}"
                )
                results.append((display, _entry_meta(entry)))
        except PermissionError:
            pass
        return results
