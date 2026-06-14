# AgentHICC TUI — Information Architecture

**Document type**: Design specification  
**Scope**: Full information architecture for the AgentHICC terminal user interface  
**Status**: Draft v1.0  
**Date**: 2026-06-13

---

## 1. Executive Summary

AgentHICC is an AI coding agent operating system that runs long-horizon autonomous engineering tasks in a terminal. The TUI must simultaneously satisfy two distinct user postures: *supervisory* (the user delegates a task, then watches it unfold) and *collaborative* (the user and agent exchange messages and approvals in a tight feedback loop). Most existing agent UIs collapse these into a single chat metaphor, which breaks down under tool-heavy, multi-turn, long-running workloads.

This document specifies an information architecture that treats the **transcript** as the primary surface — a permanent, scrollable log of everything the agent did — while layering ephemeral overlays on top for navigation, configuration, and approval gates. A single persistent status line carries ambient state (mode, model, cost, token count) so the user is never left wondering what the system is doing or how much it is costing.

Key design commitments:

- **One canonical surface, not many tabs.** The transcript is the home screen, the history view, and the debug log all at once. Secondary views appear as overlays that dismiss with Escape, never replacing the transcript.
- **Input bar is always reachable.** It lives on the last terminal row and is never displaced — not by tool output, not by menus, not by approval prompts.
- **State is visible, not implied.** Mode, active agent, streaming status, and cost are rendered on every frame. The user should never have to query the system to know what it is doing.
- **Approval gates interrupt the input bar, not a separate dialog.** HITL confirmations appear inline in the status/input zone so the user can respond without navigating away.
- **Sessions are first-class.** The session index, resume flow, and conversation store are part of the core IA, not an afterthought.

---

## 2. Information Architecture Overview Diagram (ASCII)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        AGENTHICC TUI — LAYER MAP                             │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  LAYER 3: MODAL OVERLAYS (Float — dismiss with Escape)               │   │
│  │  ┌───────────────┐  ┌───────────────┐  ┌────────────────────────┐   │   │
│  │  │  Slash-Cmd    │  │  Config Menu  │  │  Session Picker        │   │   │
│  │  │  Dropdown     │  │  (/config)    │  │  (--continue / list)   │   │   │
│  │  └───────────────┘  └───────────────┘  └────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  LAYER 2: APPROVAL GATE (replaces input bar — highest urgency)       │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │  [AWAITING APPROVAL]  Approve file_write on auth.py? [y/N]   │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  LAYER 1: PERSISTENT CHROME (always visible)                         │   │
│  │                                                                      │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │  TRANSCRIPT VIEWPORT                                         │   │   │
│  │  │  (scrollable, tail-follows by default)                       │   │   │
│  │  │                                                              │   │   │
│  │  │  ● agent:planner  09:41:22                                   │   │   │
│  │  │    > parsing intent: "refactor auth module"                  │   │   │
│  │  │    > identified 4 tasks, spawning workers                    │   │   │
│  │  │      [tool] agent_spawn        ⣾ running…                    │   │   │
│  │  │      [tool] task_create        ✓  38ms                       │   │   │
│  │  │      [tool] read_file          ✓  12ms                       │   │   │
│  │  │      [tool] write_file         ✓ 102ms                       │   │   │
│  │  │        --- a/src/auth.py                                     │   │   │
│  │  │        +++ b/src/auth.py                                     │   │   │
│  │  │        @@ -14,6 +14,8 @@                                     │   │   │
│  │  │        +  import jwt                                         │   │   │
│  │  │        +  TOKEN_ALG = "HS256"                                │   │   │
│  │  │    → tokens: 892  cost: $0.002                               │   │   │
│  │  │  ──────────────────────────────────────────────────────      │   │   │
│  │  │  ● agent:worker-1  09:41:24                                  │   │   │
│  │  │    > writing tests for AuthService                           │   │   │
│  │  │      [tool] file_write  ⣻ running…                           │   │   │
│  │  │                                                              │   │   │
│  │  ├──────────────────────────────────────────────────────────────┤   │   │
│  │  │  STATUS LINE  [AUTO] claude-opus-4-8  2 agents  $0.005  2k tok│   │   │
│  │  ├──────────────────────────────────────────────────────────────┤   │   │
│  │  │  INPUT BAR  > _                                              │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘

INFORMATION HIERARCHY
─────────────────────
  Primary concern    → Transcript (what the agent is doing / has done)
  Ambient concern    → Status line (mode, model, cost, token budget)
  Interaction point  → Input bar (always present, always on last row)
  Ephemeral concern  → Overlays (commands, config, approval, sessions)

DATA FLOW
─────────
  User input ──► CommandDispatcher ──► SlashCommandHandler ──► Overlay / Agent
  Kernel events ──► TUIEventAdapter ──► TranscriptModel ──► Viewport render
  LLM stream ──► SignalBus ──► TranscriptModel ──► RenderLoop ──► Terminal
  Tool calls ──► ToolExecutor ──► TranscriptModel (tool entries) ──► Diff viewer
```

---

## 3. Primary Views Specification

### 3.1 Main Transcript View

The transcript view is the single primary surface. It is never navigated away from — overlays float on top of it.

**Anatomy of a rendered frame:**

```
Row 1..N-2   TRANSCRIPT VIEWPORT
             Tail of TranscriptModel.render() — last (rows-2) lines shown.
             Auto-scrolls as new content arrives.

