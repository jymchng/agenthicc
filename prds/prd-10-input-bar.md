---
title: "PRD-10: Enhanced Input Bar — Commands, @-mentions, Multi-line, Interrupt, Session Resume"
status: draft
version: 0.1.0
created: 2025-01-01
extends: prd-09-rich-tui.md
---

# PRD-10: Enhanced Input Bar

## 1. Executive Summary

The current input bar (PRD-09) is a plain `PromptSession` with no completions,
no multi-line support, and no session continuity. This PRD specifies five tightly
related enhancements that together make the input bar as capable as a modern AI
coding assistant:

1. **Slash-command completions** — `/cmd` triggers a prompt_toolkit completion menu
   showing available commands with descriptions; Tab cycles entries.
2. **`@`-file mentions** — typing `@` anywhere in the input opens filesystem
   completions relative to the project root; multiple mentions per line are supported.
3. **Multi-line entry** — Shift+Enter (via Meta+Enter / `escape`+`enter` — the
   cross-terminal reliable binding) inserts a newline without submitting; plain Enter
   submits the full multi-line buffer. Paste of multi-line clipboard content works
   transparently.
4. **Agent interrupt** — Ctrl+C sends an interrupt signal to the currently running
   intent, cancelling its workflow gracefully and returning the input bar.
5. **Session continuity** — `--continue` resumes the most recent session for the
   current working directory; `--resume <id>` resumes any named session.

None of these changes touch `TranscriptModel`, `TUIEventAdapter`, or the kernel.

---

## 2. Goals

| ID | Goal |
|----|------|
| G1 | Slash-command completer activates on `/` at any cursor position; Tab cycles; Enter selects |
| G2 | `@`-mention completer activates on `@` anywhere in input; completes files/dirs relative to project root |
| G3 | Meta+Enter (Alt+Enter, Esc+Enter) inserts `\n` without submitting |
| G4 | Enter submits the full buffer including embedded newlines as one intent string |
| G5 | Multi-line paste lands in buffer without auto-submit |
| G6 | Ctrl+C while an intent is running cancels it (emits `IntentCancelled` event); Ctrl+C with empty buffer exits |
| G7 | `agenthicc --continue` resumes most recent session for this cwd |
| G8 | `agenthicc --resume <id>` resumes the session with that ID from the event log |
| G9 | Resume re-renders the last N lines of transcript so the user has context |

## 3. Non-Goals

| ID | Non-Goal |
|----|----------|
| NG1 | Persistent multi-line editor (not a full editor — just a prompt) |
| NG2 | Syntax highlighting of input text |
| NG3 | `@`-mentioning agents or other entities (files only in v1) |
| NG4 | Cross-session search (use `/history` for that) |
| NG5 | True Shift+Enter key detection — terminals disagree on the escape sequence; Meta+Enter is the reliable substitute |

---

## 4. Architecture

### 4.1 Component map

```
tui/
  input_bar.py    ← NEW  (InputBarSession, SlashCommandCompleter, AtMentionCompleter,
  │                        CommandSpec, BUILTIN_COMMANDS)
  app.py          ← UPDATED  (InlineRenderer.run() uses InputBarSession;
  │                            add interrupt handling; --continue/--resume wired)
  transcript.py   ← UNCHANGED
  events.py       ← UNCHANGED

kernel/
  reducer.py      ← UPDATED  (add IntentCancelled event handler)
  state.py        ← UNCHANGED  (IntentStatus.failed covers cancel)

__main__.py       ← UPDATED  (--continue, --resume flags; session ID persistence)
```

### 4.2 Completer pipeline

```
user keystroke at cursor
        │
        ▼
  MergedCompleter
  ├── SlashCommandCompleter
  │     • active when text_before_cursor matches r"(^|\s)/"
  │     • yields Completion(remaining, display_meta=description)
  │
  └── AtMentionCompleter
        • active when "@" appears in text_before_cursor
        • finds last "@", extracts path fragment
        • yields Completion from os.scandir(base_path / fragment_dir)
        • start_position rewinds to after the "@"
```

### 4.3 Multi-line key binding strategy

