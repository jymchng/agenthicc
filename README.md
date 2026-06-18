<div align="center">

```
╔══════════════════════════════════════════════════════╗
║           a g e n t h i c c                         ║
║   state-driven agent operating system               ║
╚══════════════════════════════════════════════════════╝
```

</div>

<p align="center">
  <em>Event-sourced kernel · parallel DAG execution · tool-only agent communication · 3-tier memory · full-screen TUI</em>
</p>

<p align="center">
<a href="https://github.com/agenthicc/agenthicc/actions/workflows/tests.yml">
  <img src="https://github.com/agenthicc/agenthicc/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests">
</a>
<a href="https://github.com/agenthicc/agenthicc/actions/workflows/lint.yml">
  <img src="https://github.com/agenthicc/agenthicc/actions/workflows/lint.yml/badge.svg?branch=main" alt="Lint">
</a>
<a href="https://codecov.io/gh/agenthicc/agenthicc">
  <img src="https://img.shields.io/codecov/c/github/agenthicc/agenthicc?color=%2334D058&label=coverage" alt="Coverage">
</a>
<a href="https://pypi.org/project/agenthicc">
  <img src="https://img.shields.io/pypi/v/agenthicc?color=%2334D058&label=pypi%20package" alt="PyPI">
</a>
<a href="https://pypi.org/project/agenthicc">
  <img src="https://img.shields.io/pypi/pyversions/agenthicc.svg?color=%2334D058" alt="Python versions">
</a>
<a href="https://github.com/agenthicc/agenthicc/blob/main/LICENSE">
  <img src="https://img.shields.io/github/license/agenthicc/agenthicc.svg?color=%2334D058" alt="License">
</a>
<a href="https://github.com/astral-sh/ruff">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff">
</a>
</p>

---

**Documentation**: <a href="https://docs.agenthicc.dev" target="_blank">https://docs.agenthicc.dev</a>

**Source Code**: <a href="https://github.com/agenthicc/agenthicc" target="_blank">https://github.com/agenthicc/agenthicc</a>

---

## For AI Agents & Coding Assistants

| Resource | What it contains |
|---|---|
| [`llms.txt`](./llms.txt) | 2 KB package overview — start here |
| [`llms-full.txt`](./llms-full.txt) | Complete API reference — all public symbols, signatures, common errors |
| [`AGENTS.md`](./AGENTS.md) | Agent rules: file ownership, by-task lookup, common errors, definition of done |
| [`CLAUDE.md`](./CLAUDE.md) | Architecture decisions, pitfall table, commands, conventions |
| [`skills/`](./skills/) | Copy-paste skill guides: adding events, tools, hooks, TUI extensions |

---

## TUI Demo

```
● assistant (laguna-m.1)  15:02:51
  ⎿ list_directory('tests')  ✓  2ms
  ⎿ list_directory  ✓  15ms
  ⎿ list_directory  ✓  7ms
  ⎿ list_directory  ✓  18ms
  ⎿ list_directory('rust_password_generator')  ✓  20ms
  ⎿ ...and 3 more tool calls

❁ ↻ Thinking │ 1m 40s │ ↑ 591,173 ↓ 3,736 │ Phase 1/4: plan
openai/poolside/laguna-m.1
29e1874d-3ea6-4ba5-9c2e-247376ae4a2e │  4 turns │  $0.602
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ ▌
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  PLAN Plan  (shift+tab to cycle)  │  ctrl+j = ↵
ESC Cancel  │  (LLM responding to tool error)
  Workflow: code_plan  │  Phase 1/4: plan
```

### Plan Mode