Row N-1      STATUS LINE (height=1)
             [MODE] provider/model  N agents  $X.XXX  N tok  [session-id-prefix]

Row N        INPUT BAR (height=1, pinned)
             > <cursor>
             OR (approval gate):
             Approve <tool> on <path>? [y/N] <cursor>
```

**Transcript line types and visual grammar:**

| Line type | Format | Example |
|---|---|---|
| Turn header | `● <agent-name>  HH:MM:SS` | `● agent:planner  09:41:22` |
| Prose line | `  > <text>` | `  > parsing intent` |
| Tool pending | `    [tool] <name>  .` | `    [tool] write_file  .` |
| Tool running | `    [tool] <name>  ⣾ running…` | (animated braille) |
| Tool success | `    [tool] <name>  ✓  Nms` | `    [tool] write_file  ✓  102ms` |
| Tool failure | `    [tool] <name>  ✗  <error>` | `    [tool] write_file  ✗  permission denied` |
| Diff block | Inline unified diff lines | `--- a/src/auth.py / +++ b/src/auth.py / @@ … / +import jwt` |
| Mention chip | `  [@file: src/auth.py  1.2 KB]` | (injected file context) |
| Turn footer | `  → tokens: N  cost: $X.XXX` | `  → tokens: 892  cost: $0.002` |
| Separator | `──────────────────────────────────` | Between turns |
| Debug footer | \`\`\`[DEBUG] elapsed=…\`\`\` | Debug mode only |

**Viewport behaviour:**

- Default: tail-following (scrolls as new content arrives).
- On resume: last 20 Q&A pairs from ConversationStore are replayed at the top before the live session begins.
- Overflow: oldest lines scroll off the top; TranscriptModel retains full history in memory; `/history` shows last 10 rendered lines.
- Wide output (e.g., long diffs): lines wrap at terminal width. No horizontal scroll — wrapping is always preferred over truncation to preserve diff readability.

### 3.2 Tool Execution Inline View

Tool calls are rendered directly in the transcript as `ToolCallEntry` objects. There is no separate "tool panel" — tool output is always in context.

**Expanded diff state** (triggered by `/expand <tool-id>` or `@path`):

```
    [tool] write_file  ✓  102ms    [EXPANDED]
      --- a/src/auth.py
      +++ b/src/auth.py
      @@ -14,6 +14,8 @@
         class AuthService:
      +      TOKEN_ALG = "HS256"
      +      TOKEN_SECRET = os.environ["JWT_SECRET"]
           def __init__(self):
```

**Collapsed state** (default for successful writes under ~10 lines of diff):

```
    [tool] write_file  ✓  102ms  [2 hunks, +8/-3 src/auth.py]
```

**Rationale**: Diffs are load-bearing information for a coding agent — the user needs to see what changed. However, a 400-line diff during an autonomous refactor would consume the entire viewport. The default is to show full diffs for small changes (< 20 lines) and a summary badge for large ones, with `/expand` to see full output.

### 3.3 Session Management View

The session management surface has two entry points:

1. **CLI**: `agenthicc sessions` — prints a table to stdout (outside TUI).
2. **Resume picker** (future overlay): activated by `--continue` or within a running TUI via `/sessions`.

**CLI session table format:**

```
  abc123456789  2026-06-13 09:41  /home/user/myproject  *
  def987654321  2026-06-12 18:22  /home/user/otherproject
  ghi112233445  2026-06-11 14:05  /home/user/myproject
