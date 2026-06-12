---
skill: running-the-tui
version: 1.0.0
tags: [tui, terminal, prompt_toolkit, headless]
summary: Complete guide to the Agenthicc interactive TUI — layout, key bindings, slash commands, HITL approval, and headless JSON-lines mode.
---

# Skill: Running the TUI


## Required: LLM API Key

Before running, set your LLM provider API key:

```bash
# Anthropic Claude (default — recommended)
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# OpenAI (also set provider in config)
export OPENAI_API_KEY="sk-..."

# Ollama (local, no key needed)
# Just have `ollama serve` running
```

To pin a model, add to `.agenthicc/agenthicc.toml`:

```toml
[execution]
model = "claude-sonnet-4-6"   # faster/cheaper than opus
```

Or override at launch: `agenthicc --set execution.model=claude-haiku-4-5`

## When to use this skill

Use this skill when you need to:
- Launch and navigate the interactive full-screen TUI
- Understand the layout and what each region displays
- Submit intents, read the transcript, and approve HITL prompts
- Use slash commands (`/status`, `/history`) and their menu overlays
- Run in headless JSON-lines mode for CI or scripted pipelines
- Configure key bindings or troubleshoot common TUI issues

---

## Full annotated layout

```
┌──────────────────────────────────────────────────────────────────────┐
│  1  ● agent:planner  09:41:22                                        │  ← [1] Turn header
│     > parsing intent: "refactor the auth module"                     │  ← [2] Turn lines
│     > identified 4 tasks                                             │
│       [tool] agent_spawn  ⣾ running...                               │  ← [3] Tool call (spinner)
│       [tool] task_create  ✓  38ms                                    │  ← [4] Tool call (success)
│       [tool] task_create  ✓  41ms                                    │
│       [tool] task_create  ✓  39ms                                    │
│     → tokens: 892  cost: $0.002                                      │  ← [5] Turn footer
│  ────────────────────────────────────────────────────────────        │  ← [6] Separator
│     ● agent:worker-1  09:41:24                                       │
│     > writing tests for AuthService                                  │
│       [tool] file_write  ✓  102ms                                    │
│       [tool] application_log  ✓  2ms                                 │
│     → tokens: 1,240  cost: $0.003                                    │
│  ────────────────────────────────────────────────────────────        │
│     ● agent:worker-2  09:41:25                                       │
│     > refactoring AuthService._validate                              │
│       [tool] file_read   ✓  8ms                                      │
│       [tool] file_write  ⣻ running...                                │  ← spinner animates
│                                                                      │
│  [STATUS MENU FLOATS HERE WHEN /status IS TYPED — see §Menus]        │  ← [7] Menu overlay
│                                                                      │
├──────────────────────────────────────────────────────────────────────┤
│  3 agents | $0.005 | 2,132 tok                          [row rows-1] │  ← [8] Status line
├──────────────────────────────────────────────────────────────────────┤
│ > _                                                     [row rows]   │  ← [9] Input bar (pinned)
└──────────────────────────────────────────────────────────────────────┘
```

### Callout legend

| # | Region | Source | Notes |
|---|---|---|---|
| 1 | Turn header | `AgentTurnEntry.header()` | `● agent:<name>  HH:MM:SS` |
| 2 | Turn lines | `AgentTurnEntry.lines` | Prefixed `  > ` in `render()` |
| 3 | Tool call (running) | `ToolCallEntry.render()` | Braille spinner `SPINNER_FRAMES` |
| 4 | Tool call (done) | `ToolCallEntry.render()` | `✓` + duration in ms |
| 5 | Turn footer | `AgentTurnEntry.footer()` | `→ tokens: N  cost: $X.XXX` |
| 6 | Separator | `SEPARATOR = "─" * 60` | Inserted between turns |
| 7 | Menu overlay | `render_menu()` | `Float(bottom=2)` — never touches input bar |
| 8 | Status line | `render_status()` | Agent count, cumulative cost, total tokens |
| 9 | Input bar | `input_window` | Pinned to last row; prefix `INPUT_PROMPT = "> "` |

---

## Submitting intents

Type your intent in the input bar and press **Enter**:

```
> refactor the auth module to use JWT
```

The text is passed to `on_input(text)` which emits `IntentCreated` to the kernel.
The transcript updates as agents begin working.

