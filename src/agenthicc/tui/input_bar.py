"""input_bar.py — CommandRegistry and CommandSpec for slash-command system."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AtMentionCompleter",
    "BUILTIN_COMMANDS",
    "CommandRegistry",
    "CommandSpec",
    "SlashCommandCompleter",
    "_entry_meta",
    "build_default_registry",
]

_GROUP_ORDER = ["Built-in", "Skills", "MCP", "Plugins"]


@dataclass
class CommandSpec:
    name: str
    description: str
    group: str = "Built-in"
    argument_hint: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


class CommandRegistry:
    """Registry of slash commands."""

    def __init__(self) -> None:
        # name -> CommandSpec (canonical names)
        self._commands: dict[str, CommandSpec] = {}
        # alias -> canonical name
        self._alias_map: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> None:
        # Remove any old aliases for the same name (overwrite)
        if spec.name in self._commands:
            old_spec = self._commands[spec.name]
            for alias in old_spec.aliases:
                self._alias_map.pop(alias, None)
        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._alias_map[alias] = spec.name

    def register_many(self, specs: list[CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> None:
        spec = self._commands.pop(name, None)
        if spec is not None:
            for alias in spec.aliases:
                self._alias_map.pop(alias, None)

    def get(self, name: str) -> CommandSpec | None:
        if name in self._commands:
            return self._commands[name]
        canonical = self._alias_map.get(name)
        if canonical:
            return self._commands.get(canonical)
        return None

    def matches(self, prefix: str) -> list[CommandSpec]:
        """Return commands whose name or alias starts with *prefix*."""
        results: dict[str, CommandSpec] = {}
        for name, spec in self._commands.items():
            if name.startswith(prefix):
                results[name] = spec
        for alias, canonical in self._alias_map.items():
            if alias.startswith(prefix):
                spec = self._commands.get(canonical)
                if spec:
                    results[spec.name] = spec
        return list(results.values())

    def all_commands(self) -> list[CommandSpec]:
        return sorted(self._commands.values(), key=lambda s: s.name)

    def groups(self) -> list[str]:
        present = {spec.group for spec in self._commands.values()}
        ordered = [g for g in _GROUP_ORDER if g in present]
        extra = sorted(g for g in present if g not in _GROUP_ORDER)
        return ordered + extra

    def commands_for_group(self, group: str) -> list[CommandSpec]:
        return [s for s in self._commands.values() if s.group == group]

    def commands(self) -> list[str]:
        """Return list of registered command names."""
        return list(self._commands.keys())

    def add(self, spec: CommandSpec) -> None:
        """Compatibility shim: register a spec."""
        self.register(spec)

    def register_command(self, spec: CommandSpec) -> None:
        """Compatibility shim: register a spec."""
        self.register(spec)

    def completions_for(self, prefix: str) -> list[CommandSpec]:
        """Compatibility shim."""
        return self.matches(prefix)

    def __len__(self) -> int:
        return len(self._commands)


BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status", "Show running agents and tool calls", group="Built-in"),
    CommandSpec(
        "/model",
        "Switch the active LLM model",
        group="Built-in",
        argument_hint="[provider] [model]",
    ),
    CommandSpec("/help", "Show available slash commands", group="Built-in"),
    CommandSpec("/history", "Show session transcript history", group="Built-in"),
    CommandSpec("/clear", "Clear the transcript", group="Built-in"),
    CommandSpec("/exit", "Exit the session", group="Built-in"),
    CommandSpec("/mcp", "Manage MCP server connections", group="MCP"),
]


def build_default_registry() -> CommandRegistry:
    """Create a CommandRegistry pre-populated with BUILTIN_COMMANDS."""
    reg = CommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg


# ── Compat classes ────────────────────────────────────────────────────────────

class SlashCommandCompleter:
    """Completes /commands anywhere in the input (compat class)."""

    def __init__(self, commands: list[CommandSpec]) -> None:
        self._commands = list(commands)

    def add(self, spec: CommandSpec) -> None:
        self._commands.append(spec)

    def matches(self, partial: str) -> list[CommandSpec]:
        if not partial.startswith("/"):
            return []
        return [c for c in self._commands if c.name.startswith(partial)]

    def get_match_for_line(self, line: str) -> list[CommandSpec]:
        m = re.search(r"(?:^|\s)(\/\S*)$", line)
        if m is None:
            return []
        return self.matches(m.group(1))


def _entry_meta(path: Path) -> str:
    """Return a short metadata string for a filesystem entry."""
    if path.is_dir():
        return "dir"
    size = path.stat().st_size
    if size >= 1024 * 1024:
        return f"{size // (1024 * 1024)} MB"
    if size >= 1024:
        return f"{size // 1024} KB"
    return f"{size} B"


class AtMentionCompleter:
    """Completes @file/path mentions (compat class)."""

    def __init__(self, base_path: str | Path = ".", *, recent_urls: list[str] | None = None) -> None:
        self._base = Path(base_path).resolve()
        self._recent_urls: list[str] = list(recent_urls or [])

    def completions(self, fragment: str) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []

        # First: match recent_urls whose full URL starts with fragment
        for url in self._recent_urls:
            if url.startswith(fragment):
                results.append((url, "url"))

        # Then: filesystem matches (only when fragment looks like a path, not a URL)
        if not fragment.startswith("http://") and not fragment.startswith("https://"):
            if "/" in fragment:
                dir_part, file_prefix = fragment.rsplit("/", 1)
                search_dir = self._base / dir_part
            else:
                dir_part = ""
                file_prefix = fragment
                search_dir = self._base

            if search_dir.is_dir():
                try:
                    for entry in sorted(search_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                        if entry.name.startswith("."):
                            continue
                        if not entry.name.startswith(file_prefix):
                            continue
                        suffix = "/" if entry.is_dir() else ""
                        display = f"{dir_part}/{entry.name}{suffix}" if dir_part else f"{entry.name}{suffix}"
                        results.append((display, _entry_meta(entry)))
                except PermissionError:
                    pass

        return results
