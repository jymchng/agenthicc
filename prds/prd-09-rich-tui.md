---
title: "PRD-09: Rich TUI — Replace prompt_toolkit Application with rich"
status: draft
version: 0.1.0
created: 2025-01-01
replaces: prd-06-tui-and-observability.md (tui/app.py only)
---

# PRD-09: Rich TUI — Replace prompt_toolkit with rich

## 1. Executive Summary

The current TUI uses `prompt_toolkit`'s `Application(full_screen=True)`, which takes
over the **alternate screen buffer**. This has three painful consequences: the user
cannot scroll back through session history after the TUI exits, all previous terminal
output is hidden while the TUI runs, and the rendering code is a complex nest of
`HSplit / FloatContainer / BufferControl` widgets that is hard to iterate on.

`rich` solves all three problems. `rich.live.Live` renders **directly into the normal
terminal scroll buffer** — content accumulates naturally, the user can scroll up with
their terminal emulator, and the session is preserved in history after exit.  The
`rich` library also provides batteries-included styled output: spinners, tables, panels,
and rules that make agent activity immediately readable without manual ANSI string
construction.

The migration is **surgical**: only `tui/app.py` changes. `TranscriptModel`,
`TUIEventAdapter`, the kernel, and all existing tests are untouched. The new rendering
entry point is `InlineRenderer`, an async class that drives a `rich.live.Live` spinner
panel above a `prompt_toolkit.PromptSession` input bar — the same "Claude Code" style
the user originally requested.

---

## 2. Goals

| # | Goal |
|---|------|
| G1 | Render transcript content directly into the terminal scroll buffer — no alternate screen |
| G2 | User can scroll back through full session history in their terminal emulator at any time |
| G3 | Input prompt always at the bottom, managed by `PromptSession` + `patch_stdout()` |
| G4 | Running tool calls show live `rich.spinner.Spinner` animation updated in place |
| G5 | Slash commands (`/status`, `/approve`, `/history`, `/settings`) render as Rich Tables / Panels inline |
| G6 | HITL approval renders as a styled Rich Panel; user types y/n at the prompt |
| G7 | Headless mode (JSON-lines to stdout) unchanged |
| G8 | All 276 existing tests continue to pass with zero modifications |
| G9 | `render_frame_ansi()` stays intact for pyte E2E tests |

## 3. Non-Goals

- Full alternate-screen TUI (that is the design being replaced)
- Mouse support
- Custom color themes in v1 — Rich defaults are good
- Removing prompt_toolkit entirely — `PromptSession` stays for readline-quality input editing

---

## 4. Architecture

### 4.1 Rendering model

```
                     asyncio event loop
                           │
    ┌──────────────────────┼────────────────────────┐
    │                      │                        │
    ▼                      ▼                        ▼
EventProcessor       render_loop()            PromptSession
(kernel)           (background task)          .prompt_async()
    │                      │                        │
    ▼                      ▼                        │
TUIEventAdapter     model.render()            user types intent
    │               diff vs last_lines               │
    ▼                      │                        ▼
TranscriptModel     console.print(new_line)   on_input(text)
                           │
                    if has_running_tools:
                      live.update(spinner_panel)
                    else:
                      live.stop()
```

**Three layers run concurrently:**

1. **Transcript printer** — every 50 ms diffs `model.render()` against already-printed
   lines; new lines are `console.print()`-ed via `patch_stdout()`.
2. **Spinner Live** — a `rich.live.Live(transient=True)` block updates every 80 ms
   while at least one tool call is in the `running` state; it clears itself when all
   tools complete, leaving only the permanent printed lines above.
3. **Input bar** — `PromptSession.prompt_async()` with `patch_stdout()` ensures any
   `console.print()` from layers 1 & 2 lands above the current prompt without
   corrupting it.

### 4.2 Component map

```
tui/
  transcript.py   ← UNCHANGED  (TranscriptModel, ToolCallState, diff_lines)
  events.py       ← UNCHANGED  (TUIEventAdapter)
  app.py          ← REPLACED   (InlineRenderer replaces build_app/Application)
  __init__.py     ← UPDATED    (export InlineRenderer, run_inline)
```

### 4.3 What rich provides