Empty input is ignored. To submit a blank line deliberately, use `/noop` (not a
built-in command — it will be passed to `on_input` as a no-op string).

---

## Reading the transcript

The transcript region auto-scrolls to the tail (most recent activity) and is
clipped to `rows - 2` visible lines. Each agent's output is grouped into turns
separated by a `─` line.

Tool call states:

| Symbol | Meaning |
|---|---|
| `.` | `PENDING` — registered but not started |
| `⣾⣽⣻⢿⡿⣟⣯⣷` | `RUNNING` — animated Braille spinner |
| `✓` | `SUCCESS` — completed with duration |
| `✗` | `FAILURE` — completed with error message |

The spinner advances every ~100ms via `TranscriptModel.advance_spinner()` called
from a background timer task.

---

## Slash commands

Type a slash command and press **Enter** to open its menu overlay. Press **Escape**
to dismiss.

### `/status` — Agent Status overlay

```
┌─────────────────────────────────────────────────┐
│  /status — Agent Status                         │
│  ├─ planner (agent:a1b2c3d4)                    │
│  ├─ worker-1 (agent:e5f6a7b8)                   │
│  ├─ worker-2 (agent:c9d0e1f2)                   │
└─────────────────────────────────────────────────┘
 3 agents | $0.005 | 2,132 tok                      ← status line stays here
> _                                                 ← input bar stays here
```

The overlay floats at `Float(bottom=2, right=0)` — two rows above the terminal
bottom — so it can never obscure the status line or input bar.

### `/history` — Event Log overlay

```
┌─────────────────────────────────────────────────┐
│  /history — Event Log                           │
│  ● agent:planner  09:41:22                      │
│    > parsing intent: "refactor the auth module" │
│    > identified 4 tasks                         │
│      [tool] agent_spawn  ⣾ running...           │
│      [tool] task_create  ✓  38ms                │
│      [tool] task_create  ✓  41ms                │
│    → tokens: 892  cost: $0.002                  │
│  ────────────────────────────────               │
│  ● agent:worker-1  09:41:24                     │
└─────────────────────────────────────────────────┘
 3 agents | $0.005 | 2,132 tok
> _
```

Shows the last 10 rendered transcript lines. Useful for reviewing recent activity
without scrolling.

### Dismissing menus

- Press **Escape** to dismiss any open menu overlay
- Type any non-slash input and press **Enter** (the menu is cleared on submit)
- Type a different slash command to switch menus

---

## HITL approval walkthrough

Human-in-the-loop (HITL) approval is triggered when a tool's `PermissionRule` has
`action = "require_confirmation"`. The TUI displays the pending call in the
transcript as a `PENDING` tool call entry and an approval prompt appears in the
input bar region:

```
  [tool] file_write  .  [AWAITING APPROVAL]
─────────────────────────────────────────────
 Approve tool call 'file_write' on '/workspace/src/auth.py'? [y/N]
> _
```

**Step-by-step:**

1. A `PENDING` tool entry appears in the current agent turn.
2. The status line shows `WAITING FOR APPROVAL`.
3. Type `y` and press **Enter** to approve; type `n` or press **Enter** on empty
   to reject.
4. On approval: the tool call transitions to `RUNNING`, then `SUCCESS` or `FAILURE`.
5. On rejection: the tool call transitions to `FAILURE` with error `"rejected by operator"`.

The kernel emits `ToolApprovalResponse` with `approved: true/false`; the executor
proceeds or raises `Rejection`.

---

## Headless mode

Run without a terminal using `run_headless`. Each kernel event is emitted as one
JSON line to `output_stream`.

```python
import asyncio
import sys
from agenthicc.tui.app import run_headless

async def main():
    # event_queue is fed by your kernel bridge
    await run_headless(event_queue, output_stream=sys.stdout)

asyncio.run(main())
```

Example output lines:

```json
{"ts": 1735689600.123, "event_type": "IntentCreated", "event_id": "a1b2c3", "payload": {"intent_id": "i1", "raw_text": "refactor auth"}, "source_agent_id": null}
{"ts": 1735689601.456, "event_type": "AgentSpawnRequest", "event_id": "d4e5f6", "payload": {"agent_id": "w1", "agent_type": "worker"}, "source_agent_id": null}
{"ts": 1735689602.789, "event_type": "TaskCreated", "event_id": "g7h8i9", "payload": {"task_id": "t1", "description": "write tests"}, "source_agent_id": "w1"}
```

