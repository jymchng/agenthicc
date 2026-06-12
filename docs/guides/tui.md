# TUI Guide

The Agenthicc TUI is a full-screen terminal application built on
[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/). It renders a
live transcript of agent activity, a persistent status line, and a pinned input
bar that never moves — no matter how much output scrolls through the transcript.

---

## Installation

```bash
pip install "agenthicc[tui]"
```

Verify prompt_toolkit is available:

```python
from agenthicc.tui.app import PROMPT_TOOLKIT_AVAILABLE
print(PROMPT_TOOLKIT_AVAILABLE)  # True
```

---

## Full layout — numbered callouts

```
Row 1
│
│  ┌─────────────────────────────────────────────────────────────────────┐
│  │ [1]  ● agent:planner  09:41:22                                      │
│  │      > parsing intent: "refactor the auth module to use JWT"        │ [2]
│  │      > identified 4 tasks, spawning workers                         │
│  │        [tool] agent_spawn  ⣾ running...                             │ [3]
│  │        [tool] task_create  ✓  38ms                                  │ [4]
│  │        [tool] task_create  ✓  41ms                                  │
│  │        [tool] task_create  ✓  39ms                                  │
│  │      → tokens: 892  cost: $0.002                                    │ [5]
│  │  ────────────────────────────────────────────────────────────       │ [6]
│  │      ● agent:worker-1  09:41:24                                     │
│  │      > writing tests for AuthService                                │
│  │        [tool] file_write  ✓  102ms                                  │
│  │        [tool] application_log  ✓  2ms                               │
│  │      → tokens: 1,240  cost: $0.003                                  │
│  │  ────────────────────────────────────────────────────────────       │
│  │      ● agent:worker-2  09:41:25                                     │
│  │      > refactoring AuthService._validate                            │
│  │        [tool] file_read   ✓  8ms                                    │
│  │        [tool] file_write  ⣻ running...                              │
│  │                                                                     │
│  │  [7] MENU OVERLAY FLOATS HERE — anchored 2 rows above bottom        │
│  │                                                                     │
Row rows-1
│  ├─────────────────────────────────────────────────────────────────────┤
│  │ [8]  3 agents | $0.005 | 2,132 tok                                  │  ← status line
Row rows
│  ├─────────────────────────────────────────────────────────────────────┤
│  │ [9]  > _                                                            │  ← input bar (pinned)
│  └─────────────────────────────────────────────────────────────────────┘
```

| # | Region | Source in code | Description |
|---|---|---|---|
| 1 | Turn header | `AgentTurnEntry.header()` | `● agent:<name>  HH:MM:SS` |
| 2 | Turn lines | `AgentTurnEntry.lines` | Each prefixed `  > ` |
| 3 | Spinner tool call | `ToolCallEntry.render()` | Braille spinner when `RUNNING` |
| 4 | Done tool call | `ToolCallEntry.render()` | `✓` + duration in ms |
| 5 | Turn footer | `AgentTurnEntry.footer()` | `→ tokens: N  cost: $X.XXX` |
| 6 | Separator | `SEPARATOR = "─" * 60` | Between turns in `render()` |
| 7 | Menu overlay | `render_menu()` | `Float(bottom=2)` — above status line |
| 8 | Status line | `render_status()` | Agent count, cumulative cost, tokens |
| 9 | Input bar | `input_window` | Pinned to last row; prefix `INPUT_PROMPT = "> "` |

### Layout implementation

The layout is an `HSplit` of three windows inside a `FloatContainer`:

```
FloatContainer(
    content = HSplit([
        transcript_window,   # grows to fill remaining height
        status_window,       # height=1, row rows-1
        input_window,        # height=1, row rows (always last)
    ]),
    floats=[
        Float(content=menu_overlay, bottom=2, right=0),
    ],
)
```

The `Float(bottom=2)` anchor means the overlay's bottom edge is 2 rows above the
terminal bottom — sitting exactly above the status line. It can never touch the
input bar.

---

## Submitting intents

Type your intent text and press **Enter**:

```
> refactor the auth module to use JWT
```

This calls `on_input(text)` which you wire to emit `IntentCreated` events. Empty
inputs are ignored. The input buffer is reset after every submit.