| rich class | Used for |
|------------|---------|
| `Console(highlight=False, markup=False)` | Permanent transcript output |
| `Live(transient=True, refresh_per_second=12)` | Spinner panel for running tools |
| `Spinner("dots", text=...)` | Per-tool animated indicator |
| `Panel(content, title=, border_style=)` | Slash-command output boxes |
| `Table(box=box.SIMPLE)` | /status agent table, /settings config table |
| `Rule(title=)` | Agent turn headers |
| `Text(style=)` | Styled tool-call result lines (green ✓ / red ✗) |
| `Group(...)` | Compose multiple renderables into one Live update |

`patch_stdout()` from **prompt_toolkit** wraps stdout so `console.print()` calls
issued from the render loop are injected above the current prompt line atomically.

---

## 5. Data Structures and Interfaces

### 5.1 InlineRenderer

```python
# src/agenthicc/tui/app.py

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable, TextIO

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.rule import Rule
from rich import box as rich_box

from .transcript import TranscriptModel, ToolCallState
from .events import TUIEventAdapter


class InlineRenderer:
    """Renders agenthicc session output directly into the terminal scroll buffer.

    Uses rich for styled output and a prompt_toolkit PromptSession for the
    input bar so readline editing (history, Ctrl-A/E, arrow keys) works.
    """

    def __init__(
        self,
        model: TranscriptModel,
        adapter: TUIEventAdapter | None = None,
        console: Console | None = None,
    ) -> None:
        self.model = model
        self.adapter = adapter
        self.console = console or Console(highlight=False, markup=False)
        self._printed_count: int = 0        # index into model.render() already printed
        self._live: Live | None = None       # active spinner Live block, or None

    # ── main loop ────────────────────────────────────────────────────────

    async def run(self, on_input: Callable[[str], None]) -> None:
        """Start the render loop and prompt until Ctrl-C / EOF."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout

        session = PromptSession(INPUT_PROMPT)
        render_task: asyncio.Task | None = None

        with patch_stdout():
            render_task = asyncio.create_task(self._render_loop())
            try:
                while True:
                    try:
                        text = await session.prompt_async()
                    except (EOFError, KeyboardInterrupt):
                        break
                    text = text.strip()
                    if not text:
                        continue
                    handled = SlashCommandHandler().handle(text, self.model, self.console)
                    if not handled:
                        on_input(text)
            finally:
                if render_task is not None:
                    render_task.cancel()
                    await asyncio.gather(render_task, return_exceptions=True)
                if self._live is not None:
                    self._live.stop()

    # ── render loop ───────────────────────────────────────────────────────

    async def _render_loop(self) -> None:
        """Background task: print new lines and update spinner every 50 ms."""
        while True:
            await asyncio.sleep(0.05)
            if self.adapter is not None:
                self.adapter.sync()
            self._flush_new_lines()
            self._update_spinner()
            self.model.advance_spinner()

    def _flush_new_lines(self) -> None:
        """Print lines from model.render() not yet printed."""
        lines = self.model.render()
        new = lines[self._printed_count:]
        for line in new:
            # Skip lines that are just spinner-state tool calls —
            # the Live spinner panel handles those.
            self.console.print(line, markup=False, highlight=False)
        if new:
            self._printed_count = len(lines)

    def _update_spinner(self) -> None:
        """Start / update / stop the spinner Live block."""
        panel = self._build_spinner_panel()
        if panel is None:
            if self._live is not None:
                self._live.stop()
                self._live = None
        else:
            if self._live is None:
                self._live = Live(
                    panel,
                    console=self.console,
                    refresh_per_second=12,
                    transient=True,   # disappears when stopped — no permanent residue
                )
                self._live.start()
            else:
                self._live.update(panel)

    def _build_spinner_panel(self) -> Panel | None:
        """Return a Panel containing one Spinner per running tool, or None."""
        running = [
            tc for turn in self.model.turns
            for tc in turn.tool_calls
            if tc.state == ToolCallState.running
        ]
        if not running:
            return None
        from rich.console import Group as RichGroup
        rows = [
            Text.assemble(
                Spinner("dots"),
                f"  [tool] {tc.name}",
                style="dim",
            )
            for tc in running
        ]
        return Panel(RichGroup(*rows), border_style="dim", padding=(0, 1))

    def has_running_tools(self) -> bool:
        return self._build_spinner_panel() is not None
```

### 5.2 SlashCommandHandler