```

`*` marks the most recent session for the current working directory.

**In-TUI session picker overlay (future):**

```
┌───────────────────────────────────────────────┐
│  Sessions  (current dir marked *)             │
│  ──────────────────────────────────           │
│  > abc123  Jun-13 09:41  myproject     *      │
│    def987  Jun-12 18:22  otherproject         │
│    ghi112  Jun-11 14:05  myproject            │
│                                               │
│  Enter: resume  Escape: cancel                │
└───────────────────────────────────────────────┘
```

### 3.4 Configuration View

Accessed via `/config` slash command. Renders as a floating overlay (Float anchored above the status line).

```
┌──────────────────────────────────────────────────┐
│  Configuration                                   │
│  ────────────────────────────────────            │
│  provider      anthropic                         │
│  model         claude-opus-4-8                   │
│  max_turns     200                               │
│  mode          Auto                              │
│  memory_path   .agenthicc/memory                 │
│  mcp_servers   3 configured                      │
│  ────────────────────────────────────            │
│  config file   .agenthicc/agenthicc.toml         │
│  user config   ~/.agenthicc/agenthicc.toml       │
│                                                  │
│  /model [provider] [model] to change model       │
│  Escape to close                                 │
└──────────────────────────────────────────────────┘
```

The config view is read-only. Mutations are achieved via slash commands (`/model`, `/mode`) or by editing the TOML file and restarting. This is intentional: in-TUI config editing creates race conditions between the TOML file and the live session state.

---

## 4. Navigation Model

### 4.1 Navigation Primitives

AgentHICC uses a **modal overlay** navigation model, not a tabbed or paned model. There is one primary surface (the transcript) and a stack of zero or more overlays above it.

| Primitive | Description |
|---|---|
| Transcript | Always present; never navigated away from |
| Overlay push | Opening a slash command, menu, or approval gate |
| Overlay pop | Escape key dismisses the topmost overlay |
| Input bar | Always available unless an approval gate is active |
| Scroll | Up/Down/PgUp/PgDn scrolls the transcript (future: not currently required while input bar is focused) |

### 4.2 Overlay Stack Rules

- Only one non-approval overlay is open at a time. Opening a second slash command replaces the first.
- Approval gates are always topmost; no other overlay can open while one is pending.
- Pressing Escape on an empty input bar with no overlay is a no-op (does not exit the application).
- Ctrl-C exits the application unconditionally.

### 4.3 Slash Command Dropdown

When the user types `/` in the input bar, a completion dropdown appears floating above the input:

```
┌───────────────────────────────────────────────┐
│  /cancel    Cancel the current intent         │
│  /clear     Clear transcript display          │
│  /config    Open configuration editor         │
│  /expand    Expand tool output or @mention    │
│  /help      List available commands           │
│  /history   Browse the event log              │
│  /mcp       Show MCP server status            │
│  /model     Show or switch LLM model          │
│  /mode      Show or switch operational mode   │
│  /skills    List available skills             │
│  /status    Show running agents               │
└───────────────────────────────────────────────┘
> /mo_
```

Filtering: as the user types after `/`, the list narrows to prefix matches. Arrow Up/Down navigates; Tab or Enter selects. The dropdown is anchored `Float(bottom=2)` — always above the status line, never overlapping the input bar.

### 4.4 Breadcrumb / Context Awareness

There are no breadcrumbs in the classical sense. Context awareness is delivered by the status line, which always shows:

- Active mode badge (`[AUTO]`, `[PLAN]`, `[ASK]`, `[REVIEW]`, `[SAFE]`, `[DEBUG]`)
- Provider/model shortname
- Active agent count
- Cumulative cost
- Cumulative token count
- Session ID prefix (last 6 chars)

This gives the user enough ambient context to know where they are without needing dedicated navigation state.

### 4.5 Mode Cycling

Modes cycle in a fixed order (Auto → Plan → Ask → Review → Safe → Debug → Auto) via `Shift+Tab`. The mode badge on the status line updates immediately. The user can also jump to a named mode with `/mode <name>`.

---

## 5. Session Model

### 5.1 Session Lifecycle

```
┌─────────────┐   agenthicc (fresh)    ┌──────────────────┐
│  No session  ├───────────────────────►  New session      │
└─────────────┘                        │  - new UUID       │
                                       │  - register in    │
                                       │    sessions.json  │
                                       │  - open JSONL log │
                                       └────────┬──────────┘
                                                │
                                     (user works, events stream)
                                                │
                                       ┌────────▼──────────┐
                                       │  Active session   │
                                       │  - events.jsonl   │
                                       │  - memory snapshot│
                                       │  - conv_store     │
                                       └────────┬──────────┘
                                                │
                     Ctrl-C / exit              │
                ┌───────────────────────────────┘
                │
       ┌────────▼──────────┐
       │  Persisted session │
       │  .agenthicc/       │
       │  sessions/         │
       │  <id>.jsonl        │
       └────────┬──────────┘
                │
    agenthicc --continue (or --resume <id>)
                │
       ┌────────▼──────────┐
       │  Resumed session  │
       │  - restore from   │
       │    JSONL log      │
       │  - replay last 20 │
       │    Q&A pairs      │
       │  - restore memory │
       │    snapshot       │
       └───────────────────┘
```

### 5.2 Session State Components

| Component | Storage location | What it contains |
|---|---|---|
| Event log | `.agenthicc/sessions/<id>.jsonl` | All kernel events (full replay) |
| Session index | `.agenthicc/sessions.json` | `{id: {cwd, created_at, last_used, log_path}}` |
| Conversation store | `.agenthicc/` (SQLite) | Q&A pairs, model_short, timestamps |
| Memory snapshot | `.agenthicc/` (SQLite) | ShortTermMemory serialised for LLM context |
| App state snapshot | `.agenthicc/snapshot.json` | Optional periodic AppState snapshot for fast restore |

### 5.3 Resume Behaviour

On resume, the TUI:

1. Restores `AppState` from the event log via `restore_from_log()`.
2. Replays the last 20 Q&A pairs from `ConversationStore` into the terminal (Rich Markdown rendering, not into `TranscriptModel`).
3. Restores `ShortTermMemory` so the LLM agent has full conversation context.
4. Prints a visual separator: `── resumed session <id-prefix> ──`.
5. Opens the live input bar for new turns.

### 5.4 Multi-Session Considerations

Multiple sessions can exist (one per project directory, or multiple per directory if the user runs concurrent shells). They are isolated by `session_id` and do not share state. There is no live multi-session view in the TUI — each TUI instance manages exactly one session. Simultaneous TUI sessions in the same directory may both write to `sessions.json`; this is last-write-wins (acceptable for the single-user use case; `.json` is small and writes are atomic via `Path.write_text`).

---

## 6. Agent Execution Model

### 6.1 Agent Turn Structure

Each LLM invocation produces one `AgentTurnEntry` in the `TranscriptModel`. An intent (`IntentCreated` event) triggers one or more agent turns. A multi-turn invocation (tool calls followed by further LLM calls) produces multiple chunks within the same `AgentTurnEntry`.

```
AgentTurnEntry
  ├── agent_id         "agent-{intent_id[:8]}"
  ├── agent_name       "assistant (claude-opus-4-8)"
  ├── started_at       monotonic timestamp
  ├── lines[]          list[str] — prose text deltas, one per stop_reason
  ├── tool_calls[]     list[ToolCallEntry]
  ├── mention_chips[]  list[MentionChip]
  ├── input_tokens     int (accumulated across all LLM turns)
  ├── output_tokens    int
  └── cost_usd         float
