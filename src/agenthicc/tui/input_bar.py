# src/agenthicc/tui/input_bar.py
"""Enhanced input bar: slash-command completions, @-file mentions, multi-line
entry, and session history (PRD-10).

Nothing here touches TranscriptModel, TUIEventAdapter, or the kernel directly —
those integrations live in tui/app.py.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    merge_completers,
)
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

__all__ = [
    "AtMentionCompleter",
    "BUILTIN_COMMANDS",
    "CommandSpec",
    "InputBarSession",
    "SlashCommandCompleter",
]


@dataclass(frozen=True)
class CommandSpec:
    """Specification for a slash command shown in the completion menu."""

    name: str           # e.g. "/status"
    description: str    # e.g. "Show running agents and their tasks"
    aliases: tuple[str, ...] = ()


BUILTIN_COMMANDS: list[CommandSpec] = [
    CommandSpec("/status",   "Show running agents and their tasks"),
    CommandSpec("/approve",  "Review and approve pending HITL tool calls"),
    CommandSpec("/history",  "Browse the event log (last 20 entries)"),
    CommandSpec("/settings", "View current configuration"),
    CommandSpec("/help",     "List available commands"),
    CommandSpec("/cancel",   "Cancel the currently running intent"),
    CommandSpec("/clear",    "Clear the transcript display"),
]

# Regex: find a slash-word optionally preceded by whitespace or start-of-line.
_SLASH_RE = re.compile(r"(?:^|\s)(\/\S*)$")


class SlashCommandCompleter(Completer):
    """Completes /commands anywhere in the input line.

    Activated when *text_before_cursor* ends with a ``/``-prefixed word
    (possibly preceded by whitespace or the start of line).
    """

    def __init__(self, commands: list[CommandSpec]) -> None:
        self._commands: list[CommandSpec] = list(commands)

    def add(self, spec: CommandSpec) -> None:
        """Dynamically register an additional slash command."""
        self._commands.append(spec)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        m = _SLASH_RE.search(text)
        if m is None:
            return
        partial = m.group(1)  # e.g. "/sta"
        for cmd in self._commands:
            candidates = (cmd.name,) + cmd.aliases
            for candidate in candidates:
                if candidate.startswith(partial):
                    yield Completion(
                        text=candidate[len(partial):],
                        start_position=0,
                        display=candidate,
                        display_meta=cmd.description,
                    )


class AtMentionCompleter(Completer):
    """Completes ``@file`` / ``@dir/file`` mentions relative to *base_path*.

    Activated whenever ``@`` appears in *text_before_cursor*; the
    last ``@`` in the text determines the active fragment.  Hidden
    entries (names starting with ``.``) are always excluded.
    """

    def __init__(self, base_path: str | Path = ".") -> None:
        self._base = Path(base_path).resolve()

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        at_idx = text.rfind("@")
        if at_idx == -1:
            return

        fragment = text[at_idx + 1:]  # e.g. "src/auth" or "" or "src/"

        # Split into directory prefix and file prefix.
        if "/" in fragment:
            dir_part, file_prefix = fragment.rsplit("/", 1)
            search_dir = self._base / dir_part
        else:
            dir_part = ""
            file_prefix = fragment
            search_dir = self._base

        if not search_dir.is_dir():
            return

        try:
            for entry in sorted(
                search_dir.iterdir(),
                key=lambda e: (not e.is_dir(), e.name),
            ):
                # Skip hidden entries.
                if entry.name.startswith("."):
                    continue
                if not entry.name.startswith(file_prefix):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display_path = (
                    f"{dir_part}/{entry.name}{suffix}"
                    if dir_part
                    else f"{entry.name}{suffix}"
                )
                remaining = display_path[len(fragment):]
                yield Completion(
                    text=remaining,
                    start_position=0,
                    display=f"@{display_path}",
                )
        except PermissionError:
            return


def _build_key_bindings() -> KeyBindings:
    """Build key bindings for the input bar.

    * ``escape`` + ``enter`` — Meta/Alt+Enter: insert ``\\n`` (multi-line)
    * ``c-j``                — Ctrl+J (ASCII linefeed): insert ``\\n``
    * ``\\x1b[13;2~``        — xterm/VTE Shift+Enter (opt-in, guarded)

    Plain ``Enter`` submits the buffer via prompt_toolkit's default handler.
    Ctrl+C behaviour is handled by :class:`prompt_toolkit.PromptSession`
    (raises :class:`KeyboardInterrupt`).
    """
    kb = KeyBindings()

    def _insert_newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    # Meta+Enter (Alt+Enter) — most reliable cross-terminal binding.
    kb.add("escape", "enter")(_insert_newline)
    # Ctrl+J (ASCII linefeed) — universal fallback.
    kb.add("c-j")(_insert_newline)

    # Attempt to bind the xterm Shift+Enter escape sequence; silently skip
    # if this prompt_toolkit version does not recognise the key.
    try:
        kb.add("\x1b[13;2~")(_insert_newline)
    except (ValueError, KeyError):
        pass

    return kb


class InputBarSession:
    """PromptSession enhanced with slash-command + @-file completers and
    Meta+Enter multi-line support.

    Usage::

        session = InputBarSession(base_path="/my/project")
        text = await session.prompt_async()   # may contain \\n
    """

    def __init__(
        self,
        commands: list[CommandSpec] | None = None,
        base_path: str | Path = ".",
        history_file: str | Path | None = None,
    ) -> None:
        from prompt_toolkit import PromptSession

        self._slash_completer = SlashCommandCompleter(
            list(commands) if commands is not None else list(BUILTIN_COMMANDS)
        )
        self._at_completer = AtMentionCompleter(base_path)
        self._completer = merge_completers(
            [self._slash_completer, self._at_completer]
        )

        kb = _build_key_bindings()

        history: FileHistory | InMemoryHistory
        if history_file is not None:
            history = FileHistory(str(history_file))
        else:
            history = InMemoryHistory()

        # Capture self in a cell so the Condition lambda avoids a forward ref.
        _self = self

        self._session: PromptSession = PromptSession(
            completer=self._completer,
            complete_while_typing=True,
            key_bindings=kb,
            history=history,
            enable_history_search=True,
            # multiline is active whenever the current buffer contains a '\n'.
            multiline=Condition(
                lambda: "\n"
                in (
                    _self._session.app.current_buffer.text
                    if _self._session.app
                    else ""
                )
            ),
            prompt_continuation="... ",
        )

    async def prompt_async(self, prefix: str = "> ") -> str:
        """Await user input; returns the full string, possibly containing ``\\n``."""
        result = await self._session.prompt_async(prefix)
        return result or ""

    def register_command(self, spec: CommandSpec) -> None:
        """Dynamically register a new slash command."""
        self._slash_completer.add(spec)