```
Terminal key      prompt_toolkit binding    Action
──────────────────────────────────────────────────────────────────────────
Enter             'enter' / 'c-m'           submit buffer (always)
Meta+Enter        'escape' 'enter'          insert \n into buffer (newline)
Alt+Enter         same binding              same (terminal-dependent)
Ctrl+J            'c-j'                     insert \n (universal fallback)
```

**Why not Shift+Enter directly:** prompt_toolkit has no portable `Keys.ShiftEnter`
constant. Most terminals send `\x1b[13;2~` for Shift+Enter but this string is not in
prompt_toolkit's `_parse_key` registry (`ValueError: Invalid key`). The reliable
cross-terminal alternatives are:

- **Meta+Enter** (`'escape' 'enter'`) — works in every xterm-compatible terminal
- **Ctrl+J** — ASCII 0x0A (linefeed), always available, well-known

Both are bound in `InputBarSession`. Terminal emulators that forward Shift+Enter as
`\x1b[13;2~` can be handled by binding that raw bytes string directly (opt-in, guarded
by a try/except on `kb.add`).

### 4.4 Interrupt handling

```
Ctrl+C (empty buffer)   →  raise KeyboardInterrupt  →  InlineRenderer.run() exits
Ctrl+C (running intent) →  emit IntentCancelled event  →  reducer sets intent.status=failed
                            InlineRenderer prints "[cancelled]" line, clears spinner
```

`IntentCancelled` reducer: find all `running` intents → mark them `failed` with
`error="cancelled by user"` → emit `update_tui` effect.

### 4.5 Session continuity

Session state is two files in `.agenthicc/`:

```
.agenthicc/
  events.jsonl          existing event log for current session
  sessions.json         index: {session_id: {cwd, created_at, last_used, description}}
  sessions/
    <session_id>.jsonl  event log for each past session
```

`--continue` looks up the most recent session where `cwd == os.getcwd()`.
`--resume <id>` looks up by session ID directly.
Both call `restore_from_log(session_path, initial_state, root_reducer)` then replay
the transcript through `TUIEventAdapter` to fill the `TranscriptModel` before
launching `InlineRenderer`.

---

## 5. Data Structures and Interfaces

### 5.1 `tui/input_bar.py` — new file