```python
class SlashCommandHandler:
    """Renders slash-command output as Rich Panels/Tables inline."""

    def handle(self, text: str, model: TranscriptModel, console: Console) -> bool:
        cmd = text.strip()
        if cmd == "/status":
            self._status(model, console)
            return True
        if cmd == "/history":
            self._history(model, console)
            return True
        if cmd == "/help":
            self._help(console)
            return True
        return False

    def _status(self, model: TranscriptModel, console: Console) -> None:
        table = Table(title="Agent Status", box=rich_box.SIMPLE)
        table.add_column("Agent ID", style="cyan")
        table.add_column("Name")
        table.add_column("Cost")
        table.add_column("Tokens", justify="right")
        for turn in model.turns:
            table.add_row(
                turn.agent_id[:8],
                turn.agent_name,
                f"${turn.cost_usd:.4f}",
                str(turn.tokens),
            )
        if not model.turns:
            table.add_row("—", "(no active agents)", "", "")
        console.print(table)

    def _history(self, model: TranscriptModel, console: Console) -> None:
        lines = model.render()[-20:]
        console.print(Panel("\n".join(lines) or "(empty)", title="/history — last 20 lines"))

    def _help(self, console: Console) -> None:
        table = Table(title="Slash Commands", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Description")
        for cmd, desc in SLASH_HELP.items():
            table.add_row(cmd, desc)
        console.print(table)
```

### 5.3 Updated `app.py` public API

```python
# New __all__
__all__ = [
    "INPUT_PROMPT",
    "InlineRenderer",
    "MENU_COMMANDS",
    "PROMPT_TOOLKIT_AVAILABLE",
    "RICH_AVAILABLE",
    "SlashCommandHandler",
    "detect_slash_command",
    "render_frame_ansi",   # UNCHANGED — pyte tests depend on this
    "run_headless",        # UNCHANGED
    "run_inline",          # NEW async entry point
]

async def run_inline(
    model: TranscriptModel,
    adapter: TUIEventAdapter | None = None,
    on_input: Callable[[str], None] | None = None,
) -> None:
    """Convenience wrapper: create InlineRenderer and run."""
    renderer = InlineRenderer(model, adapter)
    await renderer.run(on_input or (lambda _: None))
```

`build_app()` is deprecated — it raises `DeprecationWarning` and calls `run_inline`
via a sync wrapper so callers that used the old `app.run_async()` pattern break loudly
with a clear migration message.

---

## 6. Implementation Plan

### Phase 1 — Dependency (30 min)

1. Add `rich>=13.0` to `[project.optional-dependencies] tui` in `pyproject.toml`
2. Run `uv sync --extra tui`
3. Smoke-test: `python -c "from rich.live import Live; print('ok')"`

### Phase 2 — InlineRenderer core (2–3 h)

1. Write `InlineRenderer.__init__`, `_flush_new_lines`, `has_running_tools` (the simplest parts — no async yet)
2. Write `_build_spinner_panel` — build from `model.turns[*].tool_calls`; return `None` when empty
3. Write `_update_spinner` — create/update/stop `self._live`
4. Write `_render_loop` background task
5. Write `run()` — `PromptSession` + `patch_stdout()` + task management
6. Run unit tests: `pytest tests/unit/test_transcript.py tests/unit/test_tui_events.py -q`  (must stay green)

### Phase 3 — SlashCommandHandler (1 h)

1. `/status` → Rich Table with agent turns
2. `/history` → Panel with last 20 lines from `model.render()`
3. `/help` → Table of commands
4. `/approve` → Panel showing pending HITL tool call; prompt `y/N`; emit kernel event
5. `/settings` → Table of current `SystemSettings` fields

### Phase 4 — Update `__main__.py` (30 min)

Replace `build_app()` + `app.run_async()` with:
```python
from agenthicc.tui.app import run_inline
await run_inline(model, adapter, on_input=on_intent)
```

### Phase 5 — New tests (2 h)

Write `tests/unit/test_inline_renderer.py` and
`tests/integration/test_inline_renderer_pipeline.py` (see §7).

### Phase 6 — Full suite green (1 h)