```

### 6.2 Streaming States

An agent turn passes through these states, which drive the visual rendering:

```
  ┌──────────────┐
  │   PENDING    │  IntentCreated emitted; turn header rendered; no lines yet
  └──────┬───────┘
         │ LLM starts streaming
  ┌──────▼───────┐
  │  STREAMING   │  delta chunks arrive; partial_text accumulates;
  │              │  RenderLoop.tick() repaints every ~100ms
  └──────┬───────┘
         │ stop_reason received (end of one LLM turn)
  ┌──────▼───────┐
  │  TOOL-EXEC   │  ToolCallStarted/ToolCallComplete signals fire;
  │              │  transcript shows spinner → ✓/✗;
  │              │  LLM receives tool results and streams again
  └──────┬───────┘
         │ All turns exhausted (max_turns or final stop)
  ┌──────▼───────┐
  │   COMPLETE   │  footer rendered; status.active = False;
  │              │  input bar restored to idle
  └──────────────┘
```

**Streaming visual during active turn:**

The status bar shows a live spinner and partial token count. The partial_text is accumulated and periodically committed to the transcript at each `stop_reason`. This means the user sees prose text appear in chunks rather than one character at a time — a deliberate choice to reduce rendering thrash during tool-heavy turns.

### 6.3 Tool Execution Pipeline

```
User input → on_intent() → _run_agent_turn()
  │
  ├── @mention injection (build_context_prefix)
  │     adds file/dir/glob/url content as system context
  │
  ├── skill auto-trigger (find_matching_skills)
  │     appends skill body to system prompt
  │
  ├── tool registry build (build_registry)
  │     fs, git, exec, outlook, plugin, MCP tools
  │
  ├── agent runner (AgentRunnerBase.run_stream)
  │     ├── ModelCallComplete → update token counts
  │     ├── ToolCallStarted → snapshot file, add tool entry, tick render
  │     └── ToolCallComplete → generate diff, finish tool entry, tick render
  │
  └── IntentStatusChanged event emitted
```

### 6.4 Approval Gates

Approval gates pause the tool execution pipeline. They appear when a tool's `PermissionRule` carries `action = "require_confirmation"`. The gate is rendered as an inline prompt in the input zone:

```
 1 agent | $0.001 | 320 tok
 Approve file_write on /workspace/src/auth.py? [y/N]
> _
```

The agent is suspended (awaiting `ToolApprovalResponse` kernel event). The rest of the transcript continues to render above. The user types `y` (approve) or `n`/Enter (reject). After response, the tool either proceeds or receives a `Rejection` error, and the agent continues.

---

## 7. Tool Execution Model

### 7.1 Tool Display States

| State | Visual marker | Duration shown | Notes |
|---|---|---|---|
| PENDING | `.` (dot) | — | Registered, not yet started |
| RUNNING | `⣾⣽⣻⢿⡿⣟⣯⣷` | — | Animated braille; cycles every ~100ms |
| SUCCESS | `✓` | Nms | Green checkmark + elapsed time |
| FAILURE | `✗` | — | Red cross + error message |

### 7.2 Running → Complete Transitions

The `RenderLoop` is responsible for keeping the running spinner alive. It calls `TranscriptModel.advance_spinner()` every ~100ms via a background timer. When `ToolCallComplete` fires, `finish_tool_call()` is called and the entry transitions to SUCCESS or FAILURE. The next `RenderLoop.tick()` paints the final state.

Transition latency is bounded by the render loop period (nominally 100ms). In practice, tool completions appear within one frame — imperceptible to users.

### 7.3 Diff Display Policy

For file-editing tools (`write_file`, `patch_file`, `append_file`):

1. Before the tool call: snapshot the original file content (`_file_snapshots[tool_use_id]`).
2. After the tool call: read the new content, compute unified diff via `difflib.unified_diff`.
3. Attach the diff to the `ToolCallEntry` as `output`.

**Display thresholds:**

| Diff size | Default display |
|---|---|
| 0 lines (no change) | Not shown |
| 1–20 lines | Shown inline, fully expanded |
| 21–100 lines | Shown inline, collapsed to summary badge; `/expand <tool-id>` to see full |
| > 100 lines | Shown as badge only; `/expand <tool-id>` streams full diff in overlay |

The summary badge format: `[N hunks, +A/-R <path>]`

### 7.4 Error States

Tool errors are rendered as:

```
    [tool] run_bash  ✗  exit code 1: make: *** [all] Error 2
```

The first line of the error is shown inline. For multi-line errors (e.g., a Python traceback), the first line is shown and `/expand` provides the full output.

If the tool error propagates to an agent failure:

```
  > ⚠ Error: Tool 'run_bash' failed: exit code 1
```

This appears as a prose line in the turn, not as a tool entry.

### 7.5 Output Truncation

Long tool outputs (e.g., `run_bash` producing thousands of lines of test output) are truncated in the transcript:

- First 50 lines shown inline.
- A badge: `[…output truncated — /expand to view all N lines]`
- `/expand <tool-id>` streams the full output in a scrollable overlay.

This threshold is configurable (`execution.tool_output_lines_limit`, default 50).

### 7.6 MCP Tool Display

MCP tools are indistinguishable from built-in tools in the transcript. They appear with the same `[tool] <name>  ✓/✗  Nms` format. The tool name includes the server prefix when there is a name collision: `[tool] mcp:github.list_pulls  ✓  234ms`.

---

## 8. Approval / Confirmation Model

### 8.1 When Approvals Fire

An approval gate fires when:

1. The `SecurityPolicy` has a `PermissionRule` with `action = "require_confirmation"` matching the tool and resource.
2. The current mode is not `Auto` or `Debug` (configurable — `Safe` mode always confirms writes).
3. The tool is in the set of irreversible operations: file writes, shell commands, git commits, email sends.

### 8.2 Approval Gate Rendering

The approval gate displaces the standard `> _` input bar:

```
BEFORE (idle input bar):
 2 agents | $0.003 | 1,240 tok