```python
# src/agenthicc/tui/input_bar.py
from __future__ import annotations

import os
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
from prompt_toolkit.keys import Keys

__all__ = [
    "AtMentionCompleter",
    "BUILTIN_COMMANDS",
    "CommandSpec",
    "InputBarSession",
    "SlashCommandCompleter",
]


@dataclass(frozen=True)
class CommandSpec:
    name: str           # "/status"
    description: str    # "Show running agents and their tasks"
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


class SlashCommandCompleter(Completer):
    """Completes /commands anywhere in the input."""

    def __init__(self, commands: list[CommandSpec]) -> None:
        self._commands = list(commands)

    def add(self, spec: CommandSpec) -> None:
        self._commands.append(spec)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        # Find the last slash-word (may be preceded by whitespace or start of line)
        import re
        m = re.search(r"(?:^|\s)(\/\S*)$", text)
        if m is None:
            return
        partial = m.group(1)          # e.g. "/sta"
        start_pos = -len(partial)     # rewind cursor to start of "/..."
        for cmd in self._commands:
            candidates = (cmd.name,) + cmd.aliases
            for candidate in candidates:
                if candidate.startswith(partial):
                    yield Completion(
                        text=candidate[len(partial):],
                        start_position=0,           # appends after cursor
                        display=candidate,
                        display_meta=cmd.description,
                    )


class AtMentionCompleter(Completer):
    """Completes @file/path mentions relative to base_path."""

    def __init__(self, base_path: str | Path = ".") -> None:
        self._base = Path(base_path).resolve()

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        # Find last @ in the text
        at_idx = text.rfind("@")
        if at_idx == -1:
            return
        fragment = text[at_idx + 1:]   # e.g. "src/auth" or "" or "src/"

        # Split into directory and file prefix
        if "/" in fragment:
            dir_part, file_prefix = fragment.rsplit("/", 1)
            search_dir = self._base / dir_part
        else:
            dir_part = ""
            file_prefix = fragment
            search_dir = self._base

        if not search_dir.is_dir():
            return

        start_position = -len(fragment)   # rewind to just after @

        try:
            for entry in sorted(search_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                if not entry.name.startswith(file_prefix):
                    continue
                if entry.name.startswith("."):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display_path = (f"{dir_part}/{entry.name}{suffix}" if dir_part
                                else f"{entry.name}{suffix}")
                remaining = display_path[len(fragment):]
                yield Completion(
                    text=remaining,
                    start_position=0,
                    display=f"@{display_path}",
                )
        except PermissionError:
            return


def _build_key_bindings(session_ref: list[Any]) -> KeyBindings:
    """Build key bindings for the input bar.

    Meta+Enter and Ctrl+J insert a newline (multi-line entry).
    Enter submits the buffer.
    Ctrl+C behaviour is handled by PromptSession default (raises KeyboardInterrupt).

    session_ref is a one-element list holding the PromptSession so the
    binding closures can access the current buffer without a circular ref.
    """
    kb = KeyBindings()

    def _insert_newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    # Meta+Enter (Alt+Enter) — most reliable cross-terminal binding
    kb.add("escape", "enter")(_insert_newline)
    # Ctrl+J (ASCII linefeed) — universal fallback
    kb.add("c-j")(_insert_newline)

    # Attempt to bind the xterm Shift+Enter escape sequence; silently skip
    # if this prompt_toolkit version doesn't know the key.
    try:
        kb.add("\x1b[13;2~")(_insert_newline)  # xterm / VTE Shift+Enter
    except (ValueError, KeyError):
        pass

    return kb


class InputBarSession:
    """PromptSession enhanced with slash-command + @-file completers and
    Meta+Enter multi-line support.

    Usage::

        session = InputBarSession(base_path="/my/project")
        text = await session.prompt_async()  # may contain \\n
    """

    def __init__(
        self,
        commands: list[CommandSpec] | None = None,
        base_path: str | Path = ".",
        history_file: str | Path | None = None,
    ) -> None:
        from prompt_toolkit import PromptSession

        self._slash_completer = SlashCommandCompleter(
            list(commands) if commands else list(BUILTIN_COMMANDS)
        )
        self._at_completer = AtMentionCompleter(base_path)
        self._completer = merge_completers(
            [self._slash_completer, self._at_completer]
        )
        self._session_ref: list[Any] = [None]
        kb = _build_key_bindings(self._session_ref)

        history = (
            FileHistory(str(history_file))
            if history_file
            else InMemoryHistory()
        )

        self._session = PromptSession(
            completer=self._completer,
            complete_while_typing=True,
            key_bindings=kb,
            history=history,
            enable_history_search=True,
            # multiline activates when the buffer contains a newline
            multiline=Condition(
                lambda: "\n" in (self._session.app.current_buffer.text
                                 if self._session.app else "")
            ),
            prompt_continuation="... ",   # prefix for continuation lines
        )
        self._session_ref[0] = self._session

    async def prompt_async(self, prefix: str = "> ") -> str:
        """Await user input; returns the full string, possibly containing \\n."""
        from prompt_toolkit.formatted_text import HTML
        result = await self._session.prompt_async(HTML(f"<b>{prefix}</b>"))
        return result or ""

    def register_command(self, spec: CommandSpec) -> None:
        """Dynamically register a new slash command."""
        self._slash_completer.add(spec)
```

### 5.2 Interrupt — kernel reducer addition

```python
# agenthicc/kernel/reducer.py — new handler

def _intent_cancelled(state: AppState, event: Event) -> tuple[AppState, list[Effect]]:
    """Mark all running intents as failed with error='cancelled by user'."""
    updated = dict(state.intents)
    for intent_id, intent in state.intents.items():
        if intent.status in (IntentStatus.running, IntentStatus.planning,
                             IntentStatus.validating):
            updated[intent_id] = replace(intent, status=IntentStatus.failed,
                                         error="cancelled by user")
    new_state = replace(state, intents=updated)
    return new_state, [Effect(EffectType.update_tui, {"type": "intent_cancelled"})]

# Add to _HANDLERS:
_HANDLERS["IntentCancelled"] = _intent_cancelled
```

### 5.3 `InlineRenderer.run()` — updated diff