```
Now I have a complete understanding of the repository. Let me create a detailed enhancement plan:                                      

Repository Analysis Summary                                                                                                            

Current State:                                                                                                                         

 • A Python password generator library with a solid core implementation in password_generator/generator.py                             
 • Empty tests directory - no test coverage                                                                                            
 • Empty README.md files - no documentation                                                                                            
 • Minimal pyproject.toml configuration                                                                                                
 • Missing __init__.py for proper package imports                                                                                      
 • No CLI interface or __main__.py for direct module execution                                                                         
 • Empty rust_password_generator/ placeholder directory                                                                                

---------------------------------------------------------------------------------------------------------------------------------------

❊ ↻ Thinking │ 4m 24s │ ↑ 290,266 ↓ 4,022 │ Phase 1/4: plan
openai/poolside/laguna-m.1
473badbb-358b-4ba8-b1ae-5fb53bcd6f3e │  1 turn │  $0.302
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  📋 Plan Review
──────────────────────────────────────────────────────────────────
  ## Repository Enhancement Plan
  
  Based on my analysis, the repository is a Python password generator project with a well-implemented core module but missing several
  
  ### 1. Package Structure Improvements
  - **Add `password_generator/__init__.py`** - Export the `PasswordGenerator` class for clean imports
  - **Add `password_generator/__main__.py`** - Enable running via `python -m password_generator`
  
  ### 2. Documentation
  - **Update main `README.md`** - Comprehensive documentation including:
  lines 1–10 of 52 · ↓
──────────────────────────────────────────────────────────────────
  ▶ Approve
    Reject — add feedback
    Approve — add instructions
──────────────────────────────────────────────────────────────────
  ↑↓ options  [ up  ] down  Enter select  Esc deny
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ◈ Plan  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  1/4  plan
```

### Plan Mode to Execution Mode

```
Let me provide a more extensive enhancement plan:                                                                                      
  ⎿ request_plan_approval  ✗  0ms
  ⎿ request_plan_approval('# Repository Enhancement Plan for Python Password Generator)  ✓  178390ms
The plan has been approved. Let me finalize it and begin execution:                                                                    
  ⎿ finalize_plan  ✗  0ms
  ⎿ finalize_plan('# Repository Enhancement Plan for Python Password Generator)  ✓  0ms
The enhancement plan for the Python Password Generator repository has been fully approved and finalized. The plan includes:            

 1 Comprehensive test suite with tests for character sets, password generation, edge cases, and performance                            
 2 CLI interface with argparse supporting various options and interactive mode                                                         
 3 Package structure improvements with proper __init__.py and __main__.py modules                                                      
 4 Password strength estimator with entropy calculation and blacklist checking                                                         
 5 Full documentation in README.md                                                                                                     
 6 Enhanced pyproject.toml with metadata, entry points, and dev dependencies                                                           
 7 GitHub Actions CI/CD workflow for testing and coverage                                                                              

The execution phase will now begin automatically.                                                                                      
● assistant (laguna-m.1)  16:42:08
Now I'm in the execution phase. Let me implement the enhancements to the Python Password Generator repository.                         

Phase 1: Create package structure files                                                                                                
  ⎿ write_file  ✗  0ms
  Modified: password_generator/__init__.py
  ⎿ write_file(content='"""Password Generator - , path='password_generator/__ini)  ✓  8ms

✾ ↻ Thinking │ 41s │ ↑ 188,230 ↓ 6,539 │ Phase 2/4: execute
openai/poolside/laguna-m.1
597e3972-d9fa-48e8-9182-62b75fc7f34b │  3 turns │  $0.208
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ ▌
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ Auto  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  2/4  execute
```

### Agent Asking User **ONE** Question

```
❀ Thinking │ 2m 45s │ ↑ 698,939 ↓ 5,954
openai/deepseek-v4-flash
c82cd4cc-f125-4e3a-9fa0-3c71fa597f80 │  1 turn │  $0.717
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ❓ Questions  (0 of 1 answered)
──────────────────────────────────────────────────────────────────
    Question 1 of 1                                            ○

  What kind of enhancement do you want?

  ▶ New CLI feature (e.g. --version, --json output, --clipboard)
    Performance optimization (e.g. speed up generation)
    Bug fix / polish (e.g. fix broken README badges, edge cases)

──────────────────────────────────────────────────────────────────
  ↑↓ option   ←→ question   Enter confirm   Esc cancel
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ◈ Plan  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  1/4  plan
```

### Agent Asking User Multiple Questions:

