# AI Coding Agent Terminal Interface UX Research

**Date**: June 2026  
**Scope**: Claude Code, Gemini CLI, OpenAI Codex CLI, Aider, Warp Terminal, OpenCode/Crush, Continue.dev, Cline, GitHub Copilot CLI  
**Purpose**: Inform AgentHICC TUI design decisions with grounded observations from the field

---

## 1. Executive Summary

The AI coding agent terminal space has converged on several structural patterns while diverging sharply on key design decisions. The best tools share a common foundation: they treat the terminal as a first-class UX surface rather than a debug console, they make every agent action observable at the right granularity, and they design approval workflows that protect without paralyzing.

The central tensions in the design space are:

**Observability vs. noise**: Per-action breadcrumbs (Claude Code) give maximum transparency but create scrollback pollution. Grouped action summaries (Gemini CLI) are cleaner but reduce real-time visibility. Neither approach is universally correct — the right answer depends on the risk level of the action.

**Approval frequency vs. flow**: Every tool in this survey has grappled with "approval fatigue" — the phenomenon where users start reflexively approving prompts without reading them because the volume is too high. The best tools implement tiered approval, contextual auto-approval, and session-scoped policies rather than a binary approve/reject for every action.

**Streaming fidelity vs. stability**: Token-by-token streaming creates a sense of liveness but causes visual instability (input bar jumping, flicker). Most tools have moved or are moving to alternate screen buffer rendering to solve this, but it introduces new constraints (native text selection breaks, Cmd+F search breaks).

**Diff placement**: Showing diffs inline in the conversation stream (Aider, Codex CLI) is immediately visible but interrupts reading. Showing diffs in a separate editor pane (Continue.dev) keeps the conversation clean but fragments attention.

**For AgentHICC specifically**: The project already uses `prompt_toolkit` HSplit with a pinned input bar — a good foundation. The research below identifies concrete patterns to adopt and specific anti-patterns to avoid.

---

## 2. Tool-by-Tool Analysis

### 2.1 Claude Code (Anthropic)

**Overview**: Anthropic's official CLI, released February 2025, generally available May 2025. Node.js-based, uses `readline` plus a custom alternate-screen renderer.

#### Conversation Display

The default mode uses a scrolling linear transcript — new output appends downward, input bar at the bottom. However, an opt-in fullscreen mode (`/tui fullscreen` or `CLAUDE_CODE_NO_FLICKER=1`) switches to an alternate screen buffer like `vim` or `htop`. In fullscreen:

- The input box stays fixed at the bottom and never jumps during streaming
- Only visible messages are rendered (memory stays flat across long sessions)
- Mouse events are captured for click-to-expand, URL clicking, text selection, and wheel scrolling
- Native `Cmd+F` search doesn't work directly — users press `Ctrl+O` to enter transcript mode, then `/` to search, or `[` to dump to native scrollback

The `/focus` command provides a quieter view: only the last prompt, a one-line tool-call summary with diffstats, and the final response. This is the "heads-down coding" mode.

**Response style**: Short sentences, bullet points, terse lists. More scannable in narrow terminals than competitors.

#### Tool Call Display

Tool calls are announced individually in sequence as they execute: "searched for regex XYZ", "read file `src/auth.ts`", "wrote 42 lines to `config.py`". This is a step-by-step breadcrumb trail approach — every action is logged before the next begins.

By default, MCP tool calls collapse to a single-line summary ("Called slack 3 times"). `Ctrl+O` opens the full transcript viewer for expansion. File paths in tool output are clickable in fullscreen mode, opening in the default application.