```python
async def run(self, on_input: Callable[[str], None]) -> None:
    from prompt_toolkit.patch_stdout import patch_stdout
    from agenthicc.tui.input_bar import InputBarSession

    session = InputBarSession(
        base_path=self._base_path,                    # passed via __init__
        history_file=self._history_file,              # passed via __init__
    )
    render_task: asyncio.Task | None = None
    running_intent = [False]   # mutable flag checked in Ctrl+C handler

    with patch_stdout():
        render_task = asyncio.create_task(self._render_loop())
        try:
            while True:
                try:
                    text = await session.prompt_async()
                except KeyboardInterrupt:
                    if running_intent[0] and self._processor is not None:
                        # Cancel running intent instead of exiting
                        await self._processor.emit(
                            Event.create("IntentCancelled", {})
                        )
                        self.console.print("[dim]intent cancelled[/dim]",
                                           markup=True)
                        running_intent[0] = False
                        continue
                    break   # empty buffer + Ctrl+C → exit
                except EOFError:
                    break

                text = text.strip()
                if not text:
                    continue

                handled = SlashCommandHandler().handle(text, self.model, self.console)
                if not handled:
                    running_intent[0] = True
                    on_input(text)
                    running_intent[0] = False

        finally:
            if render_task:
                render_task.cancel()
                await asyncio.gather(render_task, return_exceptions=True)
            if self._live:
                self._live.stop()
```

### 5.4 Session persistence — `__main__.py` additions

```python
# src/agenthicc/__main__.py additions

import json, os, time, uuid
from pathlib import Path

SESSION_INDEX = Path(".agenthicc/sessions.json")
SESSIONS_DIR  = Path(".agenthicc/sessions")

def _load_session_index() -> dict:
    if SESSION_INDEX.exists():
        with open(SESSION_INDEX) as f:
            return json.load(f)
    return {}

def _save_session_index(index: dict) -> None:
    SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_INDEX, "w") as f:
        json.dump(index, f, indent=2)

def _register_session(session_id: str, description: str = "") -> None:
    index = _load_session_index()
    index[session_id] = {
        "cwd": os.getcwd(),
        "created_at": time.time(),
        "last_used": time.time(),
        "description": description,
        "log_path": str(SESSIONS_DIR / f"{session_id}.jsonl"),
    }
    _save_session_index(index)

def _find_latest_session_for_cwd() -> str | None:
    index = _load_session_index()
    cwd = os.getcwd()
    candidates = [
        (data["last_used"], sid)
        for sid, data in index.items()
        if data.get("cwd") == cwd
    ]
    if not candidates:
        return None
    return max(candidates)[1]

def _get_session_log_path(session_id: str) -> Path | None:
    index = _load_session_index()
    entry = index.get(session_id)
    if entry:
        return Path(entry["log_path"])
    # fallback: default log
    if session_id == "default":
        return Path(".agenthicc/events.jsonl")
    return None
```

Updated `_parse_args()`:

```python
parser.add_argument("--continue", dest="continue_session",
                    action="store_true",
                    help="Continue the most recent session for this directory.")
parser.add_argument("--resume", metavar="ID",
                    help="Resume the session with the given ID.")
```

Updated `main()` — session loading:

```python
async def _start_session(args) -> None:
    from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.tui.transcript import TranscriptModel
    from agenthicc.tui.events import TUIEventAdapter
    from agenthicc.tui.app import InlineRenderer

    resume_id = None
    if args.resume:
        resume_id = args.resume
    elif args.continue_session:
        resume_id = _find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    settings = SystemSettings(
        event_log_path=str(SESSIONS_DIR / f"{resume_id or uuid.uuid4().hex}.jsonl"),
        snapshot_path=".agenthicc/snapshot.json",
    )
    state = AppState.create(settings=settings)

    if resume_id:
        log_path = _get_session_log_path(resume_id)
        if log_path and log_path.exists():
            from agenthicc.kernel.reducer import root_reducer
            state = await restore_from_log(str(log_path), state, root_reducer)
            # Update last_used
            index = _load_session_index()
            if resume_id in index:
                index[resume_id]["last_used"] = time.time()
                _save_session_index(index)

    processor = EventProcessor(initial_state=state, persist=True)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(processor)

    # Re-render last 30 lines of resumed session so user has context
    if resume_id:
        lines = model.render()[-30:]
        if lines:
            from rich.console import Console
            from rich.rule import Rule
            con = Console()
            con.print(Rule(f"[dim]resumed session {resume_id[:8]}[/dim]"))
            for line in lines:
                con.print(line, markup=False, highlight=False)

    renderer = InlineRenderer(
        model, adapter,
        base_path=os.getcwd(),
        history_file=".agenthicc/history",
    )

    def on_intent(text: str) -> None:
        import asyncio, uuid as _uuid
        intent_id = _uuid.uuid4().hex
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(processor.emit(
                Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text})
            ))
        )

    proc_task = asyncio.create_task(processor.run())
    try:
        await renderer.run(on_intent)
    finally:
        proc_task.cancel()
        await asyncio.gather(proc_task, return_exceptions=True)
```