```
❀ Thinking │ 3m 29s │ ↑ 747,313 ↓ 6,197
openai/deepseek-v4-flash
c82cd4cc-f125-4e3a-9fa0-3c71fa597f80 │  1 turn │  $0.766
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ❓ Questions  (0 of 3 answered)
──────────────────────────────────────────────────────────────────
    Question 1 of 3 ▶                                      ○ ○ ○

  What area of the project should the enhancement focus on?

  ▶ CLI / user-facing commands
    Password generation itself (new algorithms, templates)
    Vault / storage / encryption

──────────────────────────────────────────────────────────────────
  ↑↓ option   ←→ question   Enter confirm   Esc cancel
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ◈ Plan  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  1/4  plan

✾ Thinking │ 4m 17s │ ↑ 747,313 ↓ 6,197
openai/deepseek-v4-flash
c82cd4cc-f125-4e3a-9fa0-3c71fa597f80 │  1 turn │  $0.766
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ❓ Questions  (1 of 3 answered)
──────────────────────────────────────────────────────────────────
  ◀ Question 2 of 3 ▶                                      ● ○ ○

  How much code change are you willing to accept?

    30-50 lines (moderate feature)
  ▶ I'll trust your judgment
    Other — type your answer

──────────────────────────────────────────────────────────────────
  ↑↓ option   ←→ question   Enter confirm   Esc cancel
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ◈ Plan  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  1/4  plan

✽ Thinking │ 4m 31s │ ↑ 747,313 ↓ 6,197
openai/deepseek-v4-flash
c82cd4cc-f125-4e3a-9fa0-3c71fa597f80 │  1 turn │  $0.766
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ❓ Questions  (2 of 3 answered)
──────────────────────────────────────────────────────────────────
  ◀ Question 3 of 3                                        ● ● ○

  Who is the primary beneficiary of this enhancement?

  ▶ End users (better UX, new features)
    Developers (better DX, tooling, CI)
    Both - something visible and practical

──────────────────────────────────────────────────────────────────
  ↑↓ option   ←→ question   Enter confirm   Esc cancel
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ◈ Plan  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ◈ code_plan  1/4  plan
```

### Agent Implementing the Changes

```
Now I have everything. Let me implement all changes systematically.                                                                    

Change 1: Add import json to cli.py                                                                                                    
  Modified: password_generator/cli.py
  ⎿ patch_file(path='password_generator/cli.p, old_content='import argparse\nimport , new_content='import argparse\nimport )  ✓  3ms
Change 2: Add global --json flag to root parser                                                                                        
  Modified: password_generator/cli.py
  ⎿ patch_file(old_content='    parser = argparse.Ar, new_content='    parser = argparse.Ar, path='password_generator/cli.p)  ✓  3ms
Change 3: Modify cmd_generate for JSON output                                                                                          
  Modified: password_generator/cli.py
  ⎿ patch_file(old_content='    for pw in passwords:, new_content='    if args.json:\n     , path='password_generator/cli.p)  ✓  3ms

❀ Thinking │ 1m 3s │ ↑ 991,944 ↓ 6,130
openai/deepseek-v4-flash
c82cd4cc-f125-4e3a-9fa0-3c71fa597f80 │  3 turns │  $0.957
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ ▌
───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ Auto  (shift+tab to cycle)
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
  ⏵⏵ code_plan  2/4  execute
```
---

## Features

| Feature | Description |
|---|---|
| **Event-sourced kernel** | MPSC queue → pure reducer → immutable `AppState`. Every state transition is an appended event; full replay from `events.jsonl`. |
| **Parallel DAG executor** | Intents compile to dependency DAGs; ready nodes run concurrently up to `max_parallel_tasks`. |
| **Tool-only agent comms** | Agents never call Python directly — all inter-agent signalling goes through typed communication tools (`agent_spawn`, `agent_send_message`, `task_create`, `workflow_modify`). Full observability and replay for free. |
| **Lifecycle hooks** | `LifecycleHook.on_before/on_after/on_error` at intent, workflow node, task, agent, and tool-call granularity. Loaded from TOML dotpaths at startup. |
| **3-tier memory** | Session (in-process LRU+TTL), project (SQLite namespaced KV + artifact table), global (user-wide SQLite). Reads never block; writes serialised per tier. |
| **Full-screen TUI** | `prompt_toolkit` HSplit layout: scrolling transcript viewport, status line, input bar always pinned to the last row. Braille spinners for live tool calls. |
| **Headless API** | FastAPI server with intent submission, status polling, state summary, and WebSocket stream. Optional Bearer auth. |
| **lauren-ai integration** | Agent runners are lauren-ai `AgentRunnerBase` subclasses; `LaurenToolHookAdapter` bridges agenthicc lifecycle hooks to lauren-ai `ToolHook`. |

---

## Installation

```bash
# Recommended: uv
uv add agenthicc

# TUI support
uv add "agenthicc[tui]"

# Headless API server
uv add "agenthicc[api]"

# Everything (dev + tui + api)
uv add "agenthicc[tui,api,dev]"

# pip
pip install agenthicc
pip install "agenthicc[tui]"
pip install "agenthicc[api]"
```

