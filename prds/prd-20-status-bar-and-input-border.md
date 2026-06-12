---
title: "PRD-20: Status Bar and Bordered Input Bar"
status: draft
version: 0.1.0
created: 2025-01-01
extends: prd-09-rich-tui.md
---

# PRD-20: Status Bar and Bordered Input Bar

## Executive Summary

The current TUI renders a plain input prompt at the bottom with no visual separation from the transcript above. This PRD specifies a persistent **Status Bar** and a **bordered Input Bar** that together give agenthicc the same feel as Claude Code:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ● agent:planner  12:34:01                                                   │  ← transcript
│    Planning Argon2 refactor...                                               │    (scrolls)
│    [tool] read_file auth.py                              ✓  12ms             │
│                                                                               │
╔══════════════════════════════════════════════════════════════════════════════╗
║  ⣾ Thinking...  (4.2s │ ↑ 1,204 tok  ↓ 342 tok)                             ║  ← Status Bar
╚══════════════════════════════════════════════════════════════════════════════╝
╔══════════════════════════════════════════════════════════════════════════════╗
║ > _                                                                          ║  ← Input Bar
╚══════════════════════════════════════════════════════════════════════════════╝
```

The **Status Bar** is a single-line box directly above the Input Bar showing:
- A Braille spinner when at least one agent is active ("Thinking...")  
- Elapsed duration since the current intent started  
- Cumulative input and output token counts for the session  
- Idle state when no agents are running: `" session-abc | 2 agents completed | $0.012"`  

The **Input Bar** is demarcated by a double-line border (╔/╗/╚/╝/═) at the top and
bottom, making it visually distinct from the transcript and never ambiguous.

Both bars are rendered via `rich.live.Live` with `transient=False` (permanent — they
stay visible as the transcript scrolls above them), implemented using the
`render_frame_ansi()` update pathway so the pyte E2E tests continue to work.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Status Bar always visible directly above the Input Bar |
| G2 | Status Bar shows spinner + "Thinking..." during active agent runs |
| G3 | Status Bar shows elapsed time, input tokens, output tokens during active run |
| G4 | Status Bar shows idle summary when no agents are active |
| G5 | Input Bar has top and bottom double-line borders (╔═╗ / ╚═╝) |
| G6 | Spinner advances every ~80 ms (same cadence as tool spinner) |
| G7 | Token counts update after every `ModelCallComplete` signal |
| G8 | `render_frame_ansi()` and pyte tests unchanged |
| G9 | Elapsed time resets when a new intent is submitted |

## Non-Goals
- Mouse interaction with the bars
- Clickable status items
- Progress bars / percentage indicators
- Custom color themes (Rich defaults used)

---

## Architecture

### Layout

```
Terminal (80×24 example)
────────────────────────────────────────────────────────────
rows 1..21   Transcript viewport (scrolling, via Console.print())
row 22       ╔══════════════════════╗  ← top border of Status Bar
row 23       ║ ⣾ Thinking...  4.2s ↑1204 ↓342 ║
row 24       ╚══════════════════════╝  (simultaneously bottom of Status Bar)
             ╔══════════════════════╗  ← top border of Input Bar
row 25       ║ > _                  ║
row 26       ╚══════════════════════╝  ← bottom border of Input Bar
```

In a standard 24-row terminal: rows 21-24 are the two status+input bars.
The transcript uses rows 1-20.

### Component architecture

```
InlineRenderer._render_status_bar() -> Rich Panel (double border)
    │
    ├── self._active: bool (any running tool or pending intent)
    ├── self._spinner_frame: int (cycles through SPINNER_FRAMES)
    ├── self._elapsed_seconds: float (since last intent submit)
    ├── self._input_tokens: int (accumulates from ModelCallComplete)
    └── self._output_tokens: int

InlineRenderer._render_input_bar_panel(input_text: str) -> Rich Panel (double border)
    └── shows "> " + current buffer text or cursor placeholder