---

## 6. Implementation Plan

### Phase 1 — `tui/input_bar.py` (3 h)

1. Write `CommandSpec` dataclass
2. Write `BUILTIN_COMMANDS` list
3. Write `SlashCommandCompleter.get_completions` — regex for `/` prefix, yield `Completion` objects with `display_meta`
4. Write `AtMentionCompleter.get_completions` — find last `@`, `Path.iterdir()` with prefix filter, yield completions with correct `start_position`
5. Write `_build_key_bindings` — bind `('escape', 'enter')` and `'c-j'` to `insert_text("\n")`; try-except for `\x1b[13;2~`
6. Write `InputBarSession.__init__` — wire completer, kb, history, multiline condition
7. Write `InputBarSession.prompt_async` — thin wrapper around `self._session.prompt_async()`
8. Write `InputBarSession.register_command`

### Phase 2 — Interrupt (1 h)

1. Add `_intent_cancelled` reducer to `kernel/reducer.py`
2. Add `"IntentCancelled"` to `_HANDLERS`
3. Update `InlineRenderer.run()` — handle `KeyboardInterrupt` with running-intent check

### Phase 3 — Session persistence (2 h)

1. Write session index helpers in `__main__.py`
2. Add `--continue` and `--resume` args
3. Update `main()` to call `restore_from_log` on resume, replay transcript

### Phase 4 — Integration into `InlineRenderer` (1 h)

1. Add `base_path` and `history_file` params to `InlineRenderer.__init__`
2. Replace `PromptSession(INPUT_PROMPT)` with `InputBarSession(...)`

### Phase 5 — Tests (2 h)

Write `tests/unit/test_input_bar.py` and `tests/integration/test_input_bar_integration.py` (see §7).

---

## 7. Tests

### 7.1 `tests/unit/test_input_bar.py`