> _

DURING approval gate:
 1 agent | $0.001 | 320 tok
 Approve write_file on src/auth.py? [y/N]
> _
```

The gate prompt is rendered on the status-line row. The input bar remains on the last row. The user types into the standard input buffer; the `on_intent` handler detects the pending approval and routes the response appropriately.

### 8.3 Response Routing

| User input | Effect |
|---|---|
| `y` or `yes` | `ToolApprovalResponse(approved=True)` emitted |
| `n`, `no`, or empty Enter | `ToolApprovalResponse(approved=False)` emitted |
| Any other text | Treated as `n` (reject) with a warning line in the transcript |

Rationale: during an approval gate, the user is in a binary decision context. Allowing arbitrary text would be confusing. If the user wants to send a different message to the agent, they should first reject the tool call and then type their follow-up.

### 8.4 Batched Approvals

When the agent queues multiple simultaneous tool calls that all require confirmation (e.g., `parallel_tool_calls=True` with three writes), each is gated in sequence. The transcript shows all pending tools with the `[AWAITING APPROVAL]` marker; the approval prompt cycles through them one at a time.

```
    [tool] write_file  .  [AWAITING APPROVAL]  src/auth.py
    [tool] write_file  .  [QUEUED]             src/auth_test.py
    [tool] write_file  .  [QUEUED]             src/auth_models.py

 Approve write_file on src/auth.py? [y/N]
> _
```

### 8.5 Approval Timeout

If the approval gate receives no response within `execution.approval_timeout_secs` (default: 300 seconds), the tool is automatically rejected and an `ApplicationLog` warning is emitted. This prevents the agent from hanging indefinitely in unattended scenarios.

---

## 9. Notification Model

### 9.1 Priority Levels

| Level | Visual treatment | Persistence | Examples |
|---|---|---|---|
| INFO | Dim line in transcript | Transient (scrolls off) | Plugin loaded, MCP connected |
| WARNING | Yellow `⚠` prefix in transcript | Transient | Config key unknown, tool conflict |
| ERROR | Red `✗` prefix; transcript + status pulse | Persistent in transcript | Tool failure, API error |
| APPROVAL | Replaces status-line row | Blocks until responded | HITL tool confirmation |
| CRITICAL | Full-width banner above status line | Until dismissed (Escape) | Connection lost, fatal error |

### 9.2 Transient Notifications

Transient notifications (INFO, WARNING) are appended as prose lines to the current or a synthetic agent turn. They scroll with the transcript. Examples:

```
  [dim]Loaded 3 plugin tool(s) from .agenthicc/tools/[/dim]
  [dim]MCP: 12 tool(s) from 2 server(s)[/dim]
  ⚠ Config key 'execution.unknown_key' not recognized — ignored
```

These are rendered via `Rich.Console.print()` before the live session loop starts (startup notifications) or as `application_log` kernel events during the session.

### 9.3 Persistent Notifications

ERROR-level notifications persist in the transcript and are marked with `✗` so they remain visible when the user scrolls up. They are never cleared by `/clear` (which only clears the viewport, not the model).

### 9.4 Status Line Notifications

The status line carries one persistent ambient notification: the active approval gate. While a gate is pending, the status line row changes from the standard metric strip to the approval prompt. No other notification type modifies the status line layout.

### 9.5 Notification Placement

```
STARTUP (before live loop):
  Rich Console output → terminal stdout (scrollback)
  Appears above the TUI frame; visible if user scrolls up in terminal emulator

DURING SESSION:
  ApplicationLog events → TranscriptModel synthetic turn → Transcript viewport
  Errors → inline in the turn that caused them

APPROVAL GATE:
  Status-line row → full-width prompt
```

Rationale for not using a notification sidebar or toast layer: in a terminal environment, toasts that appear and disappear are easily missed. Placing notifications in the permanent transcript ensures the user can always scroll back to find them. The approval gate is the only notification that demands immediate attention, so it is the only one that interrupts the main interaction surface.

---

## 10. Context & State Visibility

### 10.1 Status Line Anatomy

The status line is always rendered at `rows - 1` (the second-to-last row). Its content:

```
 [MODE]  provider/model  N agents  $X.XXX  N tok  [session-id]