Run `pytest tests/ -q` — fix any breakage. Update pyte test if render_frame_ansi changes
(it shouldn't).

---

## 7. Testing Strategy

### 7.1 Unchanged tests (zero modifications)

| File | Why untouched |
|------|--------------|
| `tests/unit/test_transcript.py` | `TranscriptModel` API unchanged |
| `tests/unit/test_tui_events.py` | `TUIEventAdapter` API unchanged |
| `tests/integration/test_tui_pipeline.py` | EventProcessor→adapter pipeline unchanged |
| `tests/e2e/test_tui_pyte.py` | `render_frame_ansi()` signature unchanged |

### 7.2 New: `tests/unit/test_inline_renderer.py`

```python
"""Unit tests for InlineRenderer and SlashCommandHandler (PRD-09)."""
from __future__ import annotations

import io
import pytest
from rich.console import Console

from agenthicc.tui.transcript import ToolCallState, TranscriptModel
from agenthicc.tui.events import TUIEventAdapter
from agenthicc.tui.app import InlineRenderer, SlashCommandHandler

pytestmark = pytest.mark.unit


def _console(width: int = 120) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, highlight=False, markup=False, width=width), buf


def _model_with_content() -> TranscriptModel:
    m = TranscriptModel()
    m.append_turn("a1", "agent:test", 0.0)
    m.append_line("a1", "hello from agent")
    return m


class TestInlineRendererFlush:
    def test_new_lines_printed(self):
        con, buf = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        assert "hello from agent" in buf.getvalue()

    def test_no_duplicate_on_second_flush(self):
        con, buf = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        first_output = buf.getvalue()
        r._flush_new_lines()
        # Second flush must add nothing new
        assert buf.getvalue() == first_output

    def test_new_content_printed_on_second_flush(self):
        con, buf = _console()
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        r = InlineRenderer(m, console=con)
        r._flush_new_lines()
        m.append_line("a1", "second line")
        r._flush_new_lines()
        assert "second line" in buf.getvalue()

    def test_printed_count_advances(self):
        con, _ = _console()
        m = _model_with_content()
        r = InlineRenderer(m, console=con)
        assert r._printed_count == 0
        r._flush_new_lines()
        assert r._printed_count == len(m.render())


class TestSpinnerPanel:
    def test_none_when_no_running_tools(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_panel_when_tool_running(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "read_file")
        r = InlineRenderer(m, console=_console()[0])
        panel = r._build_spinner_panel()
        assert panel is not None

    def test_none_after_tool_completes(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "write_file")
        m.update_tool_call("tc1", state=ToolCallState.success, duration_ms=5.0)
        r = InlineRenderer(m, console=_console()[0])
        assert r._build_spinner_panel() is None

    def test_has_running_tools_true_and_false(self):
        m = TranscriptModel()
        m.append_turn("a1", "agent:test", 0.0)
        m.add_tool_call("a1", "tc1", "slow_op")
        r = InlineRenderer(m, console=_console()[0])
        assert r.has_running_tools()
        m.update_tool_call("tc1", state=ToolCallState.success)
        assert not r.has_running_tools()


class TestSlashCommandHandler:
    def test_status_renders_table(self):
        con, buf = _console()
        m = _model_with_content()
        h = SlashCommandHandler()
        result = h.handle("/status", m, con)
        assert result is True
        assert "agent" in buf.getvalue().lower() or "Agent" in buf.getvalue()

    def test_history_renders_panel(self):
        con, buf = _console()
        m = _model_with_content()
        h = SlashCommandHandler()
        result = h.handle("/history", m, con)
        assert result is True
        assert "history" in buf.getvalue().lower() or "hello" in buf.getvalue()

    def test_unknown_command_returns_false(self):
        con, _ = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        assert h.handle("not a command", m, con) is False

    def test_help_renders_table(self):
        con, buf = _console()
        m = TranscriptModel()
        h = SlashCommandHandler()
        result = h.handle("/help", m, con)
        assert result is True
```

### 7.3 New: `tests/integration/test_inline_renderer_pipeline.py`

```python
"""Integration: InlineRenderer subscribes to a live EventProcessor (PRD-09)."""
from __future__ import annotations

import asyncio
import io
import pytest
from rich.console import Console

from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tui.transcript import TranscriptModel
from agenthicc.tui.events import TUIEventAdapter
from agenthicc.tui.app import InlineRenderer

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


async def test_new_lines_printed_after_ui_update(proc):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)
    renderer = InlineRenderer(model, adapter, console=con)

    await proc.emit(Event.create(
        "UIUpdate",
        {"content": "hello from integration test", "ui_type": "message"},
        source_agent_id="a1",
    ))
    await proc.drain()

    adapter.sync()
    renderer._flush_new_lines()
    assert "hello from integration test" in buf.getvalue()


async def test_tool_running_then_complete(proc):
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(proc)
    renderer = InlineRenderer(model, adapter, console=con)

    await proc.emit(Event.create("AgentSpawnRequest", {"agent_id": "a1", "agent_type": "T"}, source_agent_id="a1"))
    await proc.emit(Event.create("ToolCallStarted", {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1"}, source_agent_id="a1"))
    await proc.drain()
    adapter.sync()
    assert renderer.has_running_tools()

    await proc.emit(Event.create("ToolCallComplete", {"tool_name": "read_file", "tool_use_id": "tc1", "agent_id": "a1", "success": True, "duration_ms": 8.0}))
    await proc.drain()
    adapter.sync()
    assert not renderer.has_running_tools()
```

### 7.4 Pyte tests — zero changes

`render_frame_ansi(model, cols, rows, ...)` is **not modified**. All pyte assertions
in `tests/e2e/test_tui_pyte.py` continue to work exactly as before.

---

## 8. Backward Compatibility

| Symbol | Before | After | Breaking? |
|--------|--------|-------|-----------|
| `build_app()` | Returns `Application(full_screen=True)` | Raises `DeprecationWarning`; falls back to raising `RuntimeError` with migration message | **Yes** — callers must switch to `run_inline()` |
| `render_frame_ansi()` | Unchanged | Unchanged | No |
| `run_headless()` | Unchanged | Unchanged | No |
| `TranscriptModel` | Unchanged | Unchanged | No |
| `TUIEventAdapter` | Unchanged | Unchanged | No |
| `PROMPT_TOOLKIT_AVAILABLE` | `True` when PT installed | Same | No |
| `RICH_AVAILABLE` | Did not exist | `True` when rich installed | Additive |
| `pyproject.toml tui extra` | `prompt_toolkit>=3.0` | `rich>=13.0, prompt_toolkit>=3.0` | Additive |
| `InlineRenderer` | Did not exist | New class | Additive |
| `run_inline()` | Did not exist | New async entry point | Additive |

---

## 9. Rich Component Usage Reference

```
Session output (permanent, scroll buffer):
─────────────────────────────────────────
● agent:planner  12:34:01                      ← console.print(Rule(...))
  Planning Argon2 refactor...                  ← console.print(line)
  [tool] read_file auth/hashing.py  ✓  12ms   ← console.print(Text("✓ ...", style="green"))
  [tool] write_file auth/hashing.py ✓  8ms

● agent:tester  12:34:05
  Running test suite...
┌───────────────────────────────┐             ← Live(transient=True) spinner panel
│  ⠋  [tool] run_tests          │               floats here, updated every 80ms,
└───────────────────────────────┘               disappears when all tools finish

 session-abc | 3 agents | $0.008              ← printed to console directly

> _                                            ← PromptSession input, managed by
                                                 patch_stdout so prints never
                                                 corrupt the prompt
```

---

## 10. Open Questions

1. **Live + patch_stdout interaction** — `rich.live.Live` internally redirects
   `sys.stdout`; `prompt_toolkit.patch_stdout()` does too. Are they composable?
   Preliminary answer: yes — `patch_stdout()` wraps at the file-descriptor level
   while `Live` wraps at the Python stream level; stack order matters: enter
   `patch_stdout()` first, then start `Live`. Verify in implementation.

2. **`transient=True` on the spinner** — the spinner panel disappears when `live.stop()`
   is called. If a tool fails, should its error be printed as a permanent line before
   stopping the Live? Yes — `TranscriptModel.update_tool_call(state=failure)` → the
   next `_flush_new_lines()` call prints the ✗ line permanently, then `_update_spinner`
   stops the Live because no running tools remain.

3. **Concurrent Live blocks** — `rich.live.Live` docs warn that only one Live block
   should be active per console at a time. Since we only ever have one spinner panel
   (aggregating all running tools), this is not a problem.

4. **Input history persistence** — `PromptSession(history=FileHistory(".agenthicc/history"))`
   would persist command history across sessions. Add as a follow-up.

5. **Rich markup in agent output** — agents calling `application_ui_update` with Rich
   markup strings (`[bold red]error[/]`) will render as literal text with `markup=False`.
   A future `ui_type: "rich"` payload flag could opt into markup rendering.