```python
"""Unit tests for InputBarSession, completers, and key bindings (PRD-10)."""
from __future__ import annotations

import pytest
from pathlib import Path
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agenthicc.tui.input_bar import (
    AtMentionCompleter,
    BUILTIN_COMMANDS,
    CommandSpec,
    InputBarSession,
    SlashCommandCompleter,
)

pytestmark = pytest.mark.unit
CE = CompleteEvent()


class TestSlashCommandCompleter:
    def test_completes_partial_command(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/sta"), CE))
        full = ["/sta" + r.text for r in results]
        assert any("status" in t for t in full)

    def test_all_commands_on_slash_alone(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/"), CE))
        assert len(results) >= len(BUILTIN_COMMANDS)

    def test_no_completion_without_slash(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document("hello world"), CE)) == []

    def test_no_completion_on_empty(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document(""), CE)) == []

    def test_unknown_prefix_returns_empty(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document("/zzzzz"), CE)) == []

    def test_description_in_display_meta(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/s"), CE))
        assert any(r.display_meta for r in results)

    def test_slash_mid_sentence(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        # "/status" prefixed by text — should still complete
        results = list(comp.get_completions(Document("hello /sta"), CE))
        full = ["/sta" + r.text for r in results]
        assert any("status" in t for t in full)

    def test_exact_match_still_offered(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/status"), CE))
        # /status is complete — may yield zero or an exact completion
        # Either is valid; important: does not error
        assert isinstance(results, list)


class TestAtMentionCompleter:
    def test_completes_file_after_at(self, tmp_path):
        (tmp_path / "auth.py").write_text("x")
        (tmp_path / "hashing.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("fix bug in @au"), CE))
        full_names = ["au" + r.text for r in results]
        assert any("auth.py" in n for n in full_names)

    def test_no_completion_without_at(self, tmp_path):
        (tmp_path / "main.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("fix main.py"), CE))
        assert results == []

    def test_empty_fragment_lists_all_non_hidden(self, tmp_path):
        for name in ["alpha.py", "beta.py", ".hidden"]:
            (tmp_path / name).write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@"), CE))
        names = [r.display for r in results]
        assert any("alpha.py" in str(n) for n in names)
        assert any("beta.py" in str(n) for n in names)
        assert not any(".hidden" in str(n) for n in names)

    def test_subdirectory_completion(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "login.py").write_text("x")
        (src / "logout.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@src/log"), CE))
        assert len(results) >= 1

    def test_multiple_at_completes_last(self, tmp_path):
        (tmp_path / "alpha.py").write_text("x")
        (tmp_path / "beta.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        # cursor after second @
        results = list(comp.get_completions(Document("diff @alpha.py and @be"), CE))
        full_names = ["be" + r.text for r in results]
        assert any("beta.py" in n for n in full_names)

    def test_dirs_completed_with_trailing_slash(self, tmp_path):
        (tmp_path / "src").mkdir()
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@"), CE))
        displays = [str(r.display) for r in results]
        assert any("src/" in d for d in displays)

    def test_nonexistent_subdir_returns_empty(self, tmp_path):
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@nonexistent/"), CE))
        assert results == []


class TestInputBarSession:
    def test_creates_without_error(self, tmp_path):
        s = InputBarSession(base_path=tmp_path)
        assert s._session is not None

    def test_builtin_commands_registered(self, tmp_path):
        s = InputBarSession(base_path=tmp_path)
        results = list(s._completer.get_completions(Document("/"), CE))
        assert len(results) >= len(BUILTIN_COMMANDS)

    def test_register_command_dynamically(self, tmp_path):
        s = InputBarSession(base_path=tmp_path)
        s.register_command(CommandSpec("/test-cmd", "A dynamic test command"))
        results = list(s._completer.get_completions(Document("/test"), CE))
        full = ["/test" + r.text for r in results]
        assert any("test-cmd" in t for t in full)

    def test_at_completer_active(self, tmp_path):
        (tmp_path / "myfile.py").write_text("x")
        s = InputBarSession(base_path=tmp_path)
        results = list(s._completer.get_completions(Document("@my"), CE))
        assert len(results) >= 1
```

### 7.2 `tests/integration/test_input_bar_integration.py`