```

| Field | Source | Update frequency |
|---|---|---|
| Mode badge | `ModeManager.active_name` | On Shift+Tab or `/mode` |
| Provider/model | `cfg.execution.provider / effective_model()` | On `/model` |
| Agent count | `len(transcript.active_agents())` | Each render tick |
| Cost | `StatusModel.session_cost_usd` | On `ModelCallComplete` signal |
| Token count | `input_tokens + output_tokens` | On `ModelCallComplete` signal |
| Session ID | `StatusModel.resume_id[:6]` | Static per session |

**Mode badge colours:**

| Mode | Badge | Colour |
|---|---|---|
| Auto | `[AUTO]` | Green |
| Plan | `[PLAN]` | Yellow |
| Ask | `[ASK]` | Cyan |
| Review | `[REVIEW]` | Blue |
| Safe | `[SAFE]` | Red |
| Debug | `[DEBUG]` | Magenta |

### 10.2 @Mention Visibility

When the user includes `@file`, `@dir/`, `@glob/*.py`, or `@https://url` in a message, the injection pipeline resolves them and adds `MentionChip` entries to the current agent turn:

```
  [@file: src/auth.py  1.2 KB]      — file resolved successfully
  [@glob: src/**/*.test.ts → 7 files]
  [@url: https://docs.example.com  4,213 chars]
  [@file: src/missing.py  not found]  — unresolved (red/dim)
```

These chips appear at the top of the agent turn, before the first prose line. They give the user immediate confirmation of what context the agent received.

The injected content itself is visible via `/expand @src/auth.py` if the user wants to verify exactly what was sent.

### 10.3 Active Agent Display

During a multi-turn run, the transcript shows multiple `AgentTurnEntry` blocks with their `agent_name` and `started_at` timestamp. The status line shows the count of active agents. `/status` shows a detailed agent roster:

```
┌─────────────────────────────────────────────────────┐
│  /status — Agent Status                             │
│  ─────────────────────────────────────────          │
│  ● agent:planner    (agent-a1b2c3d4)  active        │
│  ● agent:worker-1   (agent-e5f6a7b8)  running tool  │
│  ● agent:worker-2   (agent-c9d0e1f2)  idle          │
│                                                     │
│  Total: 3 agents  |  $0.005  |  2,132 tok           │
└─────────────────────────────────────────────────────┘
```

### 10.4 Cost and Token Tracking

Token counts and costs are accumulated per-session in `StatusModel`:

- `input_tokens` / `output_tokens`: incremented on every `ModelCallComplete` signal.
- `session_cost_usd`: incremented on every `ModelCallComplete` with the `cost_usd` field.
- Displayed on the status line on every render frame.
- Not persisted across sessions (each session starts from zero).

For resumed sessions, the cost shown is the cost for the current resumed session, not the total across all sessions. A future enhancement could accumulate cross-session totals in the `ConversationStore`.

### 10.5 Streaming Progress Visibility

During active streaming, the status line transitions from the idle state to an active spinner:

```
IDLE:
 [AUTO] claude-opus-4-8  0 agents  $0.000  0 tok

STREAMING:
 [AUTO] claude-opus-4-8  ⣾ streaming  in:1,240 out:892  $0.002
```

The `partial_text` accumulation is also visible in the transcript as the text appears. The `RenderLoop.tick()` is called at each `ToolCallStarted`, `ToolCallComplete`, and on each stream chunk stop, ensuring the display stays responsive.

---

## 11. Error & Recovery Model

### 11.1 Error Taxonomy

| Error category | Examples | Recovery path |
|---|---|---|
| LLM config error | Missing API key, invalid model | Shown before TUI starts; set env var and restart |
| Connection failure (transient) | Network timeout, rate limit | Retry with backoff; shown in transcript as `⚠ Error: …` |
| Connection failure (fatal) | API key revoked, endpoint unreachable | Critical banner; agent turn marked failed; user can retry intent |
| Tool error (recoverable) | Non-zero exit code, file not found | Tool entry shows `✗ <error>`; agent may self-correct |
| Tool error (fatal) | Permission denied on sandboxed path | Tool entry shows `✗ permission_denied`; agent is notified |
| Agent error | Unhandled exception in `_run_agent_turn` | `⚠ Error: <exc>` in transcript; input bar restored |
| Approval rejection | User types `n` | Tool entry shows `✗ rejected by operator`; agent continues |
| Approval timeout | No response in 300s | Tool auto-rejected; `ApplicationLog WARNING` in transcript |
| MCP init failure | Server unreachable at startup | Logged via `logging.getLogger`; non-fatal; MCP tools unavailable |
| Plugin conflict | Two plugins export the same tool name | `warn_conflicts()` prints warning at startup; later plugin wins |

### 11.2 Error Display Hierarchy

```
RECOVERABLE (tool/agent level):
  → Inline in transcript turn
  → `✗ <error>` on the tool entry
  → Agent may self-correct on the next LLM turn

CRITICAL (session level):
  → Full-width banner line in transcript:
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ⚠  CRITICAL: Connection to anthropic failed — check API key
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  → Status line shows `[ERROR]` badge
  → Input bar remains active for retry

FATAL (process level):
  → `TUI error: <exc>` printed to stderr after the TUI exits
  → Exit code 1
```

### 11.3 Recovery Paths

**Transient API error**: The user types a follow-up message. The `_run_agent_turn` function is re-entered fresh. The `ShortTermMemory` snapshot preserves conversation history so the agent retains context.

**Tool failure mid-workflow**: The agent receives the error as a tool result and decides whether to retry, self-correct, or report to the user. No user action required unless the agent asks.

**Agent crash / exception**: The `_run_agent_turn` `except` clause captures all exceptions, writes `⚠ Error: <exc>` to the transcript, sets `status.active = False`, and restores the input bar. The session remains alive. The user can retry the intent.

**Process kill / Ctrl-C**: The `asyncio.CancelledError` path in `_run_agent_turn` cleanly exits. The event log is flushed (JSONL write is append-only and not buffered). The session can be resumed via `--continue`.

**Corrupt event log**: `restore_from_log()` skips malformed lines and logs warnings. The session may resume with a partial state, which is preferable to a complete failure.

**Connection lost mid-stream**: The `async for _chunk in _stream` loop will raise an exception. The except clause catches it, marks the turn as failed, and restores the input bar. The user can retry.

### 11.4 Error State Visibility

The transcript is the authoritative error record. Errors are never silently dropped. The user can always scroll up to find the full error context. `/history` shows the last 10 rendered lines, making recent errors easily accessible without scrolling.

---

## 12. Keyboard Navigation Map

### 12.1 Global Bindings (always active)

| Key | Action |
|---|---|
| `Ctrl-C` | Exit application |
| `Ctrl-D` | End of input (close buffer — only if empty) |
| `Shift+Tab` | Cycle mode (Auto → Plan → Ask → Review → Safe → Debug → Auto) |
| `Escape` | Dismiss topmost overlay / clear input (if overlay open) |

### 12.2 Input Bar Bindings

| Key | Action |
|---|---|
| `Enter` | Submit input; open slash command menu if text starts with `/` |
| `Up` / `Down` | Navigate input history (prompt_toolkit readline history) |
| `Ctrl-P` / `Ctrl-N` | Navigate input history (alternative) |
| `Ctrl-W` | Delete word before cursor |
| `Ctrl-U` | Clear entire input line |
| `Left` / `Right` | Move cursor character by character |
| `Ctrl-Left` / `Ctrl-Right` | Move cursor word by word |
| `Home` / `End` | Jump to start/end of input |
| `Ctrl-A` / `Ctrl-E` | Jump to start/end of input (Emacs-style) |
| `Shift+Enter` | Insert newline (multi-line input) |
| `Alt+Enter` | Alternative multi-line input submit |
| `Tab` | Complete @mention path or slash command |

### 12.3 Slash Command Dropdown Bindings

| Key | Action |
|---|---|
| `Up` / `Down` | Navigate command list |
| `Tab` | Select highlighted command, add to input |
| `Enter` | Select highlighted command and submit |
| `Escape` | Close dropdown, return to input |

### 12.4 Overlay Bindings

| Key | Action |
|---|---|
| `Escape` | Close overlay, return to transcript |
| `Up` / `Down` | Scroll overlay content (if scrollable) |
| `PgUp` / `PgDn` | Page overlay content |
| `Enter` | Confirm selection (session picker, etc.) |

### 12.5 Approval Gate Bindings

| Key | Action |
|---|---|
| `y` + `Enter` | Approve the pending tool call |
| `n` + `Enter` | Reject the pending tool call |
| `Enter` (empty) | Reject (default is No) |
| `Ctrl-C` | Exit application (tool auto-rejected) |

### 12.6 Design Rationale for Key Bindings

- **Shift+Tab for mode cycling** is chosen because Tab is reserved for completion and Ctrl+Tab is often captured by the terminal emulator. Shift+Tab is available and not commonly bound in terminal apps.
- **Escape-to-dismiss** is universally understood in terminal UIs (vi, less, htop all use it). Never using Escape to exit the application avoids accidental exits.
- **Ctrl-C for exit** is the universal terminal signal. No alternative exit path exists to prevent accidental closure.
- **Shift+Enter for newline** follows the convention established by Slack, Discord, and most modern chat interfaces. Alt+Enter is a fallback for terminals that intercept Shift+Enter.

---

## 13. User Mental Models

### 13.1 The Delegated Coder Model

The primary mental model is that of working with a skilled contractor. The user delegates a task ("refactor auth to use JWT"), the agent executes autonomously, and the user monitors progress. The transcript is the running work log. This model means:

- **Visibility over interaction.** Most of the time the user is reading, not typing.
- **Interruption is exceptional.** Approval gates are intentional interruptions; they should be rare and meaningful.
- **History matters.** The user should be able to look back at what the agent did and why.

### 13.2 The Collaborative Editor Model

The secondary mental model applies when the user is in Plan, Ask, or Review mode — tight back-and-forth. Here the transcript resembles a chat interface. This model means:

- **Each exchange is visible.** User messages and agent responses are interleaved in the transcript with clear visual separation.
- **Tool calls are secondary.** In Ask mode the agent doesn't run tools; in Plan mode it only reads. The transcript is mostly prose.
- **Mode is salient.** The mode badge on the status line needs to be noticed immediately. Colour-coding (green = Auto full power, red = Safe read-only) provides instant signalling.

### 13.3 The Audit Trail Model

For experienced users reviewing a completed session, the transcript serves as an audit trail. This model requires:

- **Every action is recorded.** Tool calls, diffs, errors, costs — nothing is hidden.
- **Resume shows history.** Resuming a session shows the last 20 Q&A pairs before the live input bar.
- **Scrollable.** The viewport scrolls to historical content without losing the input bar.

### 13.4 Conceptual Mapping: TUI → Agent OS

The TUI is the windowing layer for an agent OS. Users who understand this model can reason about:

- **Intents** = high-level tasks submitted via the input bar.
- **Agents** = workers that execute intents, shown as named turns in the transcript.
- **Tools** = atomic actions agents take, shown as `[tool]` lines.
- **Sessions** = persistent execution environments, resumed with `--continue`.
- **Modes** = capability gates that restrict what the OS lets agents do.

This model means advanced users can use the slash commands (`/status`, `/history`, `/mcp`) to inspect the underlying OS state — not just the chat history.

---

## 14. Rationale for All Major Decisions

### 14.1 Single Surface + Overlays (vs. Multi-pane / Tabbed)

**Alternative considered**: A tmux-style pane layout with a transcript pane, a tool output pane, and a status pane.

**Chosen approach**: Single transcript surface with floating overlays.

**Rationale**: Multi-pane layouts require the user to understand and manage pane focus — cognitive overhead that competes with the primary task of supervising the agent. Floating overlays that dismiss with Escape have zero navigation cost. The transcript already contains tool output inline, so a separate tool pane would be redundant. Pane layouts also interact poorly with terminal size changes and are hard to test deterministically (pyte tests become layout-sensitive).

### 14.2 Input Bar Pinned to Last Row (vs. Top of Screen / Floating)

**Alternative considered**: Input bar at the top (Vim-style command line) or floating near the cursor.

**Chosen approach**: Always last row.

**Rationale**: The user's eyes are naturally at the bottom of the screen after reading a long transcript. A top-row input bar would require a 180-degree gaze shift. A floating input bar creates layout unpredictability. Last-row placement is the convention established by bash, htop, and vim command mode. The technical implementation (HSplit with `input_window` last) trivially guarantees this — it cannot be accidentally displaced.

### 14.3 Inline Diffs (vs. Side-by-Side / Separate Diff View)

**Alternative considered**: Opening a separate diff viewer (like `git diff` in a pager).

**Chosen approach**: Unified diffs inline in the transcript, with collapse for large diffs.

**Rationale**: Side-by-side diffs are useful for human-authored code review where the reviewer wants to compare old and new simultaneously. For agent-authored diffs, the user primarily wants to confirm that the change is reasonable, not study it deeply. Unified diffs in-context (next to the tool call that produced them) reduce cognitive load by keeping cause and effect co-located. Large diffs are collapsed by default to prevent viewport flooding.

### 14.4 Status Line (vs. Title Bar / Sidebar)

**Alternative considered**: A top title bar or a right-side status sidebar.

**Chosen approach**: Single-row status line at `rows - 1`.

**Rationale**: Terminal windows may be narrow or wide, but their height is always at least a few rows. A top title bar would occupy the prime real estate where the agent's most recent output appears. A sidebar requires horizontal space that narrow terminals lack. A bottom status line is conventional (vim, htop, emacs) and provides the best balance of visibility and space efficiency. `rows - 1` specifically avoids interfering with the input bar at `rows`.

### 14.5 Approval Gates in Input Zone (vs. Modal Dialog / Inline Overlay)

**Alternative considered**: A modal dialog that takes over the full terminal.

**Chosen approach**: Approval gate replaces only the status-line row content; input bar remains.

**Rationale**: A full modal dialog would obscure the transcript context the user needs to make an informed approval decision. The user needs to see *what* the tool is about to do (visible in the transcript above) while deciding. Replacing only the status-line content keeps the full transcript visible, which is exactly the context needed for informed approval. The input bar remains on the last row so the physical typing target does not move.

### 14.6 Tail-Following Transcript (vs. Manual Scroll)

**Alternative considered**: Lock the viewport at the last human-scroll position.

**Chosen approach**: Auto-tail follows new content; manual scroll pauses auto-tail (future enhancement).

**Rationale**: During active agent runs, the user's primary need is to see what the agent is doing *now*. Auto-tail is the right default. Manual scroll with tail-follow-pause is a future enhancement; the current implementation always tails, which is correct for the primary supervisory use case.

### 14.7 `ShortTermMemory` Snapshot for Resume (vs. Full Event Log Replay)

**Alternative considered**: Replay all `IntentCreated` events and re-run the agent to rebuild LLM context.

**Chosen approach**: Serialise and restore `ShortTermMemory` snapshots in `ConversationStore`.

**Rationale**: Full replay would require re-running the LLM for every past turn — prohibitively expensive for long sessions. The `ShortTermMemory` snapshot is a compact, provider-specific representation of the conversation that the LLM can use directly. Combined with the last 20 Q&A pairs rendered in the terminal, the user and agent both have adequate context on resume without re-execution cost.

### 14.8 Event-Sourced Kernel (vs. Mutable State)

**Not a TUI decision, but the TUI design depends on it.** The event log is the source of truth for session replay. `TUIEventAdapter` subscribes to the kernel processor and translates `AppState` diffs into `TranscriptModel` mutations — the TUI never reads `AppState` directly. This means:

- The TUI is always consistent with the kernel state.
- Tests can replay arbitrary event sequences deterministically.
- The `--headless` mode uses the same event log without any TUI code.

### 14.9 Rich Console for Pre-TUI Output (vs. Custom Renderer)

Startup notifications (plugin loads, MCP discovery, LLM config errors) are rendered via `rich.Console.print()` before the live TUI loop starts. This means they appear in the terminal's normal scrollback, not in the TUI frame. This is intentional: pre-TUI output has different lifecycle characteristics (it appears once at startup and never changes) and is better suited to the terminal's native scrollback than to the frame-based TUI renderer.

### 14.10 SQLite for Conversation Store (vs. In-Memory / File-per-Turn)

`ConversationStore` uses SQLite (via `ProjectMemoryLayer`) rather than flat files or in-memory storage. Rationale: SQLite provides atomic writes, survives process interruption without corruption, and allows efficient range queries (last N turns, memory snapshot by session ID). File-per-turn approaches create thousands of small files for long sessions; in-memory storage loses data on crash.

---

*End of document.*