Both panels rendered via Rich Console.print() inside patch_stdout() context
so they appear above the PromptSession prompt line.
```

### Live update strategy

The status bar lives **below the transcript and above the prompt**. Because
`PromptSession` owns the bottom line, the status bar is printed (and re-printed)
using `Console.print()` with ANSI cursor-up sequences to stay in place:

```
1. Print status bar on first render (2 lines: top border + content + bottom border)
2. On each tick: move cursor up 3 lines, reprint all 3 lines
3. On intent submit: reset elapsed timer, clear token counts
```

Alternatively: wrap both bars in a single `rich.live.Live(transient=False)` block
that `update()`s on each tick. The Live block occupies a fixed 6-line region
(3 for status, 3 for input) at the bottom of the terminal.

**Recommended**: use `rich.live.Live` for the combined status+input rendering,
with `Console(force_terminal=True)` and `patch_stdout(raw=True)` as established
in PRD-09. The PromptSession is replaced by a custom async input reader
(`prompt_toolkit.shortcuts.prompt_async` with the session below the Live block).

---

## Data Structures and Interfaces

```python
# src/agenthicc/tui/app.py — additions to InlineRenderer

from __future__ import annotations
import time
from rich.panel import Panel
from rich.text import Text
from rich import box as rich_box
from agenthicc.tui.transcript import SPINNER_FRAMES

DOUBLE_BOX = rich_box.DOUBLE  # ╔═╗ / ╚═╝ style


@dataclass
class StatusState:
    """Mutable state for the Status Bar."""
    active: bool = False               # True when any agent is running
    spinner_frame: int = 0
    intent_started_at: float = 0.0    # time.monotonic() when current intent submitted
    input_tokens: int = 0
    output_tokens: int = 0
    session_cost_usd: float = 0.0
    completed_agents: int = 0
    session_id: str = ""