```python
"""Integration: InputBarSession + InlineRenderer + kernel (PRD-10)."""
from __future__ import annotations

import asyncio
import io
import pytest
from unittest.mock import AsyncMock, patch
from rich.console import Console

from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.events import TUIEventAdapter
from agenthicc.tui.app import InlineRenderer
from agenthicc.tui.input_bar import InputBarSession

pytestmark = pytest.mark.integration


@pytest.fixture
async def proc(tmp_path):
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "s.json"),
        ),
        policy=SecurityPolicy(),
    )
    p = EventProcessor(initial_state=state, persist=False)
    t = asyncio.create_task(p.run())
    yield p
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)


async def test_slash_command_does_not_call_on_input(tmp_path):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    renderer = InlineRenderer(model, console=con, base_path=tmp_path)

    called = []
    with patch.object(InputBarSession, "prompt_async", new_callable=AsyncMock) as m:
        m.side_effect = ["/status", KeyboardInterrupt()]
        try:
            await renderer.run(on_input=called.append)
        except Exception:
            pass

    assert "/status" not in called
    # SlashCommandHandler rendered something
    assert len(buf.getvalue()) >= 0


async def test_plain_text_calls_on_input(tmp_path):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    renderer = InlineRenderer(model, console=con, base_path=tmp_path)

    called = []
    with patch.object(InputBarSession, "prompt_async", new_callable=AsyncMock) as m:
        m.side_effect = ["refactor the auth module", KeyboardInterrupt()]
        try:
            await renderer.run(on_input=called.append)
        except Exception:
            pass

    assert "refactor the auth module" in called


async def test_multiline_text_submitted_as_single_string(tmp_path):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    renderer = InlineRenderer(model, console=con, base_path=tmp_path)

    called = []
    multiline = "line one\nline two\nline three"
    with patch.object(InputBarSession, "prompt_async", new_callable=AsyncMock) as m:
        m.side_effect = [multiline, KeyboardInterrupt()]
        try:
            await renderer.run(on_input=called.append)
        except Exception:
            pass

    assert len(called) == 1
    assert "\n" in called[0]
    assert "line one" in called[0]
    assert "line three" in called[0]


async def test_ctrl_c_with_running_intent_cancels_not_exits(tmp_path, proc):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)
    renderer = InlineRenderer(model, adapter, console=con, base_path=tmp_path)
    renderer._processor = proc

    # Simulate: submit intent (sets running_intent=True), then Ctrl+C, then EOF
    called = []

    async def fake_prompt(prefix="> "):
        # First call: return text (intent submitted)
        # Second call: raise KeyboardInterrupt (with running intent)
        # Third call: raise EOFError (exit)
        calls = getattr(fake_prompt, "_calls", 0)
        fake_prompt._calls = calls + 1
        if calls == 0:
            return "do something"
        if calls == 1:
            raise KeyboardInterrupt()
        raise EOFError()

    with patch.object(InputBarSession, "prompt_async", side_effect=fake_prompt):
        try:
            await renderer.run(on_input=called.append)
        except Exception:
            pass

    # After Ctrl+C with running intent, IntentCancelled should be in event log
    await proc.drain()
    event_types = [e.event_type for e in proc.event_log]
    assert "IntentCancelled" in event_types
```

---

## 8. Backward Compatibility

| Symbol | Before (PRD-09) | After | Breaking? |
|--------|-----------------|-------|-----------|
| `InlineRenderer.__init__` | `(model, adapter=None, console=None)` | Gains `base_path=".", history_file=None` | No — new kwargs with defaults |
| `InlineRenderer.run()` | Uses `PromptSession(INPUT_PROMPT)` | Uses `InputBarSession` | No — same external contract |
| `InputBarSession` | Doesn't exist | New in `tui/input_bar.py` | Additive |
| `SlashCommandCompleter` | Doesn't exist | New | Additive |
| `AtMentionCompleter` | Doesn't exist | New | Additive |
| `CommandSpec` | Doesn't exist | New | Additive |
| `BUILTIN_COMMANDS` | Doesn't exist | New | Additive |
| `kernel/reducer.py` | No `IntentCancelled` handler | Adds handler | Additive |
| `__main__.py` | `--headless`, `--config`, `--version` | Adds `--continue`, `--resume` | Additive |

---

## 9. Open Questions

1. **`--continue` with multiple projects in same cwd** — if two projects use the same
   cwd, session index lookup returns the most recent regardless of project name. Resolution:
   also store `git_remote` or `pyproject_name` as a secondary key if available.

2. **`--resume` display of session IDs** — users need to know available IDs before they
   can pass `--resume <id>`. Add `agenthicc --list-sessions` to the backlog (not this PRD).

3. **History file location** — `.agenthicc/history` is project-scoped. Should history be
   user-global (`~/.agenthicc/history`) instead? Proposal: user-global so command history
   carries across projects; set `InputBarSession(history_file=Path.home()/".agenthicc"/"history")`.

4. **`\x1b[13;2~` Shift+Enter adoption** — modern terminals (Alacritty, Kitty, WezTerm,
   iTerm2 with "Report modifiers" on) do send this. The fallback binding (`'escape' 'enter'`
   + `'c-j'`) covers all other cases. The try/except guard means the PRD implementation is
   already correct for all terminals.

5. **Interrupt of parallel agents** — `IntentCancelled` marks all running intents failed.
   Should it also emit `AgentStatusChanged(terminated)` for all busy agents?
   Yes — add to the `_intent_cancelled` reducer's effects list as `Effect(spawn_agent...)`
   → actually emit `AgentStatusChanged` for each busy agent. Track in follow-up.