| Extra | What you get |
|---|---|
| *(none)* | Core kernel, runtime, memory, workflow engine |
| `[tui]` | Full-screen TUI (`prompt_toolkit>=3.0`) |
| `[api]` | Headless REST+WebSocket server (`fastapi`, `uvicorn`, `websockets`) |
| `[dev]` | Test tooling (`pytest`, `pytest-asyncio`, `hypothesis`, `pyte`, `httpx`) |

---


## Environment Variables — Running with an LLM

Agenthicc uses **[lauren-ai](https://github.com/lauren-framework/lauren-ai)** for
all LLM calls. You must set at least one provider API key before agents can run.

### Anthropic Claude (default)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

That's the only required variable. The default model is `claude-opus-4-6`.

### OpenAI

```bash
export OPENAI_API_KEY="sk-..."
# then set provider in config:
# [execution]
# provider = "openai"
# model = "gpt-4o"
```

### Ollama (local, no key)

```bash
# No API key needed — just have Ollama running
# [execution]
# provider = "ollama"
# model = "llama3.2"
```

### Override model at launch

```bash
agenthicc --set execution.model=claude-haiku-4-5
```

## Quick Start

**1. Install**

```bash
uv add "agenthicc[tui]"
```

**2. Create `agenthicc.toml`** in your project root

```toml
[execution]
max_parallel_tasks = 4
agent_pool_size    = 8

[memory]
project_memory_path = ".agenthicc/memory"

[security]
sandbox_mode  = true
allowed_paths = ["/workspace"]

[tools]
allowed = ["read_file", "write_file", "run_tests"]

[api]
host = "127.0.0.1"
port = 8000
```

**3. Run the TUI**

```bash
agenthicc
```

**4. Type a natural language intent**

```
> Refactor auth/hashing.py to replace bcrypt with Argon2id and update the tests
```

**5. Watch it work**

The planner agent decomposes your intent into a DAG, spawns specialist agents for
each node, and streams their progress live to the transcript viewport.  Tool calls
show spinner → checkmark with latency.  The status line tracks active agents and
accumulated cost.

---

## TUI Guide

The TUI is the primary interface for interactive use.  It is a full-screen
`prompt_toolkit` application that renders the kernel's event stream as a live
transcript.

### Layout overview

```
  ┌─────────────────────────────────────────────────────┐
  │  1  TRANSCRIPT VIEWPORT                             │
  │     Scrolling log of agent turns.                   │
  │     Each turn: header + text lines + tool calls.    │
  │     Tool calls show spinner while running.          │
  │     ─────────────────────────────────────────────── │
  │  2  STATUS LINE                                     │
  │     <n> agents | $<cost> | <tokens> tok             │
  │     ─────────────────────────────────────────────── │
  │  3  INPUT BAR  (ALWAYS last row)                    │
  │     > _                                             │
  └─────────────────────────────────────────────────────┘
```

Callout **1 — Transcript viewport**: occupies `rows - 2` rows.  Auto-scrolls to
the tail.  Each agent turn opens with a bullet header:

```
● agent:planner  12:34:01
  > Analysing repository structure...
    [tool] read_file src/auth.py          ✓  12ms
    [tool] search_code "bcrypt"           ⣾ running…
```

The `●` bullet is followed by `agent:<name>` and a wall-clock timestamp.
Model output lines are indented with `  > `.  Tool call lines are indented with
`    [tool] <name>` and show one of:

- `⣾` / `⣽` / `⣻` / `⢿` / `⡿` / `⣟` / `⣯` / `⣷` — braille spinner (animates while running)
- `✓  <Nms>` — success with latency
- `✗  <error>` — failure with message

Callout **2 — Status line**: one row, style class `statusline`.  Format:
`<n> agents | $<cost> | <tokens> tok`.  Updated after every kernel event.

Callout **3 — Input bar**: the very last row, style class `input-bar`, prefixed
`> `.  This row is ALWAYS at `rows` (1-indexed) and is NEVER displaced by menus
or overlays.

### Input bar

Type any natural-language intent and press Enter.  The text is submitted to the
kernel as an `IntentCreated` event.  The planner picks it up, synthesises a
workflow DAG, and spawns agents.

Slash commands (see table below) are intercepted before submission and open a
floating menu overlay anchored 2 rows above the terminal bottom — the overlay
**never** touches the input bar.

Press **Escape** to dismiss the active overlay without submitting.

### Transcript viewport

The transcript is rendered by `TranscriptModel.render()` and displayed in a
`prompt_toolkit` `Window` with `wrap_lines=True`.  The most recent `rows - 2`
lines are visible; earlier output scrolls off the top.

Each `AgentTurnEntry` contributes:

1. A header line (`● agent:<name>  HH:MM:SS`)
2. Zero or more model output lines (`  > <text>`)
3. Zero or more tool call lines (`    [tool] <name>  <symbol>  <detail>`)
4. An optional footer with token count and cost

Agent turns are separated by a `────────────────────────────────────────────` rule.

### Status line

The status line sits between the transcript and the input bar.  It shows:

```
 3 agents | $0.042 | 14,302 tok
```

Values update after every state snapshot pushed by the kernel subscriber.

### Slash commands

| Command | Description |
|---|---|
| `/status` | Show all running agents and their current tasks as a tree.  Agents in the `busy` state show their `current_task_id`. |
| `/approve` | Human-in-the-loop tool approval.  Lists all tool calls currently in `require_confirmation` state.  Type `y` to approve or `n` to reject each one. |
| `/history` | Searchable event log.  Shows the last 10 events by default; type to filter by event type or agent ID.  Press Enter to inspect a full event payload. |
| `/settings` | Live TOML editor.  Opens the merged configuration in a floating editor window.  Save with Ctrl+S; changes take effect immediately without restarting. |

Dismiss any overlay with **Escape**.

### Key bindings

| Key | Action |
|---|---|
| `Enter` | Submit the current input bar text as an intent (or open a slash-command overlay) |
| `Ctrl+C` | Exit agenthicc |
| `↑` | Recall previous input from history |
| `↓` | Recall next input from history |
| `Shift+Enter` | Insert a newline in the input bar (multi-line intents) |
| `Escape` | Dismiss the active overlay menu |

### Headless mode

Run without a terminal:

```bash
agenthicc --headless
```

In headless mode the TUI is replaced by `run_headless()`, which emits one
JSON line to stdout per kernel event.  Suitable for CI, piping to `jq`, or
integration with external dashboards.

Example JSON-line output:

```json
{"ts": 1719875041.234, "event_type": "ToolCallComplete", "event_id": "a1b2c3d4", "payload": {"tool_use_id": "u123", "tool_name": "read_file", "success": true, "duration_ms": 12}, "source_agent_id": "agent-refactor-001"}
```

Key fields in every line:

| Field | Type | Description |
|---|---|---|
| `ts` | float | Unix timestamp |
| `event_type` | string | Kernel event class name |
| `event_id` | string | UUID hex for deduplication |
| `payload` | object | Event-specific data |
| `source_agent_id` | string\|null | Emitting agent, if any |

Pipe to `jq` for filtering:

```bash
agenthicc --headless | jq 'select(.event_type == "ToolCallComplete")'
```

---

## Configuration

Minimal `agenthicc.toml`:

```toml
[execution]
max_concurrent_intents = 8   # max intents running in parallel
max_parallel_tasks     = 4   # max DAG nodes running simultaneously
agent_pool_size        = 16  # max agents in the pool

[memory]
project_memory_path = ".agenthicc/memory"  # SQLite KV + artifact DB path

[security]
sandbox_mode  = true              # restrict tool file/network access
allowed_paths = ["/workspace"]    # paths tools may read/write

[api]
host        = "127.0.0.1"
port        = 8000
api_key_env = "AGENTHICC_API_KEY" # env var holding the Bearer token
```

Full reference: `agenthicc config --help` or [docs/configuration.md](./docs/configuration.md).

---

## Architecture

```
  ┌──────────────────────────────────────────────────────────┐
  │  Intent (natural language text)                          │
  └───────────────────────┬──────────────────────────────────┘
                          │ IntentCreated event
  ┌───────────────────────▼──────────────────────────────────┐
  │  Kernel  (kernel/)                                        │
  │  EventProcessor — MPSC queue → root_reducer → AppState   │
  │  Events persist to events.jsonl; snapshots every N events │
  └───────────────────────┬──────────────────────────────────┘
          Effects         │ AppState subscribers
  ┌────────────┬──────────┼──────────────┐
  │            │          │              │
  ▼            ▼          ▼              ▼
workflow/   runtime/   memory/        tui/ or api/
DAG exec   AgentPool  3-tier KV    TUI / JSON-lines
```

---

## Contributing

See [docs/contributing.md](./docs/contributing.md).  In brief:

```bash
uv sync --all-extras
uv run pytest tests/ -q
uv run ruff check src/ tests/
```

All PRs must pass lint, type-check, and the full test suite.