---

## Slash commands

Type a slash command and press **Enter** to open its overlay. Press **Escape** to
close.

### `/status`

```
┌──────────────────────────────────────────────┐
│  /status — Agent Status                      │
│  ├─ planner (agent:a1b2c3d4)                 │
│  ├─ worker-1 (agent:e5f6a7b8)                │
│  ├─ worker-2 (agent:c9d0e1f2)                │
└──────────────────────────────────────────────┘
 3 agents | $0.005 | 2,132 tok                   ← status line unchanged
> _                                              ← input bar unchanged
```

Shows every distinct `agent_id` seen in the transcript with its `agent_name`.
The overlay is re-rendered on every application invalidation tick.

### `/history`

```
┌──────────────────────────────────────────────┐
│  /history — Event Log                        │
│  ● agent:planner  09:41:22                   │
│    > parsing intent                          │
│      [tool] agent_spawn  ✓  12ms             │
│      [tool] task_create  ✓  38ms             │
│    → tokens: 892  cost: $0.002               │
│  ────────────────────────────────            │
│  ● agent:worker-1  09:41:24                  │
└──────────────────────────────────────────────┘
 3 agents | $0.005 | 2,132 tok
> _
```

Shows the last 10 rendered transcript lines. Useful for reviewing recent activity.

---

## Key bindings

| Key | Action | Implementation |
|---|---|---|
| **Enter** | Submit input; open menu if slash command | `accept_handler` on `Buffer` |
| **Escape** | Dismiss menu overlay | `@kb.add("escape")` |
| **Ctrl-C** | Exit the application | `@kb.add("c-c")` |
| **Up** | Previous input history | prompt_toolkit default |
| **Down** | Next input history | prompt_toolkit default |
| **Ctrl-W** | Delete word before cursor | prompt_toolkit default |
| **Ctrl-U** | Clear input line | prompt_toolkit default |
| **Ctrl-D** | End of input (close buffer) | prompt_toolkit default |
| **Left/Right** | Move cursor in input | prompt_toolkit default |
| **Home/End** | Jump to start/end of input | prompt_toolkit default |

---

## HITL approval walkthrough

Human-in-the-loop approval is triggered when a tool's `PermissionRule` has
`action = "require_confirmation"`. Here is the full interaction:

**Step 1** — A pending tool call appears:

```
● agent:worker-1  09:41:30
  > preparing to overwrite auth.py
    [tool] file_write  .  [AWAITING APPROVAL]
─────────────────────────────────────────────────────────────
 1 agent | $0.001 | 320 tok
 Approve file_write on /workspace/src/auth.py? [y/N]
> _
```

**Step 2** — Type `y` and press **Enter** to approve:

```
> y
```

**Step 3** — Tool executes and completes:

```
● agent:worker-1  09:41:31
  > preparing to overwrite auth.py
    [tool] file_write  ✓  88ms
```

**Step 4** — Type `n` or press **Enter** on empty input to reject:

```
    [tool] file_write  ✗  rejected by operator
```

The kernel emits `ToolApprovalResponse` with `approved: true/false`. The executor
then either proceeds or raises `Rejection("rejected by operator")`.

---

## Headless mode

Run without any terminal — one JSON line per kernel event:

```python
import asyncio
import sys
from agenthicc.tui.app import run_headless

async def main():
    event_queue = asyncio.Queue()

    # Feed your kernel events onto event_queue ...

    await run_headless(event_queue, output_stream=sys.stdout)

asyncio.run(main())
```

Stop by putting `None` onto the queue:

```python
await event_queue.put(None)
```

### JSON line format

Each line is a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `ts` | float | Unix timestamp (`time.time()`) |
| `event_type` | str | `Event.event_type` |
| `event_id` | str | `Event.event_id` |
| `payload` | dict | `Event.payload` |
| `source_agent_id` | str or null | `Event.source_agent_id` |

### Example headless output