A feature request (GitHub issue #36462) for collapsible tool call sections — similar to the remote-control web interface — is open but not yet shipped. Users in long sessions find the accumulated tool output makes the transcript hard to scan.

#### Diff and File Change Visualization

When Claude proposes a file edit, it displays a unified diff inline:
- Removed lines prefixed with `-` in red
- Added lines prefixed with `+` in green
- File path shown above the diff block

The diff appears as part of the approval prompt, not as a separate step. After accepting, file paths are printed so you can `git diff` or open in an editor.

A known rendering bug (reproducible on WSL2, Claude Code 2.1.141) causes the diff preview to fall back to showing the entire new file as a single green block with no `-`/`+` prefixes. The source contains constants `DiffTooLarge`, `diffAdded`, `diffRemoved` indicating this is a conditional code path triggered incorrectly on some systems.

In `/focus` mode, only diffstats (lines added/removed count) appear per tool call rather than full diffs.

#### Streaming Responses

Text streams token-by-token. In the default non-fullscreen renderer, the input bar visibly jumps during active streaming — the primary motivation for fullscreen mode.

Extended thinking (toggled via `Alt+T`) streams as gray italic text above the response, but only visible in the `Ctrl+O` transcript viewer. Without that toggle, users see only a spinner during the thinking phase with no incremental content. This is an active complaint (GitHub issue #30660) — users want live streaming of the thinking process.

#### Long-Running Task Indicators

- A spinner is visible during model processing (custom verbs like "Flibbertigibbeting..." — configurable)
- A task list appears in the terminal status area with pending/in-progress/complete indicators. Toggle with `Ctrl+T`. Shows up to 5 tasks at once
- Background bash commands return a task ID immediately; Claude continues responding while the command runs in the background. Output is written to a file; Claude retrieves it via the Read tool. `Ctrl+B` moves a running bash command to background
- Session recap: when returning after 3+ minutes, Claude shows a one-line recap of what happened. Generated in the background so it's ready when you return
- PR status badge in the footer: green/yellow/red/gray underline showing PR review state, refreshing every 60 seconds
- Context usage: `/context` shows a colored grid of what's consuming context window space

#### Approval and Confirmation Workflow

Six named permission modes, cycled with `Shift+Tab`:

| Mode | Behavior |
|---|---|
| `default` | Prompts for every write/execute action |
| `acceptEdits` | Auto-approves file edits and common FS commands in working directory |
| `plan` | Read-only; proposes actions without executing |
| `auto` | Background AI classifier approves/blocks actions |
| `dontAsk` | Only pre-approved tools run |
| `bypassPermissions` | No checks (intended for CI/containers only) |

The current mode is always visible in the status bar at the bottom of the terminal.

Approval prompt format:
```
Claude wants to write to [file path]

y    Accept this change
n    Reject and tell Claude why
d    View full diff
e    Edit before accepting
Esc  Abort current operation
```

Protected paths (`.git`, `.vscode`, `.claude`, shell dotfiles, npm config) are never auto-approved in any mode short of `bypassPermissions`. Left/Right arrows cycle through tabs in permission dialogs.

Auto mode has a safety circuit: if the classifier blocks 3 consecutive actions or 20 total, it falls back to prompting.

#### Error Display

- Mistyped subcommands: `Did you mean claude update?` style suggestions
- Auto mode blocked actions: notifications that accumulate in `/permissions` under "Recently denied"; press `r` to retry with manual approval
- Display corruption: `Ctrl+L` forces full terminal redraw
- Scroll position preservation: the viewport remembers position when you scroll up to review earlier content

#### Keyboard Interactions

| Key | Action |
|---|---|
| `Shift+Tab` | Cycle permission modes |
| `Ctrl+C` | Interrupt running operation; second press exits |
| `Esc` | Stop current response mid-turn (keeps work done so far) |
| `Esc` + `Esc` | Clear input draft or open rewind/checkpoint menu |
| `Ctrl+O` | Toggle transcript viewer |
| `Ctrl+G` / `Ctrl+X Ctrl+E` | Open prompt in external editor |
| `Ctrl+T` | Toggle task list |
| `Ctrl+B` | Background a running bash command |
| `Ctrl+R` | Reverse search command history |
| `Ctrl+V` / `Cmd+V` | Paste image from clipboard |
| `Ctrl+X Ctrl+K` | Stop all background subagents |
| `Alt+T` | Toggle extended thinking |
| `Alt+P` | Switch model without clearing prompt |
| `Alt+O` | Toggle fast mode |
| `!` prefix | Shell mode (run commands directly) |
| `@` | File path autocomplete |
| `/` | Commands/skills menu |
| `/btw` | Side question — ephemeral overlay, doesn't affect history |
| `?` in transcript view | Show shortcut help panel |

Full vim editor mode available via `/config`. Input history scoped to working directory. `/btw` is particularly notable: a side question that sees full conversation context but runs in an ephemeral overlay and never enters history. Available even while Claude is processing.

#### What Makes Claude Code Good (and Bad)

**Good**:
- The six-mode permission system with `Shift+Tab` cycling and always-visible status bar is genuinely ergonomic
- Step-by-step tool announcement creates strong observability and auditability
- `/btw` side questions without polluting context is a novel, genuinely useful primitive
- Fullscreen mode with flat memory and mouse support solves real problems in long sessions
- Session recap on return, PR status badge, task list, rewind checkpoints — production-quality quality-of-life
- Transcript viewer with search (`Ctrl+O`) is a major advantage over competitors
- Unix-composable: `--output-format stream-json`, pipes work

**Bad**:
- Default renderer has noticeable flicker and jumping input bar — fullscreen is opt-in, not default
- Extended thinking shows only a spinner unless you navigate the transcript viewer
- The diff rendering bug (whole file rendered green on WSL2) is a real regression
- `$20/month` minimum cost
- Tool call output accumulates and clogs the transcript with no built-in collapse in the default renderer

---

### 2.2 Gemini CLI (Google)

**Overview**: Google's open-source CLI for Gemini models. Built with Ink (React renderer for terminals) rather than raw ANSI output. Uses an alternate screen buffer by default.

#### Conversation Display

Uses Ink's component-based, dynamic terminal rendering — architecturally distinct from Claude Code. Alternate screen buffer is default (not opt-in), meaning the input is anchored to the bottom from the start.

Mouse click-and-drag text selection requires `Ctrl+S` to temporarily exit mouse mode — a friction point Claude Code avoids in fullscreen mode.

Response style is longer paragraphs and numbered lists — more "chatty" and conversational than Claude Code's terse bullet points. Less scannable in narrow terminals.

The window title dynamically reflects state: `Ready: ◇`, `Action Required: ✋`, `Working: ✦`. This gives OS-level feedback even when the terminal is backgrounded.

REPL-style `gemini>` prompt entry point.

#### Tool Call Display

Gemini CLI groups actions: it thinks, takes multiple actions, then presents them in a single summary box. This is the inverse of Claude Code's per-action breadcrumbs. Some users find this cleaner; others lose real-time visibility since intermediate state is not shown.

Since v0.27.0, queued tool confirmations allow users to batch-review multiple pending tool executions before sequential approval — a significant usability improvement over per-action prompts.

Since v0.43.0, the model prefers surgical `edit` tool calls over full file rewrites by default.

#### Diff and File Change Visualization

Before file modifications, diffs are shown with a `Y/n` prompt. The format follows unified diff conventions. A native diff viewer integration allows viewing/approving changes in your editor's diff viewer rather than in-terminal.

Memory patches are written as unified `.patch` files held in `/memory inbox` until approved — a structured, reversible approach.

#### Streaming Responses

Text streams live via Ink's component re-render cycle. The `ui.incrementalRendering` setting reduces flickering during tool operations. A documented bug caused screen-wide flickering in narrow terminals — fixed via "Terminal Buffer mode" in v0.38.0.

Extended thinking: waits for streaming to complete before generating a summary (not live-streamed). During processing, configurable "loading phrases" or tips are shown alongside the spinner.

#### Long-Running Task Indicators

- Spinner with configurable loading phrases ("Did you know..." tips) during model processing
- Wave animations during voice mode interactions (v0.42.0)
- Window title updates reflect state changes at OS level
- Background command completion configurable as `silent`, `inject` (output injected into conversation), or `notify`
- Interactive shell (v0.9.0+): run `vim`, `top`, `git rebase -i` directly inside Gemini CLI using PTY with real-time output

#### Approval and Confirmation Workflow

Three modes + YOLO mode:

| Mode | Behavior |
|---|---|
| `default` | Prompts before each tool execution |
| `auto_edit` | Auto-approves edit-specific tools only |
| `plan` | Read-only |

YOLO mode (`Ctrl+Y`): auto-approves all tool calls for the session.

Confirmation prompt format:
```
Edit src/routes/health.ts? (y/n/a)
```

The `a` option auto-approves similar operations for the rest of the session. Since v0.20.0, "Always Allow" policies persist across sessions for specific trusted commands. Sticky headers in confirmation UI keep the action summary anchored at the top during approval flows.

`security.enablePermanentToolApproval` config adds "Allow for all future sessions" button. `security.disableAlwaysAllow` removes blanket approval options.

#### Error Display

- Tool execution success/failure shown with status messages in conversation flow
- Window title reflects stuck/error states
- Historical issue: infinite "thinking" hangs with certain models (GitHub issues #2025, #5504, #21937) — a reliability concern

#### Keyboard Interactions

| Key | Action |
|---|---|
| `Shift+Tab` | Cycle approval modes |
| `Ctrl+Y` | Toggle YOLO mode |
| `Ctrl+C` | Quit/cancel request |
| `Ctrl+S` | Toggle mouse mode (for text selection) |
| `Ctrl+R` | Reverse history search |
| `Ctrl+G` | Open in external editor |
| `Ctrl+L` | Clear screen |
| `Ctrl+T` | Toggle TODO list |
| `Alt+M` | Toggle markdown rendering |
| `Ctrl+F` | Focus terminal (interactive shell) |
| `F12` | Debug console |
| `Tab` | Switch focus between shell and input |
| `?` on empty prompt | Toggle shortcuts panel |

Slash commands: `/compress`, `/copy`, `/mcp`, `/clear`, `/tools`, `/stats`, `/memory show`, `/memory refresh`, `/chat save <tag>`, `/restore`, `/theme`, `/vim`, `/init`, `/settings`, `/bug`.

#### What Makes Gemini CLI Good (and Bad)

**Good**:
- Alternate screen buffer by default — stable input from the start, no opt-in needed
- Ink/React renderer allows richer dynamic UI updates
- YOLO mode and session-scoped `a` option enable flow state
- Persistent "Always Allow" policies reduce repetitive prompts for trusted commands
- Batch tool confirmation queue is ergonomic
- Interactive PTY shell (vim, htop inside the CLI) is genuinely powerful
- Free tier with 1M token context window
- Dynamic window title provides ambient status

**Bad**:
- Text selection requires `Ctrl+S` mode toggle — awkward
- Extended thinking not live-streamed
- Flickering in narrow terminals was a persistent issue (now largely fixed)
- Grouped action summaries reduce real-time observability
- Longer paragraph responses are less scannable
- Historical reliability issues with model hangs

---

### 2.3 OpenAI Codex CLI

**Overview**: OpenAI's official CLI for the Codex/o-series models. Built in Node.js with Ink (React for terminals), full-screen alternate-screen TUI. Described by reviewers as "Apple-esque in a Linux world" — polished consumer-grade formatting with intelligent color use and good markdown rendering.

#### Conversation Display

Full-screen terminal UI with syntax-highlighted markdown code blocks and diffs. Input is fixed at the bottom. Users navigate draft history with Up/Down in the composer to restore prior messages.

#### Tool Call Display

A transcript of actions is surfaced so users can review or roll back changes with their usual git workflow. Codex explains its plan before making a change and users approve or reject steps inline. Actions are shown as they happen, not grouped.

#### Diff and File Change Visualization

The TUI syntax-highlights fenced markdown code blocks and file diffs — code is easier to scan during review. Diffs are shown inline as part of the approval step.

#### Streaming Responses

During streaming, users can:
- Press `Enter` to inject new instructions into the current turn
- Press `Tab` to queue follow-up input for the next turn

This real-time interruption model is distinctive — most tools don't allow mid-stream redirection.

#### Approval and Confirmation Workflow

Three modes:

| Mode | Behavior |
|---|---|
| Auto (default) | Can read files, edit, run commands in working directory; asks before external scope |
| Read-only | Browse files only; won't make changes or run commands until you approve a plan |
| Full Access | Works across entire machine including network, without asking |

Mode switching via `/permissions` command during sessions.

The sandbox layer (separate from approval policy) controls technical access via `sandbox_mode` configuration. Approval policy and sandbox are independently configurable, allowing nuanced security postures.

#### Keyboard Interactions

| Key | Action |
|---|---|
| `Enter` (during generation) | Inject new instructions mid-turn |
| `Tab` (during generation) | Queue follow-up for next turn |
| `Ctrl+C` or `/exit` | Close session |
| `Ctrl+L` | Clear screen, keep conversation |
| `Ctrl+O` | Copy latest output |
| `Ctrl+R` | Search prompt history |
| `Ctrl+G` | Open external editor for longer prompts |
| `@` | Fuzzy file search |
| `Esc` (x2) | Edit previous user message |
| `Up/Down` | Navigate draft history |

#### What Makes Codex CLI Good (and Bad)

**Good**:
- Mid-stream injection (`Enter` during generation) is genuinely novel and useful
- Rust-based performance — very fast TUI rendering
- Dual-layer security model (sandbox + approval policy) is flexible and principled
- Syntax-highlighted diffs make code review easier
- Draft history navigation with Up/Down is ergonomic

**Bad**:
- Three-mode system is less granular than Claude Code's six modes
- Less publicly documented than Claude Code or Gemini CLI
- Newer tool — community tutorials and known bugs are less well-documented

---

### 2.4 Aider (aider-chat)

**Overview**: Open-source, model-agnostic terminal pair programmer. The oldest major tool in this space; pure Python, runs in any terminal. Does not use alternate screen buffer — runs in the standard scrollback.

#### Conversation Display

Aider runs in the standard terminal scrollback — no alternate screen. This means it works in any terminal without configuration but the input bar can scroll away during long outputs.

Aider uses a REPL with mode-specific prompts:
- `> ` — code mode (default)
- `ask> ` — ask mode  
- `architect> ` — architect mode

The mode prompt itself communicates state — simple but effective.

#### Tool Call Display

Aider does not display tool calls in the LLM sense — it is architecturally different. Rather than having the model invoke tools, Aider sends the model a structured edit format request and parses the response to apply changes. The user sees:

1. The model's response (streaming, in colored text)
2. A "Applied edit to `filename.py`" message for each file change
3. A git commit message once the changes are committed

This is simpler and more predictable than the tool-call paradigm but less flexible.

#### Diff and File Change Visualization

When Aider edits files, it commits them immediately with a descriptive git commit message (Conventional Commits format by default). The user can run `/diff` to show all file changes since the last message, or use standard git tools.

The `/diff` command shows changes in the terminal using the standard git diff format with red/green coloring. The immediate git commit means rollback is always `git revert` — well-understood and trustworthy.

Configurable: `--diff` flag controls whether diffs are shown automatically on each commit. Colors for diffs and tool output are individually configurable via `--user-input-color`, `--tool-output-color`, `--tool-error-color`, `--assistant-output-color`.

#### Streaming Responses

Token-by-token streaming in the standard scrollback. The assistant's output is colored differently from the user's input and tool output, creating visual separation. `--no-stream` disables streaming (shows cache stats afterward).

#### Long-Running Task Indicators

No dedicated spinner or progress indicator — the streaming response itself indicates activity. A terminal bell notification can be enabled (`--notifications`) to signal when the LLM finishes a long response. For very long operations, this can look like the tool is hanging.

#### Approval and Confirmation Workflow

Aider's approval model is fundamentally different from every other tool in this survey: it does **not** ask permission before editing files. Instead:

1. You request a change in natural language
2. Aider makes the change and commits it to git
3. You review with `/diff` or `git diff`
4. You run `/undo` to reverse if you don't like it

This "commit first, review second" model is either brilliant or alarming depending on your workflow. For developers comfortable with git, it's fast. For those who want pre-approval, it's unsettling.

The safety net is git — every edit is committed, every commit is reversible. Stage-before-editing: Aider stages any uncommitted work before making changes, ensuring no existing work is lost.

#### Error Display

- Tool errors appear in the configured `--tool-error-color` (red by default)
- Git conflict or edit application failures are shown inline with a description
- The `/test` command runs the test suite and surfaces failures inline

#### Keyboard Interactions

Aider uses standard readline conventions (no custom keybindings beyond what readline provides). The primary interaction model is slash commands:

| Command | Action |
|---|---|
| `/add <file>` | Add file to the context window |
| `/drop <file>` | Remove file from context |
| `/read-only <file>` | Add file as read-only reference |
| `/diff` | Show all changes since last message |
| `/undo` | Undo and discard last change |
| `/commit` | Commit all dirty changes |
| `/git <args>` | Run raw git command |
| `/code` | Switch to code mode for this message |
| `/ask` | Switch to ask mode for this message |
| `/architect` | Switch to architect mode for this message |
| `/chat-mode <mode>` | Permanently switch active mode |
| `/model <name>` | Switch model |
| `/web <url>` | Scrape URL to markdown |
| `/paste` | Add image from clipboard |
| `/voice` | Record audio via OpenAI Whisper |
| `/test` | Run test command |

**Architect mode** is particularly notable: a two-model pipeline where:
1. The architect model proposes the approach (streaming visible to user)
2. The editor model translates the proposal into specific file edits (editor-diff or editor-whole format)

The user sees both stages but doesn't need to manage the handoff. The `architect>` prompt indicates this mode is active.

#### What Makes Aider Good (and Bad)

**Good**:
- Git-first philosophy means undo is always available and trustworthy
- Architect mode's two-model pipeline gives high-quality edits with visible reasoning
- Voice input via Whisper is a genuine differentiator
- Completely model-agnostic (any OpenAI-compatible API)
- No alternate screen — works in tmux, vim, any environment
- The `/add`/`/drop` file context management is explicit and predictable
- Very configurable: colors, edit formats, commit styles, models per mode

**Bad**:
- "Commit first, ask forgiveness later" approval model requires git discipline
- No spinner/progress for long operations
- Runs in standard scrollback — input bar can scroll away
- No image paste in default configuration (requires clipboard setup)
- Sequential modifications can cause Aider to overwrite its own changes in complex refactors

---

### 2.5 Warp Terminal

**Overview**: Not a coding agent in the same sense — Warp is a full terminal replacement that has integrated AI throughout. The "blocks" architecture is the central UX innovation.

#### Conversation Display

Warp's AI chat is accessed via `Ctrl+\` (Agent Mode) and appears in a panel that integrates with the terminal rather than replacing it. Each shell command produces a **block** — a discrete UI unit containing:
- Command header: exact command, timestamp, exit status
- Output body: filterable and searchable
- Action buttons: copy, share, save as snippet

Blocks can be navigated with `Cmd+↑/↓`, selected, and shared. This makes the terminal's output structured data rather than a continuous stream.

#### Tool Call Display

In Agent Mode, Warp narrates its steps and confirms before executing each one. The agent "plans, narrates, and executes a sequence with confirmation at each step." Since 2026.04.08, agents can ask clarifying questions during execution.

MCP tool calls use the same approval model and audit trail as shell commands — consistent semantics across tool types.

#### Diff and File Change Visualization

When a compiler error or merge conflict is detected, Warp drops an inline diff that users can accept, edit, or dismiss. This is proactive — Warp detects the situation and offers the diff without being asked.

**Warp Code** (launched September 2025) adds two major diff-related features:

**Native Code Editor**: Tabbed file viewer with syntax highlighting, find-and-replace, vim keybindings, file tree, and file palette (`Cmd+O`). Designed for lightweight in-flow edits alongside agent conversations. Not a full IDE replacement.

**PR-style Code Review Panel**: Opens when an agent modifies files, gathering all edits into a single diff. Users can:
- View every change across all modified files
- Leave inline comments anchored to specific file + line
- Submit a batch of feedback to the agent at once (agent applies all and returns an updated diff)
- Hand-edit diffs directly in the diff view without re-prompting

**Interactive Code Review**: As the agent writes code, a **live diff view shows every line being added or changed in real time**. Users can interject mid-execution; the agent pauses, adjusts, and continues.

This is the most sophisticated diff workflow in the survey — the only tool with PR-style line commenting on agent-generated changes.

#### Streaming Responses

AI command suggestions appear as inline suggestions in the input bar (like shell autocomplete but AI-powered). Agent mode streams narration of planned steps before executing.

Warp's "Active AI" watches shell state (current directory, recent commands, exit codes, branch, recent output) and surfaces contextual prompts automatically — "I see you have a failing test, would you like me to fix it?"

#### Long-Running Task Indicators

Agent 3.0 supports sub-agents for parallel tasks. Audit trail and trace are shown for each agent action. "Oz agent system" can monitor GitHub repositories, read issues, generate specifications, and draft PRs autonomously.

#### Approval and Confirmation Workflow

Each agent step requires confirmation. Since blocks are first-class objects with stable IDs, approvals can reference specific blocks. The same approval semantics apply to both shell commands and MCP calls.

#### What Makes Warp Good (and Bad)

**Good**:
- Blocks architecture makes terminal output structured and queryable rather than ephemeral text
- Active AI context (watching exit codes, directory, branch) proactively surfaces help
- Inline diff when errors detected — proactive, not reactive
- Sharing blocks as structured artifacts (not screenshots) is genuinely useful for collaboration
- `Cmd+I` natural language to command is fast for common operations
- Vertical tabs (2026.04.08) for terminal tab management

**Bad**:
- Requires account and API credits (metered AI, free terminal)
- Not universally portable — desktop app only, macOS and Linux, not embedded in other tools
- Less suited for scripting/CI use cases than pure CLIs
- Agent capabilities require trust in cloud connectivity

---

### 2.6 OpenCode (SST) / Crush (Charmbracelet)

**Note on naming**: There are two distinct tools called "OpenCode":
1. **OpenCode by SST** (`github.com/sst/opencode`): A TypeScript/Zig TUI launched June 2025, still active and maintained
2. **OpenCode by Anomaly Innovations** (`github.com/opencode-ai/opencode`): A Go/Bubble Tea TUI archived September 2025, continued as **Crush** by Charmbracelet

Both are covered below.

---

#### 2.6a OpenCode (SST)

**Overview**: Built by the SST/Serverless Stack team. Reached 150k GitHub stars quickly after June 2025 launch. Uses a custom **OpenTUI** framework (TypeScript + Zig bindings) targeting 60 FPS rendering via dirty rectangle optimization. Client/server architecture — the TUI is a client connecting to a local HTTP/SSE server, enabling future desktop app and IDE extension clients to share the same backend.

**Layout**: Four main zones — Message Area (main pane), Input Area (bottom), Status Bar (session name, model, agent mode), and optional Sidebar (file explorer + tool list). A dedicated **Diff Viewer Panel** (added v1.15.6) shows a file tree alongside diffs, with `A`/`M`/`D` file status indicators and next/previous hunk navigation.

**Plan vs Build mode**: Switched with `Tab` key. Build mode (default): full read+write. Plan mode: read-only; agent describes its intended approach step by step without touching files. The workflow is: switch to Plan → iterate on the plan → switch to Build to execute.

**Permission dialog**: When the agent wants to write a file or run a command, it blocks all other input and shows a modal with:
- Human-readable description of the operation
- Absolute file path
- Unified diff preview with 4 lines of context (known limitation — open feature request for full-file context view)
- Tool name making the request

Keyboard: `a` (allow once), `A` (allow for session, persisted in SQLite), `d` (deny), Left/Right/Tab to navigate options.

**Thinking blocks**: Collapsible inline display of reasoning from reasoning models. `/details` slash command toggles tool execution detail visibility. Long tool outputs are collapsed to keep layout readable.

**Undo**: `/undo` and `/redo` commands restore file changes via git. Every edit is git-tracked.

**@ context**: Fuzzy file search with line range syntax (`file.ts#10-20`). Structured as virtual text extmarks visually distinct from regular message text.

**Keyboard leader key**: `Ctrl+X` prefix (2000ms timeout) for session/model operations: `Ctrl+X M` (list models), `Ctrl+X N` (new session), `Ctrl+X L` (session list), `Ctrl+X C` (compact), `Ctrl+X E` (external editor). `F2`/`Shift+F2` cycles recent models quickly.

**Good**: Permission dialog with inline diff is excellent. Plan/Build toggle is low-friction. Git-backed undo. Provider-agnostic. AGENTS.md for persistent architectural context across model switches.

**Bad**: 4-line diff context is often insufficient. Custom TUI framework had early rough edges. No PR-style code review panel like Warp.

---

#### 2.6b OpenCode (Anomaly Innovations) / Crush (Charmbracelet)

**Overview**: OpenCode by Anomaly Innovations was archived September 2025 and continued as **Crush**, developed with Charmbracelet (the creators of `bubbletea`, `lipgloss`, `glow`). Crush is the production successor.

#### Conversation Display

Crush uses the Elm-inspired Bubble Tea architecture — the entire interface is a pure function of application state. Every keystroke generates a message that flows through an update function, producing new state and side effects.

Layout structure:
- **Header**: Session title, model information, context window usage
- **Chat viewport**: Message history with scrolling and "follow mode"
- **Editor**: Multi-line textarea for user input with command history
- **Pills**: Status indicators for active todos and prompt queue at bottom
- **Dialog overlay**: Modal stack for commands, model selection, session management

A centralized `Styles` struct manages all visual presentation through `lipgloss`, including standardized icons, component-specific styles, token counts, cost displays, and reasoning effort level rendering.

The PubSub system bridges backend events (LSP, MCP, Agent) into the Bubble Tea message loop, keeping UI synchronized with backend services through typed event messages.

#### Tool Call Display

Crush's tool call display is a known weakness. The tool name is shown early (via `OnToolInputStart`) for immediate feedback, but **input JSON parameters are not streamed incrementally** — users do not see parameters building in real-time. This is acknowledged as a gap (GitHub issue #1714); improvements proposed include real-time parameter preview, per-tool progress bars, and pretty-printed syntax-highlighted JSON output.

Current status icons: `✓` success, `⋯` thinking/loading, `●` in-progress tool call, `→` active task item.

Permission dialog with keyboard controls:
- Arrow keys navigate approval options
- `a` (allow once), persistent `GrantPersistent` option auto-approves identical future requests in the session
- `d` (deny)
- Only one permission dialog shows at a time (serialized via mutex)
- Desktop notifications fire for permission requests

File modification tracking is a dedicated feature: "File Change Tracking" visualizes file changes during sessions.

A major architectural advantage: messages are cached after completion (`Finished()` returns true, rendering freezes), and ANSI sequence decoding is memoized in a `ScreenBuffer` — making long session rendering efficient.

#### Diff and File Change Visualization

Diffs appear as part of the file change visualization. The `patch` tool handles code modifications; output is rendered alongside AI responses.

#### Session and Model Management

Crush supports switching between different LLMs within the same session while preserving context — a distinctive capability. Each project maintains multiple concurrent sessions stored as JSON files in `~/.config/crush/sessions/`, with each session preserving not just conversation history but the entire context graph.

LSP integration provides code understanding. MCP integration provides extensible tool access.

#### Keyboard Interactions

Vim-style keyboard-first design:
- `i` — focus editor
- `j`/`k` — navigate lists
- `Ctrl+S` — send messages
- `Ctrl+E` — launch external editor
- `Ctrl+K` — open command dialogs

Permission dialog: arrow keys to navigate, `a` to allow, `d` to deny.

#### What Makes Crush Good (and Bad)

**Good**:
- Clean, well-designed TUI via Bubble Tea + lipgloss — responsive and visually coherent
- Model switching within a session while preserving context is a unique capability
- LSP integration for code intelligence (not just file reading)
- Elm-inspired architecture makes state management predictable
- Cost and token count display with reasoning effort levels
- Both opencode.ai (SST) heritage and Charmbracelet's TUI expertise

**Bad**:
- OpenCode was archived September 2025 — Crush is still maturing
- Less documentation available compared to Claude Code or Gemini CLI
- Go binary distribution — no `pip install` path for Python users
- Smaller community and ecosystem than Anthropic/Google tools

---

### 2.7 Continue.dev

**Overview**: Open-source IDE extension (VS Code and JetBrains) focused on inline code editing with AI. Has both chat sidebar and inline edit modes. Not a standalone terminal tool — requires an IDE.

#### Conversation Display

Continue's sidebar chat is accessed via `Cmd/Ctrl+L`. It supports `@` mention syntax for injecting context:
- `@Codebase` — semantic search across indexed codebase
- `@Folder` — specific folder context
- Terminal output context: "reference the last command you ran and its output"
- Files, documentation, web pages

Codebase indexing uses embeddings (local transformers.js by default, stored in `~/.continue/index`) with semantic + keyword search. This is the richest context injection system in the survey.

#### Tool Call Display

Continue operates in three distinct modes:

**Chat mode**: Conversational, with code suggestions that must be manually applied via action buttons (Apply to file, Insert at cursor, Copy to clipboard) that appear below code blocks. No autonomous tool calls.

**Edit mode** (accessed via `Cmd/Ctrl+I`): AI edits highlighted code directly. The model response streams directly into the highlighted range as an inline diff. A recent improvement ("Instant edits") replaced streaming diffs with synchronous application for find-and-replace operations, with automatic scrolling to modified code.

**Agent mode**: Full tool access (file create/edit/delete, terminal command execution). Requires clicking "Continue" to approve each tool call, or configuring tool policies for auto-approval. Tool status indicators show when tools are actively running. Thinking tags display dimmed during processing.

The mode selector is a dropdown in the bottom-left of the input box. `Cmd/Ctrl+.` cycles through modes.

#### Diff and File Change Visualization

Edit mode shows proposed changes as inline diffs within the highlighted text. Navigation:
- `Cmd/Ctrl+Opt+Y` — accept individual change
- `Cmd/Ctrl+Opt+N` — reject individual change
- `Cmd/Ctrl+Shift+Enter` — accept all changes
- `Cmd/Ctrl+Shift+Delete/Backspace` — reject all changes

If you accept: previously highlighted lines removed, proposed changes applied. If you reject: proposed changes removed, original restored. The diff streams directly into the file (not a preview buffer), making acceptance feel immediate.

#### What Makes Continue Good (and Bad)

**Good**:
- Richest context injection system (`@` mentions with codebase indexing, web, docs, terminal)
- Edit mode inline diff is immediate and responsive
- Local embedding index for semantic search — no cloud dependency for search
- Agent mode with codebase/documentation awareness guides multi-step tasks
- Works within existing IDE workflow — no context switching

**Bad**:
- IDE extension, not a standalone terminal tool — requires VS Code or JetBrains
- Chat panel is an additional UI surface on top of the editor, increasing visual complexity
- Codebase indexing has known reliability issues (`@codebase` retrieval doesn't always include relevant files — multiple GitHub issues)
- No autonomous execution — requires manual apply for every change

---

### 2.8 Cline (VS Code Extension)

**Overview**: Open-source VS Code extension for autonomous AI coding. Also available as CLI and SDK. Known for step-by-step approval model with real cost tracking.

#### Conversation Display

Cline operates as an embedded VS Code extension panel (a React webview). Responses stream character-by-character with a typing effect. The chat renders inline tool-call blocks within the thread — not in separate panes.

The key architectural fact: Cline uses **"generative streaming UI"** triggered by XML-tagged tool call delimiters in the model output. As the model streams its response, tool call UI components are generated on the fly — they are not post-processed from a completed response. This means the interface feels alive during generation.

During streaming for file writes: a **semi-transparent overlay** covers unprocessed content in the editor diff view, with the current write position visually highlighted. The diff fills in progressively as the model generates.

Each task shows a chronological list of actions taken: file reads, edits, terminal commands, browser interactions.

#### Tool Call Display

Every tool call is a visible checkpoint. The approval-gated approach means users see each proposed action before it runs:
- File edit proposals show the file path and proposed content
- Terminal commands show the exact command to be run
- Browser actions show the URL and interaction type

Visual diffs land in the editor's diff viewer for file edits — the IDE-native diff experience rather than terminal-rendered ANSI diffs.

Auto-approval is configurable per action type: reading files can be auto-approved while terminal commands require explicit confirmation.

#### Diff and File Change Visualization

Diffs appear in VS Code's native diff viewer — the same view used for git diff. This is the most comfortable diff experience for VS Code users since it's identical to tools they already know.

#### Long-Running Task Indicators

Cline displays running costs in real-time (tokens consumed, cost in dollars) as part of the task panel. This is the strongest cost visibility in the survey — but also the most alarming when costs accumulate faster than expected. A known failure mode: retry loops can generate unexpected large bills if not caught early.

The task panel shows task progress with checkboxes for completed sub-tasks.

#### Approval and Confirmation Workflow

Per-action approval with configurable auto-approve thresholds. The philosophy is "every file edit and terminal command requires your explicit approval unless you configure otherwise."

Checkpoint rollbacks: Cline can revert to any previous checkpoint in the task. `/undo` reverts the last change.

The MCP connection management requires editing JSON config files directly — not a GUI workflow.

#### What Makes Cline Good (and Bad)

**Good**:
- Real-time cost tracking (tokens + dollars) per task is the most transparent cost UX in the survey
- Checkpoint rollbacks at any step — very strong undo story
- VS Code native diff viewer for file changes
- Auto-approval configurable per action type — granular control
- Browser control, file system, terminal — the broadest tool surface
- Iterative self-correction: reads test failures, adjusts code, reruns tests autonomously

**Bad**:
- Real-time cost display can be alarming — some users feel anxiety watching the counter
- Retry loops can generate large unexpected bills if auto-approval is too permissive
- MCP configuration requires JSON editing (not GUI)
- VS Code extension only — not a standalone terminal tool
- The approval-for-everything model can create fatigue on complex tasks

---

### 2.9 GitHub Copilot CLI

**Overview**: GitHub's standalone CLI for AI coding assistance (`copilot` command, not to be confused with the deprecated `gh copilot` extension). The old `gh copilot` with `suggest` and `explain` subcommands was deprecated with EOL October 25, 2025 — it was a simple command suggester, not an agent. The new `copilot` CLI is an agentic executor. Major UI redesign at Microsoft Build 2026 (June 2026).

On first launch in a directory, users see a trust confirmation prompt — identical semantics to VS Code's "Trust Workspace" dialog.

#### Conversation Display

The redesigned interface (June 2026) introduces:
- **Tab navigation**: Session view (default), Issues, Pull requests, Gists — all accessible without leaving the CLI. Press Tab to switch between views.
- **Color modes**: `default`, `github`, `dim`, `high-contrast`, `colorblind` — the most accessibility-focused color system in the survey
- **Screen reader support**: On by default when detected; labeled icons and disabled animations
- **Responsive layout**: Adapts to narrow terminals without truncating critical content
- **Theme-aware semantic colors**: Adapts to terminal's light/dark background

The tab system integrates GitHub context (issues, PRs, gists) directly into the conversation — you can reference open issues while coding without switching tools.

#### Streaming and Mid-Stream Enqueuing

Responses stream in real-time. The interface allows **enqueuing additional messages while Copilot is processing** — you can send follow-up prompts without waiting for the current response to complete. `Ctrl+T` toggles display of the model's reasoning process (thinking tokens).

Tool approval during streaming: numbered prompt (1: allow once, 2: allow for session, 3: reject). Session-wide approval auto-approves pending parallel permission requests of the same type instantly.

`/fleet` command runs concurrent multi-task execution from a single prompt — fans out to multiple parallel agents. Unavailable in the VS Code Chat extension.

#### Rubber Duck Mode

Built-in second-opinion agent: reviews the main agent's current plan, design, implementation, or tests and identifies "blind spots, design flaws, and substantive issues." Invoked via `/rubber-duck` or automatically when the system determines a second opinion would help. This is a unique architectural feature not seen in other tools.

#### Prompt Scheduling

`/every` and `/after` commands let developers schedule recurring or one-time prompts. A schedule manager displays active schedules and allows deletion. This enables asynchronous AI work while the developer does other things.

#### Voice Input

Hold spacebar or `Ctrl+X` then `V` to record spoken prompts. Processing is local (privacy-preserving).

#### What Makes Copilot CLI Good (and Bad)

**Good**:
- GitHub context integration (issues, PRs, gists in tabs) is genuinely unique
- Rubber duck mode for self-review is architecturally novel
- Best accessibility features in the survey (screen reader, colorblind mode, responsive)
- Prompt scheduling for async AI work
- Local voice processing preserves privacy

**Bad**:
- Requires GitHub account and Copilot subscription
- The older `gh copilot suggest/explain` (now deprecated) was one-shot, not agentic
- Tab system adds complexity vs. simpler CLIs
- Less documentation on approval workflows and tool call display than competitors

---

## 3. Universal UX Patterns That Work Well

### 3.1 Anchored Input Bar

Every tool that has shipped an alternate screen buffer (Claude Code fullscreen, Gemini CLI default, Codex CLI, Crush/OpenCode) reports the same thing: users want the input bar to stay fixed at the bottom. A jumping input bar that scrolls off during streaming output is the single most complained-about issue across the survey.

**Mechanism**: Use an alternate screen buffer (like `vim` does). The conversation scrolls within a fixed viewport; the input bar never moves.

### 3.2 Permission Mode Cycling with Visible Status

Claude Code's `Shift+Tab` to cycle through named permission modes — with the current mode always visible in the status bar — is the best-designed permission UX in the survey. Users can see at a glance what level of autonomy they've granted without entering any menu.

**Key insight**: The mode name should be visible at rest, not just when a prompt fires. Users should know their current mode before they need to approve the next action.

### 3.3 Session-Scoped Auto-Approval

Both Claude Code (`acceptEdits` mode, auto mode's learning) and Gemini CLI (the `a` option, "Always Allow" policies) provide ways to approve a class of actions once per session rather than once per occurrence. This directly addresses approval fatigue without removing oversight.

**Pattern**: "Approve this forever" / "Approve for this session" / "Approve once" — three levels of permanence at the approval prompt.

### 3.4 Inline Diff Before Write

Every tool shows a diff before writing to disk (except Aider, which commits immediately). The unified diff format (red `-` / green `+`) is universal and expected. Showing the diff inline in the conversation stream (rather than switching to a separate tool) keeps the workflow in one place.

**Key insight**: The diff should appear before the `y/n` prompt, not after. Users should not need to request the diff — it should be the default.

### 3.5 Transcript Viewer / History Search

Claude Code's `Ctrl+O` transcript viewer with search is a significant quality-of-life feature for long sessions. Without it, the only way to review past tool calls is to scroll up through the terminal scrollback — fragile and terminal-dependent.

**Pattern**: A dedicated transcript mode that shows the full history, allows search, and can be dismissed without losing the current context.

### 3.6 `@` File Mention with Autocomplete

All major tools that support file context injection use `@` as the trigger character. Continue.dev has the richest implementation (`@Codebase`, `@Folder`, `@Docs`, `@Web`). Claude Code and Codex CLI have `@` for file path autocomplete. This is a de facto standard.

### 3.7 Git as Undo Layer

Aider's entire philosophy and Codex CLI's transcript-plus-git model both demonstrate that git is the right undo primitive for coding agents. "Use git revert" is a better undo story than "press undo in the UI" because it's auditable, granular, and integrates with existing developer workflow.

**Pattern**: After each agent session, the diff should be reviewable via standard git tools. Shadow git snapshots (as in the OpenDev research) enable per-step rollback.

### 3.8 Background Task Support

Claude Code's `Ctrl+B` to background a bash command and continue responding, Gemini CLI's configurable background command completion, and Codex CLI's mid-stream injection all reflect the same insight: long-running tasks should not block the conversational interface. Developers should be able to issue a build command and continue talking to the agent while it runs.

### 3.9 Side Questions (Ephemeral Context)

Claude Code's `/btw` command — a question that sees the full conversation but doesn't enter history — is novel and valuable. It solves the "I want to ask a quick question without derailing the current task" problem. The ephemeral overlay pattern is immediately understandable.

### 3.10 Mode Indicator in Status Bar

The status bar should show at minimum: current permission mode, model name, context usage, and active task count. Claude Code, Gemini CLI (via window title), and Crush (via header) all provide some variant of this ambient status. Users should not need to ask the agent what mode it's in.

---

## 4. Anti-Patterns to Avoid

### 4.1 Approval Fatigue

Prompting for approval on every single file read, every list operation, every status check creates a stream of meaningless confirmations. After 15-20 approvals in a session, users begin approving reflexively without reading. This is documented across multiple research papers and confirmed by practitioner reports.

**Specific signals that approval fatigue has set in**:
- Approval times drop while action complexity stays constant
- Users think "probably fine" rather than verifying the diff
- Users enable `bypassPermissions` or YOLO mode as a permanent state (not as a conscious choice for a specific session)

**Fix**: Reserve approval prompts for high-risk actions. Read operations, directory listings, and non-destructive commands should auto-approve within a configured safe zone (e.g., the project working directory).

### 4.2 Jumping Input Bar

Streaming responses that push the input bar off-screen as output accumulates create UX friction. The user's cursor position becomes unpredictable. Every major tool has addressed or is addressing this. It remains the most commonly cited complaint in Claude Code GitHub issues.

**Fix**: Alternate screen buffer with the input anchored to the last row.

### 4.3 Invisible Tool Execution

Saying "Done!" after a long autonomous session without showing what changed is a trust-destroying pattern. Users must be able to audit what the agent did, even if they choose not to read the full log.

**Fix**: Every session should end with a receipt: files changed (count + paths), commands run, errors encountered. The `/focus` summary view in Claude Code is a good model.

### 4.4 Undiffed File Writes

Applying file edits without showing the diff first (or applying them too quickly for the user to see) removes the opportunity for review. Aider's commit-first model requires the user to know to run `/diff` — it's not the default path.

**Fix**: Diff-before-write should be the default, not opt-in.

### 4.5 Accumulated Tool Call Noise

Claude Code's default terminal renderer, where every tool call prints its full output inline, creates a transcript that becomes hard to scan after 20+ tool calls. GitHub issue #36462 documents this specifically. 

**Fix**: Tool call outputs should default to a one-line summary (e.g., "Read 3 files, wrote 1 file") that expands on request. The web interface already does this.

### 4.6 Cost Surprises

Cline's real-time cost display prevents the "$50 afternoon" surprise described by multiple users. Without cost visibility, developers in flow state don't notice runaway retry loops. Showing cumulative cost in the status bar — not just at the end — is the right pattern.

**Fix**: Cumulative token count and cost estimate in the status bar, updated on each turn.

### 4.7 Mode Ambiguity

When a user doesn't know whether the agent is in "read-only plan mode" or "full execution mode," they lose trust and start micro-managing. The mode should be visually obvious at all times, not just when a prompt fires.

**Fix**: Mode name in status bar, always visible, with distinct visual styling per mode.

### 4.8 No Recovery Path After Failure

When the agent produces an error, the user should have a clear path to retry with context about what went wrong, not just a raw error message. "The last action failed — here's the error. Want me to try a different approach?" is better than printing a stack trace and returning to the prompt.

### 4.9 Breaking Terminal Conventions

Warp's mouse mode (which captures mouse events for block interaction but breaks native text selection) and Gemini CLI's `Ctrl+S` to exit mouse mode represent cases where AI UX overrides expected terminal behavior. Users who try to select text with the mouse and find it doesn't work as expected experience cognitive dissonance.

**Fix**: Text selection and standard terminal shortcuts should work without mode toggling. If alternate screen buffer is used, provide a clear mechanism for native text selection.

### 4.10 Thinking Without Streaming

Showing only a spinner during extended thinking — no incremental output — is a poor experience when the thinking phase takes 10-30 seconds. Users don't know if the model is making progress or has hung. Claude Code's gray italic streaming of thinking content (when enabled) is significantly better.

---

## 5. Conversation Layout Recommendations

### 5.1 The Three-Zone Model

The most effective terminal AI interfaces use a three-zone vertical stack:
1. **Transcript zone** (top, majority of height): Scrollable conversation history
2. **Status zone** (thin fixed row): Mode, model, context %, cost, task count
3. **Input zone** (bottom, fixed): Input bar with prompt and autocomplete

This is what Claude Code's fullscreen mode, Gemini CLI, Codex CLI, and Crush all converge on. The status zone provides ambient information without consuming transcript space.

### 5.2 Conversation Structure

Each conversation turn should have a clear visual boundary. Recommended structure:

```
[User turn]
> User's prompt text

[Agent turn]  
Agent's response text...

  [Tool call] Read file: src/auth.ts (43 lines)
  [Tool call] Wrote: src/auth.ts (+12 -3)
  
Agent's conclusion text...
─────────────────────────────────────── (turn separator)
```

Tool calls indented within the agent turn (not at the same level as text) visually subordinate them as actions within the response.

### 5.3 Message Types Requiring Distinct Visual Treatment

- **User messages**: Left-aligned, distinct color
- **Agent prose**: Default terminal foreground
- **Tool call summaries**: Indented, muted/dim color, expandable
- **Tool call outputs**: Further indented, secondary color, collapsed by default
- **Diffs**: Red/green with context lines in gray — standard unified diff
- **Errors**: Red, bold, with clear error boundary
- **System messages** (mode changes, session start/end): Gray, italic, centered or prefixed
- **Status messages** (costs, token counts): Gray, right-aligned or in status bar

### 5.4 Collapsibility

Tool call outputs should default to collapsed (one-line summary) and expand on keypress or click. This keeps the transcript scannable during long tasks. The collapse state should persist — if you expand a tool call, it stays expanded until you collapse it.

Claude Code's web interface already implements this; the terminal renderer should follow.

---

## 6. Tool Execution Display Patterns

### 6.1 Pre-Execution Announcement

Before executing a tool, display a one-line announcement:
```
  ⚙ Reading: src/auth.ts
  ⚙ Running: npm test
  ⚙ Writing: src/auth.ts (diff below)
```

This provides real-time feedback without the full tool output cluttering the transcript. The `⚙` icon signals "this is a tool action, not agent prose."

### 6.2 Post-Execution Summary

After tool completion, the announcement should update to reflect the outcome:
```
  ✓ Read: src/auth.ts (312 lines)
  ✓ Ran: npm test — 47 passed, 0 failed (12.3s)
  ✓ Wrote: src/auth.ts (+12 -3 lines)
  ✗ Run failed: npm test — see output below
```

Status icons: `✓` success, `✗` failure, `⚙` in-progress, `⏸` awaiting approval.

### 6.3 Output Collapsing

By default, successful tool outputs collapse to the one-line summary. Failed tool outputs expand automatically to show the error. This puts detail where it matters.

### 6.4 Parallel Tool Execution

When multiple tools run in parallel, show them as a group:
```
  ⚙ Reading (3 files in parallel)
    ⚙ src/auth.ts
    ⚙ src/routes.ts  
    ✓ src/models.ts (89 lines)
```

Agenthicc's parallel DAG executor needs this — show the concurrency visually so users understand why multiple things are happening at once.

---

## 7. Diff Visualization Best Practices

### 7.1 Standard Unified Diff Format

Use the universal unified diff format — developers know it from `git diff`. Do not invent a proprietary diff format.

```diff
--- src/auth.ts (before)
+++ src/auth.ts (after)
@@ -45,7 +45,9 @@
 export function validateToken(token: string): boolean {
-  return token.length > 0;
+  if (!token || token.length === 0) return false;
+  return jwt.verify(token, process.env.JWT_SECRET) !== null;
 }
```

### 7.2 Context Lines

Show 3 context lines above and below each changed block (the git default). More context helps users understand where in the file the change occurs without reading the whole file.

### 7.3 Large Diff Handling

For diffs exceeding ~50 lines, show a summary header ("42 lines changed in 3 functions") with expand-on-request for the full diff. Dumping 200-line diffs inline is overwhelming.

### 7.4 File-Level Summary

When multiple files change, show a file-level summary first:
```
Changes: 3 files
  src/auth.ts       +12 -3
  src/routes.ts     +1  -0
  tests/auth.test.ts +28 -0
```

Then expand individual file diffs on request.

### 7.5 Diff Before Approval

The diff must be visible before the `y/n` approval prompt — not as an optional `d` key. If a user approves without reading, that's acceptable. If they can't read the diff without pressing an extra key, that's a design failure.

### 7.6 Avoid False-Positive Diffs

Claude Code's WSL2 bug (entire new file rendered as green additions, no red removals) destroys trust. The diff must accurately represent what changed. Test diff rendering edge cases: new files, deleted files, binary files, files with no trailing newline.

---

## 8. Streaming Response Patterns

### 8.1 Stable Viewport During Streaming

The most important streaming requirement: the input bar must not move while the agent is responding. Use an alternate screen buffer. Scroll the transcript within a fixed viewport.

### 8.2 Token-by-Token Streaming for Prose

Agent prose (explanatory text, reasoning) should stream token-by-token. This creates the "thinking out loud" feel that makes AI interaction engaging and builds user confidence that the model is progressing.

### 8.3 Thinking / Reasoning Stream

If the model supports extended thinking (like Claude's "thinking" mode), stream the thinking content as dim/gray italic text before the main response. Users who want to skip it can — but the option to observe reasoning should be default-on, not buried behind a toggle.

### 8.4 Buffered Code Blocks

Code blocks should not stream character-by-character — the syntax highlighting flickers and is hard to read mid-stream. Buffer the entire code block and render it when the closing fence (```) is received. This is a common issue in terminal markdown renderers.

### 8.5 Spinner During Non-Streaming Phases

During phases where no streaming is happening (API round-trip, tool execution), show a spinner with a verb ("Reading...", "Analyzing...", "Writing..."). Claude Code's custom verb system ("Flibbertigibbeting...") adds personality but the verb should reflect the actual operation when possible.

### 8.6 Mid-Stream Injection

Codex CLI's ability to press `Enter` mid-stream to inject new instructions is a significant usability feature for long-running generations. The model can pivot based on user feedback before completing the current response. Consider supporting this in AgentHICC.

---

## 9. Failure and Error Handling UX

### 9.1 Error Boundary Clarity

Every error needs a clear visual boundary that separates it from normal output. Red text alone is insufficient — errors get lost in scrollback.

Recommended pattern:
```
╔═ Error ════════════════════════════════════════════╗
║ npm test failed: ModuleNotFoundError              ║
║ Cannot find module './auth' from 'routes.test.ts' ║
╚═══════════════════════════════════════════════════╝
```

Or a simpler inline version:
```
  ✗ Error: npm test failed
  │ ModuleNotFoundError: Cannot find module './auth'
  │ from 'routes.test.ts'
  └─ [View full output] [Retry] [Tell Claude what happened]
```

### 9.2 Agent Self-Recovery Prompts

After a tool failure, the agent should present a structured recovery prompt rather than silently retrying or silently failing:
- "The last command failed. Want me to try a different approach?"
- "I found 3 errors. Should I fix them all, or start with the critical ones?"

The agent knows what failed — surface that knowledge rather than hiding it.

### 9.3 Doom-Loop Detection

If the agent attempts the same tool call with the same arguments 3+ times consecutively, that's a doom loop. Break it explicitly:
```
  ⚠ I've tried this approach 3 times without success.
  I need your help to proceed. Here's what I've tried: [...]
```

This mirrors the doom-loop detection described in the arxiv research. Do not let the agent silently burn tokens in a retry loop.

### 9.4 Budget/Cost Circuit Breaker

When cumulative token usage or cost exceeds a configured threshold, pause and present the user with a clear checkpoint:
```
  ⚠ Session cost: $2.47 (threshold: $2.00)
  Continue? [y] Stop now [n] Raise threshold [r]
```

Cline's real-time cost display is the right direction; adding a configurable circuit breaker makes it actionable.

### 9.5 Graceful Degradation

When the model returns an error (rate limit, context overflow, API error), show:
1. The error type (human-readable, not raw error code)
2. What the agent was doing when it failed
3. Whether the partial work was saved (and where)
4. What the user can do next

Never drop the user at a bare error message with no path forward.

---

## 10. Long-Running Task Experience

### 10.1 Task List as First-Class UI

Claude Code's task list (`Ctrl+T`) — with pending/in-progress/complete indicators — is the right model for multi-step autonomous tasks. The task list should be:
- Visible by default when more than one step is planned
- Automatically hidden when only one step remains
- Persistent across context compactions
- Named with human-readable verbs ("Testing authentication", not "Step 3 of 7")

### 10.2 Background Command Support

Long-running commands (builds, test suites, watchers) must not block the conversational interface. The user should be able to start a build and continue talking to the agent while it runs.

Pattern:
1. Agent starts `npm run build` in background
2. Immediately returns to awaiting user input
3. Status bar shows "npm run build... (running)" 
4. When build completes: "npm run build completed — 0 errors"
5. Agent can reference build output in subsequent responses

### 10.3 Session Recap on Return

Claude Code's session recap — generated after 3+ minutes of inactivity, shown when the user returns — is a thoughtful detail. Long AI sessions often span multiple focus periods. The recap bridges the context gap without the user having to scroll up and re-read.

Minimum recap content:
- What the agent completed
- What's in progress
- What's next
- Any errors or blockers encountered

### 10.4 Progress for Predictable Subtasks

When the agent knows how many steps remain in a plan, show progress:
```
[3 / 7] Updating authentication middleware...
```

This is particularly valuable in architect mode (Aider) or plan mode (Claude Code) where the plan is explicit and enumerable.

### 10.5 Sub-Agent Visibility

AgentHICC's parallel DAG executor can spawn multiple sub-agents. Each sub-agent should be visually distinct (Claude Code uses 8 named colors). The user should be able to see at a glance:
- How many sub-agents are active
- What each is doing
- Which have completed vs. are blocked

---

## 11. Human Approval Workflow Patterns

### 11.1 The Five-Level Approval Hierarchy

Based on surveying all tools in this study, the optimal approval model has five distinct levels:

| Level | Triggers | Example |
|---|---|---|
| **Silent** (never ask) | Read operations, directory listings, non-destructive queries | `ls`, `cat`, `grep`, `git status` |
| **Session-auto** (approved for session) | Low-risk operations approved once by user | `npm test`, `git add`, well-known commands |
| **Diff-then-approve** (default for writes) | File modifications | File edits, new file creation |
| **Explicit** (require y/n each time) | High-risk or irreversible operations | `git push`, `rm`, database writes |
| **Blocked** (never execute) | Dangerous operations | `rm -rf /`, `chmod 777` |

### 11.2 Context-Aware Prompts

The approval prompt should show enough context for an informed decision:
```
Claude wants to write to: src/auth.ts

Summary of changes:
  +12 lines: Added JWT validation with expiry check
  -3 lines: Removed deprecated token format support

[d] View full diff   [y] Accept   [n] Reject   [e] Edit first
```

The summary (auto-generated from the diff) lets users make fast decisions on confident changes without reading the full diff every time.

### 11.3 Batch Approval for Related Changes

Gemini CLI's queued tool confirmations (v0.27.0) implement this. When the agent plans to edit 3 related files as part of one logical change, present all three for review together rather than one at a time. Reviewing a coherent batch of 3 files is faster and more informative than reviewing each file in isolation.

### 11.4 Approval Fatigue Mitigation

Active mitigation measures:
- **Session policy**: After manually approving the same command 3x in a session, offer "Auto-approve this command for the rest of this session"
- **Complexity indicator**: High-complexity diffs (>100 lines) get a brief pause before the prompt appears, discouraging reflexive approval
- **Gate frequency target**: Aim for 10-15 meaningful approval prompts per session (based on research recommendations), not hundreds

### 11.5 Approval Semantics for Sub-Agents

AgentHICC spawns sub-agents. Each sub-agent's actions need approval semantics. Options:
1. **Consolidated approval**: The orchestrator presents a plan; user approves the whole plan; sub-agents execute without further prompts
2. **Per-agent approval**: Each sub-agent's high-risk actions prompt independently
3. **Delegated trust**: Sub-agents inherit the trust level of the spawning agent

Recommendation: Use consolidated plan approval for the initial task, with per-agent blocking for high-risk actions (file writes outside working directory, network access, process execution).

---

## 12. Keyboard Ergonomics

### 12.1 The Essential Bindings

Every terminal AI agent interface needs these bindings to feel complete:

| Action | Recommended Binding | Rationale |
|---|---|---|
| Submit input | `Enter` | Universal |
| New line in input | `Shift+Enter` | Widely supported without config |
| Interrupt/cancel | `Ctrl+C` | Unix convention |
| Exit session | `Ctrl+D` | EOF convention |
| Clear screen | `Ctrl+L` | Universal |
| Approval cycle | `Shift+Tab` | Claude Code / Gemini CLI precedent |
| Scroll transcript up | `Page Up` / `Ctrl+U` | Standard scrollback |
| Background command | `Ctrl+B` | Claude Code precedent |
| Open external editor | `Ctrl+G` / `Ctrl+X Ctrl+E` | readline convention |
| History search | `Ctrl+R` | readline convention |
| Command palette | `/` at input start | Universal in this space |

### 12.2 Vim Mode

Vim mode in the input bar (`hjkl`, visual selection, text objects) is present in Claude Code, Gemini CLI, and Crush. It's a strongly expected feature for terminal-focused developers. It should be:
- Off by default (don't surprise non-vim users)
- Enabled via a simple toggle in `/config`
- Complete enough to be useful: motion, text objects, visual mode

The input bar vim mode is distinct from vim as an external editor. Both should be supported independently.

### 12.3 Chord Bindings and Discovery

`Ctrl+X Ctrl+K` (stop background subagents) is a two-key chord borrowed from readline. Chords are powerful but require discoverability:
- `?` to show shortcut help should be universal
- The help panel should be context-aware (different bindings in transcript viewer vs. input mode)
- Keyboard shortcuts should be listed in the documentation and discoverable without leaving the tool

### 12.4 Shell Mode Passthrough

`!` prefix (Claude Code, Gemini CLI) to run shell commands without going through the AI is a productivity accelerator. The command and its output should be added to conversation context so the agent can reference it, but should not require AI processing for straightforward commands.

### 12.5 File Path Autocomplete

`@` file path autocomplete (Claude Code, Codex CLI) is expected by users who work with large codebases. The autocomplete should:
- Fuzzy-search, not prefix-match
- Show file previews on hover
- Support the full context injection vocabulary when applicable (`@Codebase`, `@File`, etc.)

---

## 13. What the Best Tools Do Right

### Claude Code
- **The permission mode system with visible status bar** is the gold standard for approval UX
- **`/btw` ephemeral side questions** is a novel and valuable primitive
- **Fullscreen mode with mouse support** shows the right architectural direction
- **Session recap on return** is a thoughtful quality-of-life feature
- **Background subagent colors** (8 named colors for parallel agents) is the right way to visualize concurrency

### Gemini CLI
- **Alternate screen buffer by default** — doesn't require opt-in
- **Persistent "Always Allow" policies** across sessions reduce repetitive approval for trusted commands
- **Batch tool confirmation queue** for reviewing multiple pending actions at once
- **PTY-based interactive shell** (run vim, htop inside the CLI) is genuinely powerful
- **Dynamic window title** for OS-level status without taking terminal space

### Codex CLI (OpenAI)
- **Mid-stream input injection** (`Enter` during generation) enables real-time redirection
- **Dual-layer security** (sandbox policy + approval policy) is a principled model
- **Draft history navigation** with Up/Down in the composer

### Aider
- **Git-first philosophy** makes undo trustworthy and auditable
- **Architect mode two-model pipeline** separates reasoning from execution
- **Explicit file context management** (`/add`, `/drop`) is transparent and predictable
- **Configurable colors per output type** lets power users tune the visual experience

### GitHub Copilot CLI
- **GitHub context integration** (issues, PRs in tabs) reduces context switching
- **Rubber duck second-opinion agent** is an architecturally novel self-review primitive
- **Accessibility-first design** (colorblind mode, screen reader, responsive)

### Cline
- **Real-time cost tracking** is the most transparent cost UX in the survey
- **Per-action checkpoint rollbacks** — the strongest undo story

### Warp Terminal
- **Blocks architecture** makes terminal output structured and queryable
- **Proactive inline diff** on detected errors without being asked

---

## 14. Synthesis and Recommendations for AgentHICC

AgentHICC already uses `prompt_toolkit` HSplit with a pinned input bar — a solid foundation that avoids the jumping input bar anti-pattern. The following recommendations are grounded in the research above and specific to AgentHICC's architecture (event-sourced kernel, parallel DAG executor, lifecycle hooks, 3-tier memory).

### 14.1 Immediate Wins (Low Effort, High Impact)

**A. Always-visible mode indicator in status bar**

The bottom status bar should always show the current permission mode with color coding:
- `default` → yellow
- `plan` (read-only) → blue
- `auto` → green
- `bypassPermissions` → red

`Shift+Tab` to cycle. Users should never wonder what mode they're in.

**B. Collapsible tool call outputs**

Tool calls should default to one-line summaries in the transcript:
```
  ✓ Read: src/auth.ts (312 lines)
  ⚙ Running: npm test...
```

With a keypress (e.g., `Ctrl+E` or `Enter` on the summary line) to expand the full output. This directly addresses the transcript clutter problem.

**C. Diff before write, always**

File writes should show the unified diff before the approval prompt. No extra keypress required. This is table stakes for user trust.

**D. Doom-loop detection**

If the same tool call with the same arguments fires 3 times in sequence, break the loop and surface the failure to the user with a clear prompt for guidance.

### 14.2 AgentHICC-Specific Patterns

**E. Parallel sub-agent visualization**

AgentHICC's DAG executor runs tasks concurrently. Display concurrent sub-agents as a named group in the transcript:

```
[Parallel execution: 3 agents]
  ⚙ agent-red:   Analyzing src/auth.ts
  ✓ agent-blue:  Read tests/auth.test.ts (47 tests found)
  ⚙ agent-green: Running: npm test
```

Eight named colors (one per agent color slot) as Claude Code does. The color should be consistent within a session: agent-red is always red.

**F. Event-sourced transcript viewer**

AgentHICC's append-only event log is a perfect backing store for a transcript viewer. Implement a `Ctrl+O` transcript mode that:
- Shows the full event history for the session
- Supports search by event type, tool name, file path
- Allows jumping to any point in history
- Can dump to a file for external review

**G. Lifecycle hook visibility**

When lifecycle hooks fire (`on_before`, `on_after`, `on_error` at intent/workflow/task/agent/tool-call granularity), show them in the transcript:
```
  🔗 Hook: intent.on_before → validate_intent [42ms]
  🔗 Hook: task.on_after → update_metrics [8ms]
```

Collapsed by default. Developers building hooks need to see them firing; end users don't.

**H. 3-tier memory status in status bar**

Show active memory usage:
```
| session:12 | project:47 | global:3 |
```

These counts (items in each tier) give developers working with AgentHICC's memory system quick feedback. Clickable to open a memory browser.

**I. DAG progress visualization**

When a workflow DAG is executing, show progress as a compact task list:
```
[Workflow: add-authentication — 4/7 nodes complete]
  ✓ design-schema
  ✓ implement-models
  ✓ write-routes        
  ✓ add-middleware
  ⚙ write-tests          ← running
  ○ update-documentation ← blocked (waiting for write-tests)
  ○ run-integration      ← ready
```

The `←` annotations show blocked vs. ready state. This is the "parallel DAG" equivalent of Claude Code's task list.

### 14.3 Streaming Architecture for Tool Calls

Cline's generative streaming UI model is worth studying: tool call UI components are generated on the fly from XML-tagged delimiters in the model output stream, not post-processed from a completed response. This creates the "alive during generation" feel.

For AgentHICC, the equivalent is: when the LLM response stream contains a tool call (`use_mcp_tool`, `bash`, etc.), the TUI should begin rendering the tool call UI before the tool parameters are fully received. Specifically:

1. **Name first**: Show the tool name as soon as it appears in the stream (e.g., "Reading:" with a spinner)
2. **Parameters as they arrive**: Show parameters incrementally as they stream (Crush does not do this yet — it's an open issue; this would be a differentiator)
3. **Switch to result**: When the tool completes, replace the spinner with the result summary

This requires the transcript renderer to parse the LLM output stream character-by-character rather than waiting for complete XML tags. The prompt_toolkit text area can be updated incrementally — AgentHICC should pipe LLM stream chunks into the transcript viewport in real time.

### 14.5 Design Decisions for the Approval Workflow

The event-sourced kernel means every approval decision is itself an event — log it. Recommendations:

1. **Plan mode as default for new sessions**: Start in plan mode (read-only, proposes without executing). Users consciously escalate to execution mode. This mirrors the research finding that "plan then execute" reduces errors and builds trust.

2. **Session-scoped auto-approval via `CommunicationTools.hook_register`**: When a user approves a tool call pattern, register a `LifecycleHook` that auto-approves matching calls for the session. This makes the approval learning visible and reversible.

3. **Consolidated plan approval**: For workflow intents, the `IntentPlanner` output should be presented as a reviewable plan before any execution begins. Users approve the plan; individual tool calls within approved workflow nodes don't require additional prompts unless they exceed their planned scope.

4. **Hard blocks via `SecurityPolicy`**: AgentHICC's `SecurityPolicy` (allow/deny/require_confirmation rules) should enforce hard blocks at the kernel level — not just at the UI level. This is the "defense-in-depth" model from the OpenDev research.

### 14.6 Streaming Implementation

Given that AgentHICC uses `prompt_toolkit` with an HSplit layout:

1. Keep the input bar at the last terminal row (already implemented per CLAUDE.md)
2. Stream agent text into the transcript viewport — do not buffer full turns
3. Implement the three-phase streaming model:
   - Phase 1 (thinking): Gray dim text, streaming as it arrives
   - Phase 2 (prose): Normal foreground text, streaming
   - Phase 3 (tool calls): Indented, icon-prefixed, one-line-per-call
4. Buffer complete code blocks before rendering to avoid mid-render flicker
5. Auto-scroll the transcript viewport as new content arrives ("follow mode")
6. Allow scroll-up to review history without interrupting streaming ("anchor mode" when user scrolls up)

### 14.7 Error Handling Display

Leverage AgentHICC's lifecycle hooks for error display:

- `LifecycleHook.on_error` at the tool-call level should surface errors with full context
- Error display should include: tool name, arguments that caused the error, the error message, and suggested recovery actions
- Errors from sub-agents should be attributed to the specific sub-agent (not a generic "agent error")
- `Effect` objects in the reducer can trigger UI notifications for errors that occur asynchronously (background tasks, tool timeouts)

### 14.8 The Keyboard Map

Recommended keyboard bindings for AgentHICC TUI:

| Key | Action |
|---|---|
| `Enter` | Submit input |
| `Shift+Enter` | New line in input |
| `Ctrl+C` | Interrupt / cancel |
| `Ctrl+D` | Exit session |
| `Ctrl+L` | Redraw screen |
| `Shift+Tab` | Cycle permission mode |
| `Ctrl+O` | Toggle transcript viewer |
| `Ctrl+T` | Toggle task list / DAG progress |
| `Ctrl+B` | Background running tool call |
| `Ctrl+R` | History search |
| `Ctrl+G` | Open external editor |
| `Ctrl+X Ctrl+K` | Stop all sub-agents |
| `Alt+T` | Toggle extended thinking display |
| `Alt+P` | Switch model |
| `!` prefix | Shell passthrough mode |
| `@` | File path autocomplete |
| `/` | Command palette |
| `/btw` | Ephemeral side question |
| `Esc` | Interrupt agent mid-turn |
| `Esc` + `Esc` | Rewind/checkpoint menu |
| `?` in transcript | Shortcut help panel |

Vim mode available via `/config`, off by default.

---

## Sources

- [Terminal Is All You Need: Design Properties for Human-AI Agent Collaboration](https://arxiv.org/html/2603.10664v1) — arxiv.org
- [Building Effective AI Coding Agents for the Terminal](https://arxiv.org/html/2603.05344v2) — arxiv.org
- [Claude Code Interactive Mode Reference](https://code.claude.com/docs/en/interactive-mode) — Anthropic
- [Claude Code Fullscreen Rendering](https://code.claude.com/docs/en/fullscreen) — Anthropic
- [Claude Code Permission Modes](https://code.claude.com/docs/en/permission-modes) — Anthropic
- [Gemini CLI Tips and Tricks](https://addyosmani.com/blog/gemini-cli/) — Addy Osmani
- [Gemini CLI Changelog](https://geminicli.com/docs/changelogs/) — geminicli.com
- [OpenAI Codex CLI Features](https://developers.openai.com/codex/cli/features) — OpenAI
- [OpenAI Codex Agent Approvals and Security](https://developers.openai.com/codex/agent-approvals-security) — OpenAI
- [Aider Chat Modes Documentation](https://aider.chat/docs/usage/modes.html) — aider.chat
- [Aider Review: A Developer's Month](https://www.blott.com/blog/post/aider-review-a-developers-month-with-this-terminal-based-code-assistant) — Blott
- [Crush TUI Architecture](https://deepwiki.com/charmbracelet/crush/5.1-tui-architecture-and-appmodel) — DeepWiki
- [OpenCode GitHub Repository](https://github.com/opencode-ai/opencode) — GitHub
- [Continue.dev Diff Management](https://deepwiki.com/continuedev/continue/6.8-diff-management) — DeepWiki
- [Continue.dev Edit Mode](https://docs.continue.dev/edit/how-it-works) — Continue.dev
- [Cline VS Code Extension Review](https://blog.vibecoder.me/cline-ai-pair-programming-vs-code) — VibeCoder
- [GitHub Copilot CLI Improved UI](https://github.blog/changelog/2026-06-02-copilot-cli-improved-ui-rubber-duck-prompt-scheduling-and-voice-input/) — GitHub Blog
- [Warp Terminal Guide 2026](https://aiproductivity.ai/guides/warp-terminal-guide/) — AI Productivity
- [Warp Block Architecture](https://starlog.is/articles/ai-agents/warpdotdev-warp) — Starlog
- [Agent UX Patterns: Chat-First UX Fails](https://hatchworks.com/blog/ai-agents/agent-ux-patterns/) — Hatchworks
- [Approval Fatigue Encyclopedia](https://aipatternbook.com/approval-fatigue) — AI Pattern Book
- [Claude Code Hidden Commands](https://marmelab.com/blog/2026/05/12/claude-code-hidden-commands.html) — Marmelab
- [Getting More Out of Claude Code](https://www.datacamp.com/tutorial/claude-code-terminal) — DataCamp
- [Gemini CLI vs Claude Code Comparison](https://www.datacamp.com/blog/gemini-cli-vs-claude-code) — DataCamp
- [Claude Code Review and Analysis](https://thediscourse.co/p/claude-code) — The Discourse
- [Warp Agentic CLI Workflows Guide](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) — Digital Applied
- [Agentic Design Patterns — UI/UX](https://agentic-design.ai/patterns/ui-ux-patterns) — Agentic Design