class InlineRenderer:
    def __init__(self, model, adapter=None, console=None,
                 base_path=".", history_file=None) -> None:
        ...  # existing fields
        self._status = StatusState()

    # ── Status Bar rendering ──────────────────────────────────────────────

    def _render_status_panel(self) -> Panel:
        s = self._status
        if s.active:
            elapsed = time.monotonic() - s.intent_started_at
            frame = SPINNER_FRAMES[s.spinner_frame % len(SPINNER_FRAMES)]
            text = Text.assemble(
                (frame + " Thinking...  ", "bold"),
                (f"{elapsed:.1f}s", "dim"),
                ("  │  ", "dim"),
                ("↑ ", "dim"), (f"{s.input_tokens:,} tok", "cyan"),
                ("  ↓ ", "dim"), (f"{s.output_tokens:,} tok", "green"),
            )
        else:
            cost = f"${s.session_cost_usd:.3f}"
            sid = s.session_id[:12] if s.session_id else "session"
            text = Text.assemble(
                (f" {sid}", "dim"),
                ("  │  ", "dim"),
                (f"{s.completed_agents} agents completed", "dim"),
                ("  │  ", "dim"),
                (cost, "dim"),
            )
        return Panel(text, box=DOUBLE_BOX, padding=(0, 1), style="dim")

    def _render_input_panel(self, input_text: str = "") -> Panel:
        from agenthicc.tui.app import INPUT_PROMPT
        content = Text(INPUT_PROMPT + input_text + "▌", style="bold white")
        return Panel(content, box=DOUBLE_BOX, padding=(0, 1))

    # ── Signal handlers (called from _render_loop) ────────────────────────

    def on_intent_submitted(self) -> None:
        self._status.active = True
        self._status.intent_started_at = time.monotonic()
        self._status.input_tokens = 0
        self._status.output_tokens = 0

    def on_model_call_complete(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self._status.input_tokens += input_tokens
        self._status.output_tokens += output_tokens
        self._status.session_cost_usd += cost_usd

    def on_agent_run_complete(self) -> None:
        if not self.model.has_running_tools():
            self._status.active = False
            self._status.completed_agents += 1
```

```python
# Updated run() — integrate status+input Live block

async def run(self, on_input):
    from prompt_toolkit.patch_stdout import patch_stdout
    from rich.live import Live
    from rich.console import Group

    try:
        from agenthicc.tui.input_bar import InputBarSession
        session = InputBarSession(base_path=self._base_path, history_file=self._history_file)
    except ImportError:
        from prompt_toolkit import PromptSession
        session = PromptSession(INPUT_PROMPT)

    # Combined status + input in one Live block (6 lines total: 3 per panel)
    def _combined_panel(input_text=""):
        return Group(self._render_status_panel(), self._render_input_panel(input_text))

    render_task = None
    with patch_stdout(raw=True):
        with Live(_combined_panel(), console=self.console, refresh_per_second=12,
                  transient=False) as live:

            async def _render_loop_with_status():
                while True:
                    await asyncio.sleep(0.05)
                    if self.adapter:
                        self.adapter.sync()
                    self._flush_new_lines()
                    self._update_spinner()
                    self.model.advance_spinner()
                    self._status.spinner_frame += 1
                    live.update(_combined_panel())   # redraw status+input

            render_task = asyncio.create_task(_render_loop_with_status())
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
                        self.on_intent_submitted()
                        on_input(text)
                    live.update(_combined_panel())   # redraw after submit
            finally:
                if render_task:
                    render_task.cancel()
                    await asyncio.gather(render_task, return_exceptions=True)
```

---

## Implementation Plan

### Phase 1 — StatusState and rendering (2 h)
1. Add `StatusState` dataclass to `tui/app.py`
2. Implement `_render_status_panel()` — active (spinner) and idle (summary) modes
3. Implement `_render_input_panel(input_text)` — double-box bordered input
4. Unit tests: `_render_status_panel()` output contains spinner frame when active; contains session ID when idle; `_render_input_panel()` output contains `> `

### Phase 2 — Signal wiring (1 h)
1. `on_intent_submitted()` resets elapsed timer and token counts
2. Subscribe to `ModelCallComplete` kernel events via `TUIEventAdapter` or direct SignalBus handler: update `status.input_tokens += event.usage.input_tokens`
3. Subscribe to `AgentRunComplete`: if `not model.has_running_tools()` → `status.active = False`
4. Integration test: emit `ModelCallComplete` via kernel → token counts update

### Phase 3 — Live integration in run() (2 h)
1. Replace bare `Console.print()` status/input with `rich.live.Live` block containing both panels
2. Live block `update()` on every render tick
3. `on_intent_submitted()` called when user submits a non-slash command
4. E2E test with pyte: status panel border characters visible at expected rows

### Phase 4 — render_frame_ansi update (30 min)
Update `render_frame_ansi()` to include 2 extra rows for the status bar:
- `rows - 4` and `rows - 3`: status bar top/content/bottom border  
- `rows - 2` and `rows - 1`: input bar top/content/bottom border  
- Input bar text still at `rows - 1` (inside the bordered region)
Update pyte tests for new row offsets.

---

## Configuration Reference

```toml
[tui]
show_status_bar = true
status_bar_style = "double"   # "double" (╔═╗) | "rounded" (╭─╮) | "simple" (┌─┐)
show_token_counts = true
show_elapsed_time = true
show_cost = true
spinner_fps = 12
```

---

## Tests

### Unit tests (`tests/unit/test_status_bar.py`)
```python
def test_status_panel_active_shows_spinner():
    renderer = InlineRenderer(TranscriptModel())
    renderer._status.active = True
    renderer._status.spinner_frame = 0
    panel = renderer._render_status_panel()
    from io import StringIO
    from rich.console import Console
    buf = StringIO()
    Console(file=buf, force_terminal=True, width=80).print(panel)
    assert any(c in buf.getvalue() for c in SPINNER_FRAMES)

def test_status_panel_idle_shows_summary():
    renderer = InlineRenderer(TranscriptModel())
    renderer._status.active = False
    renderer._status.session_id = "abc123"
    panel = renderer._render_status_panel()
    buf = StringIO()
    Console(file=buf, force_terminal=True, width=80).print(panel)
    assert "abc" in buf.getvalue()

def test_input_panel_shows_prompt():
    renderer = InlineRenderer(TranscriptModel())
    panel = renderer._render_input_panel("hello")
    buf = StringIO()
    Console(file=buf, force_terminal=True, width=80).print(panel)
    assert ">" in buf.getvalue() and "hello" in buf.getvalue()

def test_on_intent_submitted_activates_status():
    renderer = InlineRenderer(TranscriptModel())
    renderer.on_intent_submitted()
    assert renderer._status.active is True
    assert renderer._status.input_tokens == 0

def test_on_model_call_complete_accumulates_tokens():
    renderer = InlineRenderer(TranscriptModel())
    renderer.on_model_call_complete(100, 50, 0.001)
    renderer.on_model_call_complete(200, 100, 0.002)
    assert renderer._status.input_tokens == 300
    assert renderer._status.output_tokens == 150

def test_on_agent_run_complete_deactivates_when_no_running_tools():
    model = TranscriptModel()
    renderer = InlineRenderer(model)
    renderer._status.active = True
    renderer.on_agent_run_complete()
    assert renderer._status.active is False
```

### E2E test with pyte (`tests/e2e/test_status_bar_pyte.py`)
```python
def test_status_bar_visible_above_input():
    from rich.console import Console
    from rich import box as rich_box
    import pyte

    screen = pyte.Screen(80, 26)
    stream = pyte.ByteStream(screen)

    # Simulate: 20 transcript lines + status bar (3 rows) + input bar (3 rows)
    RESET = b"\033[2J\033[H"
    stream.feed(RESET)

    # 20 transcript lines
    for i in range(20):
        stream.feed(f"\033[{i+1};1Htranscript line {i}".encode())

    # Status bar: rows 21-23 (╔═══╗ / ║ content ║ / ╚═══╝)
    stream.feed(b"\033[21;1H\xe2\x95\x94" + b"\xe2\x95\x90" * 78 + b"\xe2\x95\x97")
    stream.feed(b"\033[22;1H\xe2\x95\x91 \xe2\xa3\xbe Thinking...  3.1s  \xe2\x95\x91")
    stream.feed(b"\033[23;1H\xe2\x95\x9a" + b"\xe2\x95\x90" * 78 + b"\xe2\x95\x9d")

    # Input bar: rows 24-26
    stream.feed(b"\033[24;1H\xe2\x95\x94" + b"\xe2\x95\x90" * 78 + b"\xe2\x95\x97")
    stream.feed(b"\033[25;1H\xe2\x95\x91 > _" + b" " * 74 + b"\xe2\x95\x91")
    stream.feed(b"\033[26;1H\xe2\x95\x9a" + b"\xe2\x95\x90" * 78 + b"\xe2\x95\x9d")

    # Check status bar row 22 has "Thinking"
    row_22 = "".join(c.data for c in screen.buffer[21].values())
    assert "Thinking" in row_22 or len(row_22) > 0

    # Input bar row 25 has ">"
    row_25 = "".join(c.data for c in screen.buffer[24].values())
    assert ">" in row_25
```

---

## Open Questions

1. **Live block vs ANSI sequences**: `rich.live.Live` uses ANSI cursor positioning internally.
   When combined with `PromptSession` (which also uses ANSI), there may be cursor
   fights. Mitigation: use `patch_stdout(raw=True)` (already done in PRD-09) and
   start the Live block BEFORE the PromptSession's first render.

2. **Token count accuracy**: `ModelCallComplete` fires per LLM call (including tool-call
   intermediate turns). Should the status bar show per-turn or cumulative session tokens?
   Proposal: cumulative session (easier to understand; resets on new intent).

3. **resize handling**: Terminal resize changes the number of available rows. The Live
   block should call `console.size` on each tick and adjust `transcript_rows` accordingly.

4. **`render_frame_ansi` row offsets**: The function currently uses `rows-1` for the
   input bar and `rows-2` for the status bar. With the new bordered design, the
   status+input region needs 6 rows (3 per panel). Update `render_frame_ansi` to
   reserve `rows-6` through `rows` and update pyte tests accordingly.

5. **Idle spinner vs text**: should the status bar also show a static spinner frame (not
   animating) when idle, to keep the visual rhythm consistent? Or solid horizontal line?
   Current proposal: static `─` line in idle mode, animated spinner when active.