```json
{"ts": 1735689600.123, "event_type": "IntentCreated", "event_id": "abc", "payload": {"intent_id": "i1", "raw_text": "refactor auth"}, "source_agent_id": null}
{"ts": 1735689601.456, "event_type": "AgentSpawnRequest", "event_id": "def", "payload": {"agent_id": "w1", "agent_type": "worker"}, "source_agent_id": null}
{"ts": 1735689602.789, "event_type": "ApplicationLog", "event_id": "ghi", "payload": {"level": "INFO", "message": "starting refactor"}, "source_agent_id": "w1"}
```

### Parsing headless output from a subprocess

```python
import json
import subprocess

proc = subprocess.Popen(
    ["python", "-m", "agenthicc", "--headless"],
    stdout=subprocess.PIPE, text=True,
)
for line in proc.stdout:
    record = json.loads(line)
    if record["event_type"] == "ApplicationLog":
        print(f"[{record['payload']['level']}] {record['payload']['message']}")
```

---

## Common workflows

### How to refactor a codebase in 5 steps

1. Start the TUI: `python -m agenthicc`
2. Submit the intent: `> refactor the auth module to use JWT`
3. Watch the planner turn — it will create tasks and spawn workers.
4. Monitor worker turns — each `file_write` shows duration; failures show `✗ <error>`.
5. When all turns show footers (`→ tokens: N  cost: $X.XXX`), the intent is complete.

If a `require_confirmation` tool fires, type `y` to approve or `n` to reject.

### How to debug a failing test

1. Submit: `> run the failing test tests/unit/test_auth.py::test_validate and fix it`
2. Open `/status` to see which worker picked it up.
3. Watch the worker turn for `file_read` (reading the test file) then `file_write` (fix).
4. If the tool shows `✗`, the error message is shown inline — no need to switch windows.
5. Submit a follow-up intent to re-run the test once the fix is in.

### How to monitor a long-running workflow

1. Open `/history` to see the last 10 rendered lines.
2. The status line shows cumulative cost — useful for budget monitoring.
3. Press **Escape** to close the overlay and resume reading new output as it arrives.

---

## Transcript anatomy

Each `AgentTurnEntry` renders as:

```
● agent:<name>  HH:MM:SS            ← header()
  > <line 1>                         ← lines (prefixed "  > ")
  > <line 2>
    [tool] <name>  <symbol> <info>   ← tool calls (indented 4 spaces)
  → tokens: N  cost: $X.XXX         ← footer() (only if tokens/cost set)
```

Tool call symbols:

| Symbol | State | Extra info |
|---|---|---|
| `.` | `PENDING` | Registered, not started |
| `⣾⣽⣻⢿⡿⣟⣯⣷` | `RUNNING` | Animated Braille spinner |
| `✓` | `SUCCESS` | Duration in ms |
| `✗` | `FAILURE` | Error message |

The spinner cycles through `SPINNER_FRAMES` (8 Braille characters) and advances
every ~100ms via a background timer task calling `model.advance_spinner()`.

---

## Config hot-reload

`agenthicc.toml` changes are not automatically hot-reloaded by the TUI. To reload:

1. Stop the TUI with **Ctrl-C**.
2. Edit `agenthicc.toml`.
3. Restart: `python -m agenthicc`.

For daemon use cases, send `SIGHUP` to trigger a config reload (if your launcher
supports it).

---

## render_frame_ansi for testing

`render_frame_ansi` produces a deterministic ANSI frame without a real terminal,
used in pyte-based e2e tests:

```python
from agenthicc.tui.app import render_frame_ansi, INPUT_PROMPT
from agenthicc.tui.transcript import TranscriptModel

model = TranscriptModel()
model.append_turn("agent-1", "planner")
model.append_line("agent-1", "hello from planner")

frame = render_frame_ansi(model, cols=80, rows=24)

# The last row always contains the input prompt
import re
lines = re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]', '\n', frame).split('\n')
last_row = [l for l in lines if INPUT_PROMPT in l]
assert last_row, "Input bar missing from last row"
```

Row layout (1-indexed):
- Rows `1..rows-2`: transcript lines (tail-clipped to `rows-2` lines)
- Row `rows-1`: status line
- Row `rows`: input bar with `INPUT_PROMPT = "> "` prefix (always last)

Menu overlay (when `menu_lines` is provided): painted over transcript rows,
anchored so its last row is at `rows-2` (just above the status line).