Stop headless mode by putting `None` onto the queue:

```python
await event_queue.put(None)
```

### Parsing headless output

```python
import json
import subprocess

proc = subprocess.Popen(
    ["python", "-m", "agenthicc", "--headless"],
    stdout=subprocess.PIPE, text=True
)
for line in proc.stdout:
    record = json.loads(line)
    print(record["event_type"], record["ts"])
```

---

## Key bindings

| Key | Action |
|---|---|
| **Enter** | Submit input text; open menu if slash command |
| **Escape** | Dismiss open menu overlay |
| **Ctrl-C** | Exit the TUI application |
| **Ctrl-D** | (handled by prompt_toolkit buffer) — clear input |
| **Up / Down** | Scroll input history (prompt_toolkit default) |
| **Ctrl-W** | Delete word before cursor (prompt_toolkit default) |
| **Ctrl-U** | Clear input to start of line (prompt_toolkit default) |

Key bindings are defined in `build_app` via `KeyBindings`:

```python
kb = KeyBindings()

@kb.add("escape")
def _dismiss(event):
    ui_state["menu_visible"] = False
    ui_state["active_menu"] = None

@kb.add("c-c")
def _exit(event):
    event.app.exit()
```

To add custom bindings, pass a modified `KeyBindings` object to `build_app`.

---

## Common issues

### TUI does not start / ImportError

```
RuntimeError: prompt_toolkit is not installed; use run_headless() instead
```

Install the TUI extra:

```bash
pip install "agenthicc[tui]"
# or
uv sync --extra tui
```

### Input bar disappears after resize

The ANSI renderer always pins the input bar to `row rows`. If you observe it
moving, check that your terminal resize handler calls `app.renderer.reset()` or
re-renders the frame.

### Spinner is not animating

The spinner requires a background task calling `model.advance_spinner()` every
~100ms. Ensure the spinner task is running:

```python
async def spin():
    while True:
        model.advance_spinner()
        app.invalidate()
        await asyncio.sleep(0.1)

asyncio.create_task(spin())
```

### Menu overlay covers transcript content

This is by design — the overlay floats at `Float(bottom=2, right=0)` and is
anchored above the status line. It is always dismissable with **Escape**.

### Cost shows $0.000

Cost is populated from `AgentTurnEntry.cost_usd`. Ensure your agent runner bridge
sets this field when the turn is closed:

```python
turn = model.append_turn(agent_id, agent_name)
# ... after run completes ...
turn.cost_usd = completion.usage.cost_usd
turn.tokens = completion.usage.total_tokens
```

### render_frame_ansi for testing

Use `render_frame_ansi` in tests to get a deterministic ANSI frame without a real
terminal:

```python
from agenthicc.tui.app import render_frame_ansi
from agenthicc.tui.transcript import TranscriptModel

model = TranscriptModel()
model.append_turn("agent-1", "planner")
model.append_line("agent-1", "hello world")

frame = render_frame_ansi(model, cols=80, rows=24)
# frame contains ANSI escape sequences; strip them for text assertions
import re
text = re.sub(r'\x1b\[[^m]*m|\x1b\[\d+;\d+H|\x1b\[2J|\x1b\[H', '', frame)
assert "hello world" in text
```

---

## Key points

- `INPUT_PROMPT = "> "` is the literal prefix shown in the input bar every row.
- `MENU_COMMANDS = {"/status": "status", "/history": "history"}` — only these two
  slash commands open overlays; any other `/text` is passed to `on_input` unchanged.
- `detect_slash_command(text)` returns the menu name or `None`.
- The menu overlay is a `Float(bottom=2, right=0)` — it never displaces the status
  line or input bar because those are fixed-height rows in the `HSplit`.
- `run_headless` works with zero terminal dependencies; use it in CI pipelines.
- `render_frame_ansi` is the test hook for frame-accurate assertions without pyte.
- `build_app` raises `RuntimeError` if `prompt_toolkit` is not installed; check
  `PROMPT_TOOLKIT_AVAILABLE` before calling.
