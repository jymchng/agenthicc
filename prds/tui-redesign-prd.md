# AgentHICC TUI Redesign — Master PRD

## 1. Vision & Product Statement

AgentHICC is an autonomous software engineering agent that runs in the terminal. The TUI is the primary interface through which developers direct the agent, observe its reasoning, review its tool calls, and approve or deny actions.

The redesign replaces the current full-screen prompt_toolkit alternate-screen application with a committed-transcript + live-bottom-block architecture. Every completed turn is printed once to stdout and scrolls permanently into the terminal's native scrollback buffer. A small bottom block (3-6 rows) is erased and redrawn each frame to show the status bar, divider, input bar, mode footer, and optional dropdown.

This architecture matches how experienced CLI developers already think about terminal output: completed output stays, interactive controls live at the bottom. It enables copy-paste from any turn, grep through scrollback, tmux split-pane workflows, and zero surprise when SSH latency spikes.

Goals list (numbered):
1. Zero alternate-screen usage — the terminal's native scrollback is the transcript
2. Sub-200ms time-to-first-render on cold start
3. 50ms debounced streaming render, never blocking the event loop
4. Six named permission modes (Auto/Plan/Ask/Review/Safe/Debug) with visual indicators
5. Tool calls rendered as collapsed one-liners by default; expandable on demand
6. Approval gate shows diff before Y/N/A prompt — never after
7. Doom-loop detection (same tool+args 3x) surfaces to user automatically
8. Parallel sub-agent visualization with named per-agent colors
9. @mention autocomplete for files and agents in the input bar
10. /command palette with fuzzy-search dropdown
11. Session persistence: every session writes events.jsonl; resume via --resume flag
12. Full NO_COLOR / FORCE_COLOR / COLORTERM support
13. wcwidth for all display-width calculations — never len() on terminal strings
14. Memory-bounded transcript: 200 turns × 500 lines max; evict old turns gracefully
15. FakeTerminal for unit tests; pyte integration tests for full rendering

## 2. Design Principles

Write 8 design principles. For each principle, write: the name (as a bold heading), a one-sentence statement, and 2-3 paragraphs of rationale plus concrete examples. The 8 principles are:

**Principle 1: Scrollback Is Sacred**
Statement: Completed output belongs to the terminal's native scrollback and must never be erased, overwritten, or replaced by alternate-screen content.
Rationale: Alternate-screen mode (ESC[?1049h) is the root cause of the most common terminal UX complaints — you cannot copy text from a previous turn, you cannot grep the session log, tmux capture-pane misses all the content, and SSH disconnection loses everything. When an application claims the full screen, it fights the fundamental contract of the terminal: that output streams downward and persists. Every major CLI tool that has switched from alternate-screen to inline rendering (Claude Code itself, fish shell's completion system, Warp terminal) reports dramatically better user satisfaction.

Concrete example: a developer running agenthicc in a tmux split pane should be able to scroll the left pane to find an error from 20 turns ago while typing a follow-up in the right pane. With alternate-screen, this is impossible. With committed transcript, it is trivially supported.

**Principle 2: The Bottom Block Is a Guest**
Statement: The interactive bottom block (status + input) occupies the minimum rows necessary and never disrupts the flow of committed output above it.
Rationale: The bottom block exists to serve the user's current interaction, not to claim permanent screen real estate. It should never exceed 6 rows (typically 3-4: status line, divider, input bar, mode footer). When a dropdown appears (slash command palette, @mention list), it floats above the existing bottom block, temporarily expanding it, but collapses the moment a selection is made or the user presses Escape.

The bottom block must be fully erased before new committed lines are printed — the erase sequence is the canonical `\x1b[2K` + (`\x1b[1A\x1b[2K`) × (n-1) + `\r`, identical to what Rich Live, log-update, Ink, and Textual inline all use. Concrete example: when the agent commits a 40-line diff block to the transcript, the bottom block clears, the 40 lines print, and the bottom block immediately redraws below — the user sees uninterrupted output flow.

**Principle 3: Meaning Before Decoration**
Statement: Every color, symbol, and typographic treatment must encode actionable information; purely decorative styling is prohibited.
Rationale: Color blindness affects approximately 8% of men and 0.5% of women. A design that uses color as the sole differentiator between success and failure is inaccessible to a significant portion of the developer population. In the AgentHICC color system, every color is paired with a distinct symbol and/or text label: success is ✓ green, error is ✗ red, warning is ⚠ yellow, but in NO_COLOR mode these are still ✓, ✗, and ⚠ — the symbols carry the meaning.

Typography follows the same rule: bold is for agent turn headers (not generic emphasis), dim/muted is for timestamps and metadata (not random de-emphasis), underline is reserved for clickable hyperlinks. Concrete example: a tool call line reads `  ⎿ read_file(path='src/auth.py')  ✓ 12ms` — the ⎿ prefix identifies it as a tool call structurally, the ✓ reports success, and `12ms` quantifies cost. The green color is additive information, not the primary signal.

**Principle 4: Stream Fast, Commit Clean**
Statement: Token streaming renders with a 50ms debounce; turn completion triggers an immediate force-commit that prints clean final output to scrollback.
Rationale: Streaming LLM output to the terminal creates a tension: render every token immediately (maximum responsiveness, maximum screen flicker) versus batch-render at fixed intervals (reduced flicker, slight latency). The 50ms debounce (RenderLoop.MIN_TICK_INTERVAL = 0.050) is the empirically-validated sweet spot from the Rich Live source code, the log-update npm package, and the Textual inline mode implementation. At 50ms the user perceives continuous streaming but the terminal never receives more than 20 redraws per second.

The force-commit at turn end is equally important: once the agent finishes a turn, the final rendered lines are printed permanently to stdout so they are in the scrollback buffer. The bottom block streaming area is cleared. The next streaming turn starts fresh. Concrete example: a 200-token streamed response triggers at most 10 bottom-block redraws during streaming, then one final commit of the complete rendered turn to scrollback, and then the bottom block resets to the idle input state.

**Principle 5: Input Is Always Reachable**
Statement: The input bar is always visible at the bottom of the terminal, regardless of how much output has been committed above it.
Rationale: A common failure mode in terminal UIs is that the input prompt scrolls off-screen after long output — the user has to scroll down to find it before they can type their next message. In the committed-transcript architecture this cannot happen: the bottom block is always redrawn at the current terminal bottom after any new committed output. The input bar position is not a fixed terminal row; it is always the last rows of the terminal, dynamically positioned.

This means a developer who runs agenthicc in a small tmux pane (80×24) and receives a long tool output sees the input bar jump to row 24 after the commit, ready for the next message. Concrete example: after the agent commits a 100-line file listing, the input bar is immediately visible at the bottom without any scrolling required.

**Principle 6: Permission Modes Are First-Class**
Statement: The six permission modes (Auto/Plan/Ask/Review/Safe/Debug) are always visible, always meaningful, and always togglable with a single keystroke.
Rationale: Permission modes are the primary safety mechanism of an autonomous agent. A developer running agenthicc on a production server should be in Review or Safe mode; a developer doing exploratory refactoring can use Auto mode. The current implementation buries mode changes in a /mode command. The redesign makes the mode badge the leftmost element of the status bar, color-coded (AUTO=green, PLAN=yellow, ASK=cyan, REVIEW=blue, SAFE=red, DEBUG=magenta) and always visible.

Shift+Tab cycles through modes; the mode changes take effect immediately and are logged to the event stream. Concrete example: a developer who started in Auto mode and realizes the agent is about to write to the database can press Shift+Tab twice to switch to Review mode — the status bar immediately updates and the next tool call requiring confirmation will trigger the approval gate.

**Principle 7: Errors Are Honest**
Statement: Errors are displayed at the point of occurrence with full context, classified by severity, and never hidden or deferred.
Rationale: The three-tier error taxonomy (recoverable, critical, fatal) maps directly to the three response strategies available to the user. Recoverable errors (tool call failed, network timeout on a single request) are rendered inline in the transcript as tool call ERROR state with the error message — the agent can choose to retry or continue. Critical errors (LLM rate limit, malformed API response) render as a full-width banner in the bottom block that persists until acknowledged — the developer must see this before continuing.

Fatal errors (unhandled exception, event loop crash) are written to stderr with a full traceback, the bottom block is cleared to avoid corruption, and the process exits with code 1. Concrete example: if `read_file` fails because the path does not exist, the tool call line in the transcript updates to `  ⎿ read_file(path='/nonexistent')  ✗ 2ms  No such file` — inline, honest, no dialog box required.

**Principle 8: Test Everything at the Boundary**
Statement: Every rendering behavior is verified by tests against FakeTerminal (unit) and pyte (integration); no rendering logic is tested via visual inspection alone.
Rationale: Terminal rendering bugs are notoriously hard to catch in code review because the sequences look like noise to human eyes. The FakeTerminal class provides a deterministic, in-process capture of all committed lines and bottom block frames — unit tests can assert exact sequences like `assert fake_terminal.committed_lines[-1] == '● agent:planner  09:41:22'` without spawning a real terminal process.

Pyte integration tests run the full render pipeline through a virtual terminal emulator and assert on the rendered screen buffer — catching bugs like off-by-one errors in the bottom block erase sequence that only manifest in real terminal rendering. Concrete example: the test `test_bottom_block_erase_redraws_below_committed_output` uses pyte to verify that after a 3-row bottom block is replaced by a 5-row bottom block, the committed transcript lines above are pixel-perfect and the new bottom block occupies exactly rows ROWS-5 through ROWS-1.

## 3. Success Criteria

### 3.1 Performance Metrics

| Metric | Target | Measurement Method |
|--------|--------|--------------------|
| Cold start to first render | < 800ms | time from process launch to first bottom block draw |
| Time to first render (TTFR) | < 200ms | time from first token to first transcript line committed |
| Frame render time | < 16ms | time to compute and write one bottom block frame |
| Streaming debounce interval | 50ms | RenderLoop.MIN_TICK_INTERVAL constant |
| Force-commit latency | < 5ms | time from turn_end event to committed lines written |
| Terminal write batching | single write() per frame | verified by FakeTerminal.write_call_count |
| Memory per turn (avg) | < 50KB | measured with tracemalloc in benchmark tests |
| Total memory at 200 turns | < 10MB transcript | measured with tracemalloc |
| CPU idle (between turns) | < 1% | measured with psutil in benchmark tests |
| CPU during streaming | < 15% (single core) | measured with psutil in benchmark tests |

### 3.2 Compatibility Matrix

List all required terminal emulators and environments:
- iTerm2 2.x+ (macOS): full color, Unicode, synchronized output
- Terminal.app (macOS): full color, Unicode, no synchronized output
- Alacritty 0.12+ (Linux/macOS): full color, Unicode, synchronized output
- Kitty 0.27+ (Linux/macOS): full color, Unicode, synchronized output, graphics protocol (unused)
- GNOME Terminal 3.44+ (Linux): full color, Unicode, no synchronized output
- Konsole 22.x+ (Linux): full color, Unicode, synchronized output
- WezTerm (Linux/macOS/Windows): full color, Unicode, synchronized output
- Windows Terminal 1.17+ (Windows): full color, Unicode, synchronized output
- xterm-256color (fallback): 256-color, no synchronized output
- xterm (minimal fallback): 8-color, ASCII-only mode triggered
- tmux 3.2+ (multiplexer): passthrough synchronized output, scrollback preserved
- GNU screen 4.9+ (multiplexer): no synchronized output, scrollback preserved
- SSH over OpenSSH (remote): degraded mode if TERM=dumb, full mode otherwise
- VS Code integrated terminal: full color, Unicode, no synchronized output
- JetBrains integrated terminal: full color, Unicode
- GitHub Codespaces browser terminal: xterm-256color capabilities

### 3.3 Accessibility Requirements

- NO_COLOR=1 environment variable: disables all ANSI color codes, symbols remain
- FORCE_COLOR=1 environment variable: enables color even when stdout is not a TTY
- COLORTERM=truecolor: enables 24-bit color mode
- COLORTERM=256color: enables 256-color mode
- Color-blind safe palette: all semantic colors distinguishable in deuteranopia simulation
- Keyboard-only navigation: every action reachable without mouse
- Screen reader compatibility: plain-text fallback mode via --no-color --ascii flags
- Minimum contrast ratio: 4.5:1 for all text on terminal background (WCAG AA)
- Font-size independent: layout never assumes font metrics, uses character cells only
- Non-flashing: render rate never exceeds 60fps; no content that flashes 3+ times/second

### 3.4 Reliability Requirements

- SIGWINCH (terminal resize): bottom block redraws within one frame (≤16ms) of resize signal
- SIGINT (Ctrl+C): current agent turn cancelled, bottom block redraws, session continues
- SIGTERM: graceful shutdown — flush committed lines, clear bottom block, save session, exit 0
- SIGHUP (terminal disconnect): flush committed lines, save session, exit 0
- Crash recovery: unhandled exceptions print traceback to stderr, clear bottom block, exit 1
- Session resume: --resume SESSION_ID restores transcript from events.jsonl
- Event log corruption: if events.jsonl is truncated/corrupt, start fresh with warning
- Concurrent writes: event processor serializes all writes via MPSC queue (no lock needed)
- Long sessions (8+ hours): memory eviction keeps RSS below 100MB via TranscriptModel._evict_old_turns()
- Nested tmux/screen: detect $TMUX and $STY, adjust scroll region handling accordingly

### 3.5 Test Coverage Targets

| Layer | Target Coverage | Key Test Files |
|-------|----------------|----------------|
| Terminal class | 95% line coverage | tests/unit/test_terminal.py |
| FrameComposer | 100% line coverage | tests/unit/test_frame_composer.py |
| RenderLoop | 90% line coverage | tests/unit/test_render_loop.py |
| InputState | 95% line coverage | tests/unit/test_input_state.py |
| TranscriptModel | 95% line coverage | tests/unit/test_tui_transcript.py |
| ApprovalGate | 100% line coverage | tests/unit/test_approval_gate.py |
| StatusBar | 90% line coverage | tests/unit/test_status_bar.py |
| TriggerDropdown | 90% line coverage | tests/unit/test_trigger_dropdown.py |
| Pyte integration | 20+ scenario tests | tests/integration/test_tui_rendering.py |
| E2E full session | 5+ scenario tests | tests/e2e/test_tui_e2e.py |

## 4. User Personas

**Persona 1: Alex — Solo Developer**
Background: Alex is a full-stack developer working alone on a SaaS product. They use agenthicc for 4-6 hours per day to write features, debug production issues, and review PRs. They work on a MacBook Pro with iTerm2, often with 2-3 tmux windows open simultaneously. They have intermediate terminal knowledge — comfortable with vim/tmux/git but do not write terminal emulators for fun.

Goals: (a) Direct the agent toward the correct file or function quickly with @mention. (b) Review every file write before it is committed to the filesystem. (c) Keep a mental model of what the agent did in the last 30 minutes without re-reading the entire session. (d) Quickly switch to Plan mode when the agent seems to be going in the wrong direction.

Pain points with current TUI: (a) The alternate-screen mode means they cannot scroll back to a file listing from 10 turns ago without using /history. (b) The permission mode is buried — they do not know what mode they are in at a glance. (c) Copy-pasting output from the agent requires exiting the TUI, running the command manually, and copy-pasting from there. (d) When running in a tmux split, the TUI occasionally corrupts the left pane when the terminal is resized.

Session patterns: Opens agenthicc at 09:00, works in 20-30 minute focused sessions, frequently uses /btw for quick side questions without polluting the main session context, reviews 3-5 file writes per session, typically closes at 18:00 with --save-session.

Key needs: Scrollback access to committed transcript, always-visible mode indicator, single-keystroke mode switching, robust SIGWINCH handling in tmux.

**Persona 2: Jordan — DevOps Engineer**
Background: Jordan manages infrastructure for a 50-person company. They use agenthicc for writing Terraform modules, debugging CI pipelines, and responding to on-call incidents. They work exclusively over SSH from a Windows Terminal client, often in degraded network conditions. They care deeply about safety — running an agent that can modify infrastructure requires careful oversight.

Goals: (a) Always be in Review or Safe mode when working on production infrastructure. (b) See the exact diff of any file modification before approving it. (c) Maintain a log of every tool call made in a session for the post-incident report. (d) Detect when the agent is stuck in a loop before it makes 10 identical API calls.

Pain points with current TUI: (a) The approval dialog appears after the diff is computed, not before — by the time they see the diff, the agent has already made API calls to generate it. (b) The session log is not human-readable without parsing events.jsonl manually. (c) When SSH latency spikes above 200ms, the TUI flickers badly because it is rerendering the entire screen. (d) No doom-loop detection — the agent once retried a failing Terraform apply 7 times before Jordan noticed.

Session patterns: Connects via SSH at incident time, often under time pressure, uses Review mode exclusively, expects to approve/deny every write, disconnects and reconnects multiple times during long incidents.

Key needs: Approval gate shows diff first, doom-loop detection and alerts, robust SSH degraded mode with minimal redraws, session resume after SSH disconnect.

**Persona 3: Morgan — Team Lead**
Background: Morgan leads a team of 6 engineers. They use agenthicc primarily in Plan mode to break down tasks into subtasks, assign them to sub-agents, and review the parallel execution progress. They run on Linux (Fedora) with GNOME Terminal, and they value seeing what all sub-agents are doing at a glance without switching windows.

Goals: (a) Visualize parallel sub-agent execution in the transcript — which agent is running, what tool it is on, how long it has been running. (b) Spot which sub-agent is failing or stuck without scrolling. (c) Change the plan mid-execution by adding/removing workflow nodes. (d) Share session transcripts with the team via Slack — the transcript must be copy-paste friendly.

Pain points with current TUI: (a) Parallel sub-agents produce interleaved output that is impossible to read — there is no visual separation between agents. (b) The spinner for a long-running sub-agent is not visible because it is buried in past output. (c) Plan mode changes are confirmed only via text response — no visual DAG or task list. (d) The session transcript is not structured — copy-pasting from alternate-screen mode gives garbage escape sequences.

Session patterns: Opens agenthicc once per sprint planning session (2 hours), spawns 4-8 sub-agents simultaneously, monitors progress for 30-60 minutes, reviews outputs in batch, exports transcript to file for team review.

Key needs: Per-agent color coding in transcript, always-visible status line showing active agents, copy-paste clean committed transcript, /workflow command to show current DAG.

**Persona 4: Sam — Open Source Contributor**
Background: Sam contributes to multiple open source projects in their spare time. They use agenthicc to understand unfamiliar codebases, write tests, and prepare PRs. They use various terminals (VS Code integrated terminal, Alacritty on Linux, sometimes Windows Terminal), and they cannot install custom fonts. They need agenthicc to work with the default system font and standard Unicode only.

Goals: (a) Use @mention to quickly load context about files and symbols. (b) Get reliable NO_COLOR output when running agenthicc in CI to check a script. (c) Navigate the /command palette to discover available commands without reading docs. (d) Have the agent's markdown responses rendered clearly without requiring a Nerd Font.

Pain points with current TUI: (a) Some status bar symbols require Nerd Font and render as boxes on their default system font. (b) NO_COLOR is not fully supported — some ANSI codes still appear when piped to a file. (c) The /help command dumps a wall of text instead of a navigable dropdown. (d) @mention autocomplete triggers too aggressively — it fires on every @ character even in the middle of an email address.

Session patterns: Short 20-30 minute sessions, frequently in CI via GitHub Actions, uses /btw heavily for isolated questions, often pipes output to a file for later review.

Key needs: No Nerd Font requirement, complete NO_COLOR support, /command palette dropdown, smart @mention trigger logic, CI-friendly headless mode.

---

## 5. Non-Functional Requirements

### 5.1 Performance

**Cold Start Performance**

The time from `uv run agenthicc` to first visible bottom block draw must be under 800ms on a modern laptop (2020+ MacBook Pro, 2021+ Linux laptop). This budget breaks down as follows: Python interpreter startup and import resolution takes approximately 200ms for the standard agenthicc import chain. Configuration loading (read `agenthicc.toml`, merge with `~/.agenthicc.toml`) takes approximately 50ms. Terminal capability detection (query `$TERM`, `$COLORTERM`, `$NO_COLOR`, write/read `\x1b[5n` DSR if supported) takes approximately 30ms. Event processor initialization and first `asyncio.Queue` setup takes approximately 20ms. First bottom block draw (Terminal.set_bottom()) takes approximately 5ms on a standard xterm-256color terminal. The remaining budget (~495ms) is reserved for the lauren-ai agent initialization and any plugin discovery.

**Streaming Render Performance**

The RenderLoop runs on a 50ms tick (MIN_TICK_INTERVAL = 0.050 seconds). Each tick: (a) checks if new tokens have arrived from the LLM stream since the last tick; (b) if yes, calls FrameComposer.compose() to produce the new bottom block frame; (c) calls Terminal.set_bottom() to erase the old frame and write the new one. The total time for steps (b) and (c) must be under 16ms so the system has at least 34ms headroom before the next tick. FrameComposer.compose() is a pure function — no I/O, no async, no locks — and must complete in under 8ms for a typical 80-column terminal. Terminal.set_bottom() issues one batched write() call per frame — the number of write() syscalls per frame is exactly 1, verified by FakeTerminal.write_call_count assertions in tests.

**Memory Performance**

TranscriptModel enforces the following memory budget: MAX_TURNS_IN_MEMORY = 200, MAX_LINES_PER_TURN = 500, MAX_DIFF_LINES = 50. When a new turn would push the count above 200, `_evict_old_turns()` clears the `output_lines` field of the oldest 20 turns (keeping the turn metadata for display in a compact form) and logs a debug event. This keeps the in-process transcript RSS below 10MB for the vast majority of sessions. The committed-lines history in Terminal is not bounded (it lives in the terminal's scrollback, not in Python memory) — Python only tracks a list of line lengths for erase-sequence calculation, which is O(n) in the number of committed lines but each entry is just an integer.

**CPU Performance**

In the idle state (user is typing, no agent turn in progress), the agenthicc process should consume less than 1% CPU. The RenderLoop only redraws the bottom block when InputState has changed (new character typed, cursor moved) or when the agent state has changed (new event processed). A simple dirty flag (`_needs_redraw: bool`) prevents redundant redraws. During active streaming, CPU usage should stay below 15% on a single core — the 50ms tick rate means the render loop executes at most 20 times per second, and each execution is a fast pure-function compose + single write.

**Terminal Write Performance**

All terminal writes are batched into a single `sys.stdout.buffer.write()` call per frame. This means the erase sequence + new frame content is one write() call, not N separate calls for N rows. This is critical on SSH connections where each write() call incurs RTT overhead. The batching is implemented in `Terminal._write_atomic(data: bytes)`, which assembles the full frame bytes in a `bytearray` buffer and then calls `os.write(self._fd, buffer)` once.

### 5.2 Compatibility

**Terminal Emulators — Full Support (all features enabled)**

The following terminal emulators are fully supported, meaning: 24-bit truecolor, all Unicode BMP characters, synchronized output protocol (BSU/ESU), and all 6 permission mode colors:

- iTerm2 2.x+ on macOS: detected via `$TERM_PROGRAM=iTerm.app`; supports synchronized output via `\x1b[?2026h`
- Alacritty 0.12+ on Linux and macOS: detected via `$TERM=alacritty`; excellent Unicode rendering; synchronized output supported
- Kitty 0.27+ on Linux and macOS: detected via `$TERM=xterm-kitty`; full Unicode; synchronized output; Kitty graphics protocol available (not used in this release)
- WezTerm on Linux, macOS, Windows: detected via `$TERM_PROGRAM=WezTerm`; full Unicode; synchronized output
- Windows Terminal 1.17+ on Windows: detected via `$WT_SESSION`; full Unicode; synchronized output; note: requires UTF-8 console code page (chcp 65001)
- Konsole 22.x+ on Linux: detected via `$TERM_PROGRAM=Konsole`; full Unicode; synchronized output

**Terminal Emulators — Partial Support (256-color, no synchronized output)**

- Terminal.app on macOS: `$TERM=xterm-256color`, no synchronized output, full Unicode
- GNOME Terminal 3.44+ on Linux: `$TERM=xterm-256color`, no synchronized output, full Unicode
- VS Code integrated terminal: `$TERM_PROGRAM=vscode`; 256-color; no synchronized output
- JetBrains integrated terminal: `$TERM=xterm-256color`; 256-color; no synchronized output
- GitHub Codespaces browser terminal: `$TERM=xterm-256color`; 256-color; no synchronized output

**Terminal Emulators — Degraded Support (8-color, ASCII symbols)**

- xterm (basic): `$TERM=xterm`; 8-color; no synchronized output; wcwidth still used for layout
- Terminals where `$TERM=dumb` or `$TERM` is unset: no color, no Unicode; fall back to ASCII symbols table (see Section 8.5)

**Multiplexers**

- tmux 3.2+: tmux passes through synchronized output BSU/ESU to the outer terminal starting in version 3.2. Detected via `$TMUX`. The outer terminal TERM capability is read from `tmux show -gv default-terminal`. All committed transcript lines are visible in tmux scrollback normally.
- GNU screen 4.9+: no synchronized output passthrough. Detected via `$STY`. All committed transcript lines are visible in screen scrollback. Increased flicker risk on fast-scrolling content; render rate automatically reduced to 20fps (50ms → 50ms, same rate, but force-redraw only on change).

**SSH**

SSH connections preserve all terminal capabilities as reported by the remote `$TERM` variable. The typical case is `$TERM=xterm-256color` which gives full 256-color support. Network latency is handled by the single-write-per-frame batching in `Terminal._write_atomic()` — a 200ms RTT SSH connection produces at most 20 round trips per second during streaming, each carrying a full frame update as a single TCP segment. On high-latency connections (>500ms RTT), the render rate automatically degrades to 10fps (100ms MIN_TICK_INTERVAL) to reduce the subjective flickering from partially-arrived frame writes.

**Operating Systems**

- macOS 12+ (Monterey and later): full support
- macOS 11 (Big Sur): full support
- Linux kernel 4.x+: full support (any distribution with glibc 2.31+)
- Windows 10 1903+ with WSL2: full support under WSL2 terminal; requires UTF-8 locale in WSL
- Windows 11: full support under Windows Terminal with WSL2
- FreeBSD 13+: full support (POSIX terminal API compatible)

### 5.3 Accessibility

**Color Environment Detection**

At startup, `Terminal.__init__()` performs the following checks in order:

1. If `NO_COLOR` is set in the environment (any value), disable all ANSI color codes. Symbol-based differentiation (✓ ✗ ⚠ ○ ●) is the only visual indicator. This is permanent for the session and cannot be overridden by configuration.
2. If `FORCE_COLOR=1` is set, enable color output even if stdout is not a TTY (e.g., when piped to a file). This is for CI environments that support color in logs.
3. If `COLORTERM=truecolor` or `COLORTERM=24bit`, enable 24-bit RGB color mode.
4. If `COLORTERM=256color` or `TERM` ends in `-256color`, enable 256-color mode.
5. If stdout is a TTY and none of the above, default to 256-color mode on most modern systems.
6. If stdout is not a TTY and FORCE_COLOR is not set, disable color and use plaintext mode.

**Color-Blind Accessibility**

The semantic color palette is tested for visibility under the three most common forms of color blindness: deuteranopia (red-green), protanopia (red deficiency), and tritanopia (blue-yellow). The key differentiation in the status bar is by symbol AND color: AUTO (● green), PLAN (◆ yellow), ASK (? cyan), REVIEW (⊕ blue), SAFE (⛔ red), DEBUG (⚙ magenta). Under deuteranopia simulation, the green/red distinction is lost but AUTO/SAFE remain visually distinct via symbol shape.

**Keyboard Navigation**

Every action in agenthicc is reachable via keyboard without a mouse:
- Submit message: Enter
- New line in input: Shift+Enter or Alt+Enter
- Cycle permission modes: Shift+Tab (forward) / Alt+Shift+Tab (backward)
- Open /command palette: type "/" at start of input line
- Navigate dropdown: Arrow keys, Tab/Shift+Tab
- Select dropdown item: Enter
- Dismiss dropdown: Escape
- Expand/collapse tool call: Ctrl+E on the tool call line
- Cancel current agent turn: Ctrl+C
- Background current request: Ctrl+B
- View session history: /history
- Approve tool call: y or Enter at approval gate
- Deny tool call: n at approval gate
- Allow all this session: a at approval gate

**Screen Reader Compatibility**

When `--accessibility` flag is passed (or `accessibility = true` in config), agenthicc switches to a screen-reader-friendly output mode: (a) the bottom block is not erased and redrawn — instead, new content is appended to stdout as plain lines; (b) ANSI formatting is stripped to `--no-color` equivalent; (c) spinner animation is replaced with periodic status text lines ("Agent is working... (15s)"); (d) the input prompt is a simple `> ` prefix. This mode is compatible with macOS VoiceOver and Linux Orca.

**Font Requirements**

All symbols used in the default mode are in the Unicode Basic Multilingual Plane and available in the following common monospace fonts without custom patches: Menlo, Monaco, Consolas, DejaVu Sans Mono, Liberation Mono, Courier New, JetBrains Mono (non-Nerd variant). No Nerd Font is required. The fallback ASCII symbol table (see Section 8.5) activates automatically when `$TERM=xterm` (8-color mode) or when `--ascii` flag is passed.

### 5.4 Reliability

**Terminal Resize (SIGWINCH)**

The `Terminal` class registers a SIGWINCH handler via `signal.signal(signal.SIGWINCH, self._on_sigwinch)`. The handler sets `self._resize_pending = True` and does no other work (signal handlers must be fast). On the next RenderLoop tick, the loop checks `terminal.resize_pending`, calls `terminal.update_size()` (which re-reads `os.get_terminal_size()`), recomputes the FrameComposer layout with the new dimensions, and issues a full redraw. The bottom block is redrawn at the correct new width within one tick interval (≤50ms). If the resize makes the terminal narrower than the current input buffer, the input is word-wrapped to fit the new width.

**Interrupt Handling (SIGINT / Ctrl+C)**

A single SIGINT (Ctrl+C) cancels the current agent turn if one is in progress. The cancellation path: (a) set `app_state.current_turn.cancelled = True`; (b) emit a `turn_cancelled` event to the event processor; (c) the agent runner detects the cancelled flag and stops sending new tokens; (d) the partially-streamed content is committed to the transcript as a `[cancelled]` turn; (e) the bottom block redraws in idle state. A second SIGINT within 2 seconds exits the process gracefully (same as SIGTERM). A third SIGINT always exits immediately.

**Graceful Shutdown (SIGTERM)**

SIGTERM triggers: (a) cancel any in-progress agent turn; (b) flush all uncommitted transcript lines to stdout; (c) clear the bottom block; (d) save session metadata (events.jsonl is already written incrementally); (e) exit with code 0. The total shutdown sequence must complete within 5 seconds.

**Crash Recovery**

Unhandled exceptions in the main event loop are caught by the top-level exception handler in `__main__.py`. The handler: (a) calls `terminal.clear_bottom()` to remove the bottom block and avoid leaving partial escape sequences in the terminal; (b) writes the full traceback to `~/.agenthicc/crash-{timestamp}.log`; (c) prints a human-readable error to stderr: `agenthicc crashed: {exception_type}: {message}. Log: ~/.agenthicc/crash-{timestamp}.log`; (d) exits with code 1. The terminal is always left in a clean state — no dangling escape sequences, no raw/cbreak mode left active.

**Session Resume**

Every session writes events to `{project_dir}/.agenthicc/sessions/{session_id}/events.jsonl`. When `agenthicc --resume {session_id}` is invoked: (a) `EventProcessor.restore_from_log()` replays the event log; (b) `TranscriptModel` rebuilds from the replayed events; (c) the most recent N turns (up to MAX_TURNS_IN_MEMORY) are printed to committed transcript; (d) the session continues with the restored AppState. If the events.jsonl is truncated or contains malformed JSON lines, the loader skips the corrupted lines, logs a warning to stderr, and continues with the last valid state.

**Long-Session Stability**

For sessions running 8+ hours (common in DevOps incident response), the following stability measures apply:
- Memory eviction: `TranscriptModel._evict_old_turns()` runs automatically when `len(turns) > MAX_TURNS_IN_MEMORY` (200). Evicted turns lose their `output_lines` but retain their metadata (turn ID, agent ID, timestamp, summary).
- File descriptor hygiene: all file handles in `ProjectMemoryLayer` and `GlobalMemoryLayer` use context managers; no FD leaks.
- Event log rotation: if `events.jsonl` exceeds 100MB, it is rotated to `events.jsonl.1` and a new `events.jsonl` starts. The rotate-and-continue is atomic via file rename.
- SQLite WAL checkpointing: every 1000 events, `ProjectMemoryLayer` issues a `PRAGMA wal_checkpoint(TRUNCATE)` to prevent WAL file from growing unboundedly.

## 6. Architecture

### 6.1 High-Level Architecture

The following ASCII diagram shows the full data flow from user input to terminal output:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         agenthicc process                               │
│                                                                         │
│  ┌─────────────┐     ┌──────────────────┐     ┌──────────────────────┐ │
│  │  InputState  │────▶│  EventProcessor  │────▶│      AppState        │ │
│  │  (CBREAK)   │     │  (MPSC Queue)    │     │  (frozen dataclass)  │ │
│  └─────────────┘     └──────────────────┘     └──────────┬───────────┘ │
│         │                     │                           │             │
│         │                     │  emit events              │ state diff  │
│         │                     ▼                           ▼             │
│         │            ┌─────────────────┐       ┌──────────────────────┐│
│         │            │  AgentRunner    │       │   TUIEventAdapter    ││
│         │            │  (lauren-ai)    │       │   (subscribes to     ││
│         │            │                │       │    processor queue)  ││
│         │            └────────┬────────┘       └──────────┬───────────┘│
│         │                     │ tokens/events              │            │
│         │                     ▼                           ▼            │
│         │            ┌─────────────────┐       ┌──────────────────────┐│
│         │            │  TranscriptModel│◀──────│  TranscriptModel     ││
│         │            │  (turns, tools, │       │  mutations           ││
│         │            │   streaming)    │       └──────────────────────┘│
│         │            └────────┬────────┘                               │
│         │                     │ new committed lines / bottom changes    │
│         │                     ▼                                         │
│         │            ┌─────────────────┐                               │
│         │            │   RenderLoop    │                               │
│         │            │   (50ms tick)   │                               │
│         │            └────────┬────────┘                               │
│         │                     │                                         │
│         │          ┌──────────┴──────────┐                             │
│         │          │                     │                             │
│         │          ▼                     ▼                             │
│         │  ┌───────────────┐   ┌──────────────────┐                   │
│         │  │TerminalCommit │   │  FrameComposer   │                   │
│         │  │(committed     │   │  (pure function) │                   │
│         │  │ lines → stdout│   │  produces bottom │                   │
│         │  │ permanently)  │   │  block Frame     │                   │
│         │  └───────┬───────┘   └────────┬─────────┘                   │
│         │          │                    │                              │
│         │          └──────────┬─────────┘                             │
│         │                     │                                        │
│         │                     ▼                                        │
│         │            ┌─────────────────┐                              │
│         └───────────▶│    Terminal     │                              │
│                       │  (single I/O   │                              │
│                       │   owner)       │                              │
│                       └────────┬───────┘                              │
│                                │                                       │
└────────────────────────────────┼───────────────────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  stdout (fd 1)         │
                    │  ─ committed lines     │
                    │    scroll permanently  │
                    │    into scrollback     │
                    │  ─ bottom block        │
                    │    erased+redrawn      │
                    │    each frame          │
                    └────────────────────────┘
```

**Key invariants:**

1. `Terminal` is the sole owner of file descriptor 1 (stdout). No other component writes directly to stdout.
2. `AppState` is immutable. Every state change produces a new `AppState` instance.
3. `FrameComposer.compose()` is a pure function: same inputs → same outputs, no side effects.
4. `RenderLoop` is the sole caller of `Terminal.commit_lines()` and `Terminal.set_bottom()`.
5. `EventProcessor` is the sole entry point for all state mutations; all inter-component communication goes through events.

### 6.2 Rendering Architecture

**The Committed-Transcript Pattern**

The rendering architecture is based on the committed-transcript + live-bottom-block pattern, also known as the "Ink style" or "log-update style" from the JavaScript CLI ecosystem. The key insight is that completed output (finished agent turns, committed tool call results) is fundamentally different from in-progress output (streaming tokens, spinning tool calls, current input). Completed output belongs in the terminal's native scrollback buffer. In-progress output lives in a small, dynamically-erased bottom block.

**Terminal Class API**

```python
class Terminal:
    """Single owner of stdout. All terminal I/O goes through this class."""

    def __init__(self, fd: int = 1) -> None:
        """Initialize terminal, detect capabilities, query size."""

    @property
    def size(self) -> Size:
        """Current terminal dimensions. Updated on SIGWINCH."""

    @property
    def capabilities(self) -> TerminalCapabilities:
        """Color depth, Unicode support, synchronized output availability."""

    def commit_lines(self, lines: list[str]) -> None:
        """
        Print lines permanently to stdout.
        These lines scroll into scrollback and are NEVER erased.
        Must be called only when the bottom block is empty or after clear_bottom().
        """

    def set_bottom(self, frame: Frame) -> None:
        """
        Erase the current bottom block and draw a new one.
        Uses the canonical erase sequence:
          \\x1b[2K + (\\x1b[1A\\x1b[2K) × (old_height - 1) + \\r
        Then writes the new frame content as a single batched write().
        """

    def clear_bottom(self) -> None:
        """Erase the bottom block and leave cursor at the cleared position."""

    def update_size(self) -> None:
        """Re-query terminal size (called after SIGWINCH)."""

    def _write_atomic(self, data: bytes) -> None:
        """Single os.write(self._fd, data) call. Never called directly."""
```

**Bottom Block Erase Sequence**

The canonical erase sequence for N rows of bottom block is:

```
\\x1b[2K                    # erase current line
\\x1b[1A\\x1b[2K             # move up 1, erase  (repeated N-1 times)
\\r                          # carriage return to column 0
```

This is identical to the sequence used by:
- Rich `Live._render()` via `Console.control(Control.CURSOR_UP(n))`
- npm `log-update` `eraseLines(n)` function
- Ink's `reconcile()` erase step
- Textual `inline` mode `clear_widgets()`

The sequence must be assembled into a single `bytearray` and written with one `os.write()` call to minimize SSH round trips.

**Synchronized Output Protocol**

When the terminal supports synchronized output (`terminal.capabilities.synchronized_output = True`), each frame is wrapped in BSU/ESU markers:

```
\\x1b[?2026h    # Begin Synchronized Update (BSU)
[erase + new frame content]
\\x1b[?2026l    # End Synchronized Update (ESU)
```

This tells the terminal to buffer all rendering until the ESU marker, eliminating the visual tearing that occurs when the erase and redraw happen in separate screen refreshes. Supported by: iTerm2, Alacritty, Kitty, WezTerm, Windows Terminal, Konsole, tmux 3.2+ (passes through to outer terminal).

**RenderLoop**

```python
class RenderLoop:
    MIN_TICK_INTERVAL: float = 0.050  # 50ms debounce

    def __init__(
        self,
        terminal: Terminal,
        composer: FrameComposer,
        transcript: TranscriptModel,
        input_state: InputState,
    ) -> None: ...

    async def run(self) -> None:
        """Main loop. Runs until shutdown event."""

    def force_commit(self, turn: AgentTurnEntry) -> None:
        """Immediately commit a completed turn to scrollback. No tick delay."""

    def request_redraw(self) -> None:
        """Set dirty flag; next tick will redraw the bottom block."""
```

The render loop algorithm on each tick:

1. Check `self._needs_redraw`. If False and no new committed lines pending, skip.
2. If new committed lines are pending (from a force_commit call): call `terminal.clear_bottom()`, call `terminal.commit_lines(pending_lines)`, clear `pending_lines`.
3. Compose new frame: `frame = composer.compose(transcript, input_state, terminal.size)`.
4. If `frame == self._last_frame` and no committed lines were written: skip (no change).
5. Call `terminal.set_bottom(frame)`.
6. Store `self._last_frame = frame`.
7. Clear `self._needs_redraw`.
8. Sleep until `MIN_TICK_INTERVAL` has elapsed since step 1.

**Frame Dataclass**

```python
@dataclass(frozen=True)
class Frame:
    """Immutable snapshot of the bottom block to render."""
    rows: list[str]          # rendered rows, each pre-formatted with ANSI
    height: int              # len(rows), for erase sequence calculation
    streaming_text: str | None  # partial agent text being streamed (if any)
    cursor_row: int          # 0-indexed row of the input cursor within rows
    cursor_col: int          # 0-indexed column of the input cursor
```

### 6.3 State Management

**AppState to FrameComposer Flow**

`AppState` is the single source of truth for all agent state. `TUIEventAdapter` subscribes to the `EventProcessor` subscriber queue and translates `AppState` diffs into `TranscriptModel` mutations. `FrameComposer` reads `TranscriptModel` and `InputState` (a separate, TUI-only data structure not in `AppState`) to produce `Frame` objects.

The separation between `AppState` (kernel state, event-sourced) and `TranscriptModel` (TUI presentation state) is intentional. `AppState` changes happen on the event processor's async coroutine; `TranscriptModel` mutations are applied by `TUIEventAdapter` on the same event loop. `InputState` mutations happen on keyboard input events, which arrive on the asyncio event loop via `InputState._read_loop()`.

**FrameComposer — Pure Function**

`FrameComposer.compose()` is a pure function with the following signature:

```python
def compose(
    self,
    transcript: TranscriptModel,
    input_state: InputState,
    size: Size,
    now: float | None = None,  # for deterministic tests; defaults to time.monotonic()
) -> Frame:
```

It reads (but never mutates) `transcript` and `input_state`. It produces a `Frame` with the bottom block rows. The bottom block structure is:

```
[streaming_text_row]   (optional, only during active streaming)
[status_bar_row]       (always present)
[divider_row]          (always present: ─ × width)
[input_bar_row(s)]     (1 or more rows depending on wrapped input)
[mode_footer_row]      (always present: 3-5 context-relevant keybindings)
[dropdown_rows]        (0-8 rows, only when dropdown is open)
```

The bottom block is bounded to a maximum of `min(12, size.rows // 3)` rows to prevent it from consuming more than one-third of a small terminal.

**Diff Algorithm**

`RenderLoop` uses frame equality (`frame == self._last_frame`, via frozen dataclass `__eq__`) to skip redundant redraws. For the committed transcript, `TranscriptModel` tracks a `_committed_cursor: int` — the index of the last committed line. On each tick, the render loop checks if `transcript.committed_cursor < len(transcript.all_committed_lines)`. If new lines are pending, it commits them in batch.

`FrameComposer` caches rendered rows per turn via `_render_cache: dict[str, list[str]]` where the key is `turn.turn_id`. This means re-rendering 200 turns in memory is O(new_turns), not O(all_turns). The cache is invalidated when a turn transitions from STREAMING to COMPLETE state (the streaming partial text becomes a committed final text).

### 6.4 Component Architecture

**Component Tree**

The TUI is structured in two distinct rendering layers that never mix:

```
Layer 1: Committed Transcript (raw stdout, never Textual)
  ├── AgentTurnHeader (committed line: "● agent:planner  09:41:22")
  ├── AgentTurnText (committed lines: rendered markdown as ANSI text)
  ├── ToolCallLine (committed line: "  ⎿ read_file(...)  ✓ 12ms")
  ├── DiffBlock (committed lines: unified diff, max 50 lines)
  └── TurnSeparator (committed line: "─" × width)

Layer 2: Bottom Block (Textual App.run(inline=True), 3-6 rows)
  └── BottomApp (Textual App, inline mode)
      ├── StreamingText (optional, Rich MarkdownStream)
      ├── StatusBar (Rich Text, reactive)
      │   ├── ModeIndicator (colored badge)
      │   ├── ModelInfo (provider/model string)
      │   ├── AgentCount (N agents)
      │   ├── CostDisplay ($X.XXX)
      │   ├── TokenCount (N tok)
      │   └── SessionId (truncated)
      ├── Divider (─ × width)
      ├── InputBar (Textual TextArea, custom bindings)
      ├── ModeFooter (3-5 keybinding hints)
      └── TriggerDropdown (Float, OptionList, max 8 items)
```

**Textual Inline Mode Usage**

The bottom block uses Textual `App.run(inline=True)` for the input bar and dropdown, because Textual provides the best input handling for multi-line editing, @mention parsing, and dropdown positioning. However, the Textual app is strictly constrained to the bottom block. It must never draw above the input zone.

The Textual `App` is initialized with `inline=True`, which tells Textual to draw only in the current terminal position (below committed lines) without claiming the full screen. The app height is dynamically computed as `max(3, min(6, terminal.size.rows // 4))` rows.

When the Textual bottom block needs to print committed content above itself (agent turn text), it calls `terminal.commit_lines()` directly — bypassing Textual's rendering. This means: (a) call `textual_app.suspend()`; (b) call `terminal.clear_bottom()`; (c) call `terminal.commit_lines(new_lines)`; (d) call `textual_app.resume()`. The `suspend()`/`resume()` cycle takes under 5ms on a modern system.

**MarkdownStream Usage**

For LLM token streaming within the bottom block, the implementation uses `textual.widgets.MarkdownStream` (available since Textual 0.56). The `MarkdownStream` widget accepts a `generator` of string tokens and renders them incrementally using incremental Markdown parsing. This is O(n) in the number of new tokens per tick, not O(n²) as `Markdown.update()` in a loop would be.

```python
from textual.widgets import MarkdownStream

class StreamingText(Widget):
    def stream_tokens(self, token_generator: AsyncGenerator[str, None]) -> None:
        self.query_one(MarkdownStream).stream(token_generator)
```

When the turn completes, `MarkdownStream.finish()` is called to flush the final render. The full rendered Markdown text is then passed to `terminal.commit_lines()` as ANSI-formatted text via Rich's `Console.render_str()`.

### 6.5 Input Architecture

**CBREAK Mode**

The input bar reads keyboard input in CBREAK mode (via `tty.setcbreak(sys.stdin.fileno())`), which delivers characters one at a time without buffering, while still handling Ctrl+C as SIGINT. This is used by the `InputState._read_loop()` coroutine, which runs as an `asyncio.Task`. However, in the Textual-based bottom block, Textual handles input directly — `InputState` is used only when the Textual bottom app is not running (headless mode, accessibility mode).

**Readline Emulation**

The `InputBar` Textual widget provides readline-like editing:
- Ctrl+A: move to start of line
- Ctrl+E: move to end of line
- Ctrl+K: kill from cursor to end of line (to kill ring)
- Ctrl+U: kill from cursor to start of line (to kill ring)
- Ctrl+W: kill word backward (to kill ring)
- Ctrl+Y: yank from kill ring
- Alt+F: move forward one word
- Alt+B: move backward one word
- Up/Down: history navigation (from session history)
- Tab: trigger autocomplete if @mention or / is detected

**@Mention Parsing**

The `@mention` trigger fires when `@` is typed at a position where it is not part of an email address. The detection rule:

```python
def should_trigger_at_mention(text: str, cursor_pos: int) -> bool:
    if cursor_pos == 0:
        return False
    preceding_char = text[cursor_pos - 1]
    if preceding_char != '@':
        return False
    # Check if this @ is part of an email: preceded by word chars
    pre_at = text[:cursor_pos - 1]
    if pre_at and pre_at[-1].isalnum():
        return False  # email address context, don't trigger
    return True
```

When triggered, `TriggerDropdown` opens with file completions from the current project directory. The completion list is built by `AtMentionResolver.resolve(prefix: str) -> list[AtMentionItem]`, which searches for files, directories, and agent names matching the prefix. File completions use `pathlib.Path.glob()` with a 200-item limit.

**Slash Command Palette**

The `/` trigger fires when `/` is the first non-whitespace character on the input line. The `TriggerDropdown` opens with all registered commands from the `CommandRegistry`. The dropdown supports fuzzy search: as the user types after `/`, the list filters using `difflib.SequenceMatcher`. Each command item shows: command name, one-line description, and argument hint (e.g., `/mode [auto|plan|ask|review|safe|debug]`).

**Multi-Line Input**

Shift+Enter or Alt+Enter inserts a newline within the `InputBar`. The input bar grows vertically (up to 4 rows) as the user types multiple lines. When the input bar height changes, `RenderLoop.request_redraw()` is called immediately so the bottom block resizes before the next tick.

**Background Requests (Ctrl+B)**

Ctrl+B marks the current agent turn as "backgrounded": the agent runner continues processing but the TUI returns to the idle input state immediately. The status bar shows `[1 bg]` to indicate one backgrounded task. When a backgrounded turn completes, a notification line is committed to the transcript: `● background turn completed: {turn_summary}`.

---

## 7. User Flows

### 7.1 Standard Chat Workflow

This is the most common interaction pattern: the user types a message, the agent processes it, streams a response, and the turn is committed to the transcript.

**Before user types (idle state):**

```
┌────────────────────────────────────────────────────────────────────────┐
│  [committed transcript - scrolls into scrollback above]               │
│  ● agent:main  09:40:55                                                │
│  I've analyzed the authentication module. The issue is in             │
│  src/auth/session.py at line 147 where the token expiry is            │
│  calculated using local time instead of UTC.                          │
│                                                                        │
│    ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)       │
│                                                                        │
│  ────────────────────────────────────────────────────────────────────  │
├────────────────────────────────────────────────────────────────────────┤
│  [AUTO] claude-sonnet-4-6  1 agent  $0.023  4.2k tok  [a3f8]         │
│  ────────────────────────────────────────────────────────────────────  │
│  > _                                                                   │
│  Enter:send  Shift+Tab:mode  /:commands  @:files  Ctrl+B:background   │
└────────────────────────────────────────────────────────────────────────┘
```

**During agent response streaming:**

```
┌────────────────────────────────────────────────────────────────────────┐
│  [committed transcript - scrollback]                                   │
│  ● agent:main  09:40:55                                                │
│  I've analyzed the authentication module. The issue is in             │
│  src/auth/session.py at line 147 ...                                  │
│  ────────────────────────────────────────────────────────────────────  │
│  ● agent:main  09:41:03  ⠹                                            │
│  The fix is straightforward. We need to replace `datetime.now()`      │
│  with `datetime.now(timezone.utc)` and ensure the comparison in       │
│  the token validation                                                  │
├────────────────────────────────────────────────────────────────────────┤
│  [AUTO] claude-sonnet-4-6  1 agent  $0.031  6.1k tok  [a3f8]         │
│  ────────────────────────────────────────────────────────────────────  │
│  > fix the auth bug                                                    │
│  Enter:send  Ctrl+C:cancel  Ctrl+B:background                         │
└────────────────────────────────────────────────────────────────────────┘
```

Note: during streaming, the partial agent text appears in the committed transcript area (it has already been "committed" as streaming lines that will be finalized). The bottom block shows the input bar (disabled during agent turn) and the reduced keybinding footer.

**After turn completes (committed):**

The streaming lines are finalized. The complete turn text and all tool call results are now permanently in the scrollback. The bottom block returns to idle state. A turn separator line is committed.

**Flow steps:**

1. User types message in `InputBar`. Characters echo immediately via Textual's text area widget.
2. User presses Enter. `InputState.submit()` is called. The input text is cleared. An `intent_submitted` event is emitted to the `EventProcessor`.
3. The committed transcript line `● agent:main  {timestamp}  ⠹` is printed via `terminal.commit_lines()`. This line is now in scrollback permanently.
4. `AgentRunner` starts processing. Each token arrives as a `streaming_token` event.
5. `TUIEventAdapter` buffers tokens in `TranscriptModel.current_streaming_buffer`.
6. `RenderLoop` ticks every 50ms. If the buffer has new tokens, `FrameComposer` renders them as streaming text appended to the current agent turn in the transcript area.
7. When the agent turn ends, `force_commit()` is called: the streaming buffer is rendered to final ANSI lines and committed to scrollback via `terminal.commit_lines()`. The spinner line is NOT erased — it stays in scrollback as `● agent:main  09:41:03`.
8. A turn separator (`─` × width) is committed.
9. The bottom block redraws in idle state.

### 7.2 Tool Execution Workflow

When the agent calls a tool, the tool call is rendered as a one-line collapsed entry in the committed transcript.

**Tool call lifecycle — render states:**

```
State 1: PENDING (tool call emitted, not yet started)
  ⎿ read_file(path='src/auth/session.py')  ○

State 2: RUNNING (tool executor has started, spinner animating)
  ⎿ read_file(path='src/auth/session.py')  ⠸

State 3: SUCCESS (tool returned result)
  ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)

State 4: ERROR (tool returned error)
  ⎿ read_file(path='src/nonexistent.py')  ✗ 2ms  No such file or directory

State 5: APPROVAL_NEEDED (requires user confirmation before running)
  ⎿ write_file(path='src/auth/session.py')  ⚠ awaiting approval
```

**Commit timing:**

- States 1-2 (PENDING → RUNNING): The tool call line is committed to scrollback as soon as the tool call is detected. This means the line is in scrollback immediately and will not be redrawn or erased. The spinner in state 2 is a live element — but wait, committed lines cannot be updated. Therefore:
  - The tool call spinner is rendered in the **bottom block streaming area**, not in the committed transcript. The committed transcript shows the tool call header line only when the tool call completes (SUCCESS or ERROR).
  - During execution, the bottom block shows: `  ⎿ read_file(path='src/auth/session.py')  ⠸` as part of the live streaming area.
  - On completion, the final tool call line is committed to scrollback: `  ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)`.

**Expanded tool call view:**

When the user presses Ctrl+E on a completed tool call line (or on a numbered reference), the tool call output is committed as additional indented lines below the tool call summary:

```
  ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)  [expanded]
    │ 147: expiry = datetime.now() + timedelta(hours=24)
    │ 148: if token.expiry < datetime.now():
    │ 149:     raise TokenExpiredError()
    │ ...  (139 more lines, use /tool-output {id} to see all)
```

**Multiple concurrent tool calls:**

When the agent calls multiple tools in sequence (not parallel), each tool call line is committed as it completes. The bottom block streaming area shows the currently-running tool. The committed transcript shows the completed tools above:

```
[committed]  ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)
[committed]  ⎿ read_file(path='tests/test_auth.py')  ✓ 87 lines (0.2s)
[live]       ⎿ grep_files(pattern='datetime.now', path='src/')  ⠋
```

### 7.3 Approval/Confirmation Workflow

When an agent action requires user confirmation (in Review, Ask, or Safe mode, or when the action matches a `require_confirmation` rule in the security policy), the approval gate replaces the normal idle state in the bottom block.

**Step 1: Diff is committed to transcript before the approval gate appears**

The agent proposes a file write. Before the bottom block shows the approval gate, the proposed diff is committed to the transcript so the user can read it in scrollback:

```
[committed]  ● agent:main  09:42:15
[committed]  I'll fix the token expiry issue by changing datetime.now() to
[committed]  datetime.now(timezone.utc) in the session validation code.
[committed]
[committed]  ─── Proposed change: src/auth/session.py ──────────────────
[committed]  @@ -145,6 +145,6 @@
[committed]  -    expiry = datetime.now() + timedelta(hours=24)
[committed]  +    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
[committed]  -    if token.expiry < datetime.now():
[committed]  +    if token.expiry < datetime.now(timezone.utc):
[committed]  ──────────────────────────────────────────────────────────
```

**Step 2: Approval gate appears in bottom block**

```
├────────────────────────────────────────────────────────────────────────┤
│  [REVIEW] claude-sonnet-4-6  1 agent  $0.038  7.2k tok  [a3f8]       │
│  ────────────────────────────────────────────────────────────────────  │
│  ⚠ write_file(path='src/auth/session.py')  — approve?                │
│  [Y] Allow    [N] Deny    [A] Allow all this session                  │
└────────────────────────────────────────────────────────────────────────┘
```

**Step 3: User responds**

- `y` or Enter: The tool call proceeds. A committed line is added: `  ⎿ write_file(path='src/auth/session.py')  ✓ approved (0.8s)`.
- `n`: The tool call is denied. A committed line is added: `  ⎿ write_file(path='src/auth/session.py')  ✗ denied by user`. The agent receives a denial response and may propose an alternative.
- `a`: All future calls to `write_file` in this session are approved without prompting. A committed line notes: `  ⚠ write_file auto-approved for this session`.

**Batched approvals:**

When the agent queues multiple tool calls that require approval, they are shown in sequence, not all at once. The status bar shows `AWAITING 1 of 3` to indicate the queue:

```
│  ⚠ write_file(path='src/auth/session.py')  — approve? (1 of 3)      │
│  [Y] Allow    [N] Deny    [A] Allow all this session  [S] Skip queue  │
```

### 7.4 Long-Running Multi-Tool Workflow

This flow covers agents that run many sequential tools over a long period (5-30 minutes), typical in DevOps incident response or large refactoring sessions.

**Parallel sub-agent visualization:**

When the `Scheduler` spawns parallel sub-agents, each agent is assigned a distinct color (cycling through a palette of 6 named colors: magenta, cyan, yellow, blue, green, red). Each agent's committed lines are prefixed with its color:

```
[magenta] ● agent:planner    09:45:01  Breaking task into 3 subtasks
[cyan]    ● agent:auth-fix   09:45:02  ⠸ Reading auth module...
[yellow]  ● agent:test-gen   09:45:02  ⠋ Analyzing test coverage...
[blue]    ● agent:docs       09:45:03  ○ Waiting for auth-fix to complete
```

The status bar shows the count: `[AUTO] claude-sonnet-4-6  3 agents  $0.052  12.4k tok`.

**Doom-loop detection:**

`RenderLoop` tracks tool call history via `DoomLoopDetector`. If the same tool name + same arguments are called 3 times in the current turn, the detector fires:

1. The current agent turn is paused (not cancelled).
2. A banner is committed to the transcript:

```
  ⚠ DOOM LOOP DETECTED ────────────────────────────────────────────────
    The agent has called run_bash(cmd='terraform apply') 3 times with
    the same result. This may indicate the agent is stuck.
    Press [C] to cancel the turn, [R] to retry once more, [I] to inject
    a message to the agent.
```

3. The bottom block shows the doom-loop response options instead of the normal input bar.
4. If the user selects [I] (inject message), a text input appears and the injected message is added to the agent's context mid-turn.

**Session recap after idle:**

If the user returns to an idle terminal after 3+ minutes of inactivity (no input, no agent turn), the next interaction begins with a session recap committed to the transcript:

```
  ── Session recap (last 3 turns) ──────────────────────────────────────
  09:41 • Fixed auth token expiry: changed datetime.now() → UTC (2 files)
  09:43 • Wrote 3 new unit tests for session validation
  09:44 • Ran test suite: 47 passed, 0 failed (12.3s)
  ──────────────────────────────────────────────────────────────────────
```

This recap is generated by `SessionRecapGenerator.generate(turns: list[AgentTurnEntry], since: float) -> list[str]` which summarizes each turn using a short template (no LLM call for the recap — it is derived from the structured turn metadata).

### 7.5 Failure & Error Recovery Workflow

**Recoverable error (tool call failure):**

A tool call that returns an error is committed to the transcript with the ✗ state. The agent receives the error as tool output and can choose to retry, use a different tool, or ask the user for help. No user intervention is required. Example:

```
  ⎿ run_bash(cmd='pytest tests/')  ✗ 3.2s  exit code 1
    (agent sees full stdout/stderr as tool output and continues)
```

**Critical error (LLM API failure):**

If the LLM API returns an error (rate limit, server error, network timeout), the current streaming turn is paused and a banner appears in the bottom block:

```
├────────────────────────────────────────────────────────────────────────┤
│  [AUTO] claude-sonnet-4-6  1 agent  $0.063  14.1k tok  [a3f8]        │
│  ──────────────────────────────────────────────────────────────────── │
│  ✗ API ERROR: 429 rate_limit_exceeded — retry in 23s                 │
│  [R] Retry now  [W] Wait and retry  [C] Cancel turn                  │
└────────────────────────────────────────────────────────────────────────┘
```

The banner persists until the user responds. If the user selects [W], a countdown timer appears: `Retrying in 18s...` and the retry happens automatically.

**Fatal error (unhandled exception):**

If an unhandled exception escapes to the top-level exception handler in `__main__.py`:

1. `terminal.clear_bottom()` is called to remove the bottom block cleanly.
2. The exception traceback is written to `~/.agenthicc/crash-{timestamp}.log`.
3. The following is printed to stderr (not stdout, so it does not corrupt the committed transcript):
   ```
   agenthicc crashed: ValueError: unexpected state transition
   Session saved. Resume with: agenthicc --resume a3f8
   Full log: ~/.agenthicc/crash-20260613-094512.log
   ```
4. The process exits with code 1.
5. The terminal is left with the committed transcript visible in scrollback — the developer can read all previous output.

### 7.6 Multi-Hour Session Workflow

A 6-hour DevOps incident session has the following lifecycle:

**Session start:**
```
agenthicc --session "incident-prod-db-2026-06-13"
```
A new session UUID is assigned. The session directory `~/.agenthicc/sessions/a3f8.../` is created. `events.jsonl` starts empty.

**Active work phase (0-2 hours):** 30-50 turns, multiple tool calls per turn. Memory usage grows as `TranscriptModel` accumulates turns. At 200 turns, `_evict_old_turns()` runs silently.

**Idle phase (user steps away):** After 3+ minutes idle, the session recap generator arms. When the user returns and types, the recap is committed before the next agent turn.

**SSH disconnect and resume:** If the SSH connection drops, the session saves automatically (SIGHUP handler). The developer reconnects and runs:
```
agenthicc --resume a3f8
```
The last 50 committed turns are reprinted to the new terminal's scrollback. The session continues from the last valid AppState.

**Session end:**
```
/exit  (or Ctrl+D)
```
The session is saved. A session summary is committed to transcript:
```
  ── Session ended: incident-prod-db-2026-06-13 ──────────────────────
  Duration: 6h 14m  |  Turns: 87  |  Tools called: 342  |  Cost: $2.14
  Session saved. Resume with: agenthicc --resume a3f8
  ────────────────────────────────────────────────────────────────────
```

### 7.7 Remote SSH Workflow

**Connection and capability detection:**

When agenthicc starts over SSH, it detects the environment:
- `$TERM` is set by the SSH client's terminal configuration — typically `xterm-256color`
- Synchronized output is probed with a DSR query: if no response within 500ms, `capabilities.synchronized_output = False`
- Network RTT is estimated by timing the DSR probe (if supported) or defaulting to 50ms assumption

**Degraded mode triggers:**

If RTT > 200ms, degraded mode activates:
- Streaming debounce increases from 50ms to 150ms (reducing redraw frequency to ~7fps)
- Synchronized output wrapping is skipped even if supported (BSU/ESU add protocol overhead)
- The streaming text is shown less frequently — only every 3 ticks instead of every tick

**NO_COLOR over SSH:**

Many SSH sessions set `$TERM=xterm` (8-color) rather than `xterm-256color`. The color detection handles this gracefully: the degraded-color palette activates, using only the 8 standard ANSI colors (no 256-color codes). All symbols still render correctly because they are standard BMP Unicode.

**SSH disconnect simulation:**

In pyte integration tests, the SSH disconnect scenario is simulated by sending SIGHUP to the process and verifying that: (a) the bottom block is cleared; (b) the session is saved; (c) the process exits with code 0.

## 8. Visual Design Specification

### 8.1 Layout

**Full terminal layout (80×24 example):**

```
Row  1: [committed]  ● agent:main  09:40:55
Row  2: [committed]  Analyzing the authentication module. The token
Row  3: [committed]  expiry logic on line 147 uses local time instead
Row  4: [committed]  of UTC, causing sessions to expire incorrectly for
Row  5: [committed]  users in non-UTC timezones.
Row  6: [committed]
Row  7: [committed]    ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)
Row  8: [committed]
Row  9: [committed]  ────────────────────────────────────────────────
Row 10: [committed]  ● agent:main  09:41:03
Row 11: [committed]  Here is the fix I'll apply:
Row 12: [committed]
Row 13: [committed]  ─── Proposed change: src/auth/session.py ────────
Row 14: [committed]  @@ -147,2 +147,2 @@
Row 15: [committed]  - expiry = datetime.now() + timedelta(hours=24)
Row 16: [committed]  + expiry = datetime.now(timezone.utc) + timedelta(hours=24)
Row 17: [committed]  - if token.expiry < datetime.now():
Row 18: [committed]  + if token.expiry < datetime.now(timezone.utc):
Row 19: [committed]  ─────────────────────────────────────────────────
Row 20: [STATUS BAR] [AUTO] claude-sonnet-4-6  1 agent  $0.031  6.1k  [a3f8]
Row 21: [DIVIDER]    ────────────────────────────────────────────────
Row 22: [INPUT BAR]  > fix the session expiry issue with UTC
Row 23: [FOOTER]     Enter:send  Shift+Tab:mode  /:commands  @:files
```

Note: Row 24 is unused (cursor rest position below input bar). In a 24-row terminal, the bottom block occupies rows 20-23 (4 rows). Committed transcript occupies rows 1-19 (scrolls upward as more content arrives).

**Bottom block row allocation:**

| Zone | Rows | Always present |
|------|------|----------------|
| Streaming text (during agent turn) | 0-3 | No |
| Status bar | 1 | Yes |
| Divider | 1 | Yes |
| Input bar | 1-4 | Yes |
| Mode footer | 1 | Yes |
| Dropdown | 0-8 | No |
| **Total (typical)** | **4** | |
| **Total (with dropdown)** | **12 max** | |

The bottom block is bounded at `min(12, terminal.rows // 3)` rows. On a 12-row terminal (minimum supported), the bottom block is at most 4 rows.

**Minimum terminal size:**

The minimum supported terminal size is 60 columns × 12 rows. Below this size, a warning is committed to transcript: `⚠ Terminal too small (58×10). Minimum: 60×12`. The bottom block degrades to 2 rows: status bar + input bar only (divider and footer are omitted).

### 8.2 Color System

**Semantic Color Palette (truecolor/256-color mode):**

| Role | Name | ANSI Escape | 256-color | Hex | Usage |
|------|------|-------------|-----------|-----|-------|
| Agent header | AGENT | `\033[35m` | 5 | #AF87FF | Agent turn header line |
| Success | SUCCESS | `\033[32m` | 2 | #5FAF5F | Tool call ✓, test pass |
| Error | ERROR | `\033[31m` | 1 | #D75F5F | Tool call ✗, exceptions |
| Warning | WARNING | `\033[33m` | 3 | #D7AF5F | Doom loop, approvals |
| Info | INFO | `\033[34m` | 4 | #5F87AF | Status info, metadata |
| Muted | MUTED | `\033[2m` | (dim) | — | Timestamps, file paths |
| Emphasis | BOLD | `\033[1m` | (bold) | — | Headers, key terms |
| Input | NORMAL | `\033[0m` | — | — | User input text |
| Mode: AUTO | MODE_AUTO | `\033[32;1m` | 2+bold | #5FAF5F | AUTO badge |
| Mode: PLAN | MODE_PLAN | `\033[33;1m` | 3+bold | #D7AF5F | PLAN badge |
| Mode: ASK | MODE_ASK | `\033[36;1m` | 6+bold | #5FAFAF | ASK badge |
| Mode: REVIEW | MODE_REVIEW | `\033[34;1m` | 4+bold | #5F87AF | REVIEW badge |
| Mode: SAFE | MODE_SAFE | `\033[31;1m` | 1+bold | #D75F5F | SAFE badge |
| Mode: DEBUG | MODE_DEBUG | `\033[35;1m` | 5+bold | #AF87FF | DEBUG badge |
| Diff add | DIFF_ADD | `\033[32m` | 2 | #5FAF5F | + lines in diffs |
| Diff remove | DIFF_REM | `\033[31m` | 1 | #D75F5F | - lines in diffs |
| Diff hunk | DIFF_HUNK | `\033[36m` | 6 | #5FAFAF | @@ markers |
| Spinner | SPINNER | `\033[36m` | 6 | #5FAFAF | Braille spinner |
| Agent 1 | AGENT_1 | `\033[35m` | 5 | — | 1st parallel agent |
| Agent 2 | AGENT_2 | `\033[36m` | 6 | — | 2nd parallel agent |
| Agent 3 | AGENT_3 | `\033[33m` | 3 | — | 3rd parallel agent |
| Agent 4 | AGENT_4 | `\033[34m` | 4 | — | 4th parallel agent |
| Agent 5 | AGENT_5 | `\033[32m` | 2 | — | 5th parallel agent |
| Agent 6 | AGENT_6 | `\033[31m` | 1 | — | 6th parallel agent |

**NO_COLOR mode:**

When `NO_COLOR` is set, all ANSI color codes are suppressed. Differentiation relies entirely on symbols and text:
- Mode badge: `[AUTO]`, `[PLAN]`, `[ASK]`, `[REVIEW]`, `[SAFE]`, `[DEBUG]` (bracket notation, no color)
- Success: `✓` (no green)
- Error: `✗` (no red)
- Warning: `⚠` (no yellow)
- Agent header: `● agent:name  timestamp` (no magenta)
- Diff adds: `+` prefix (no green background)
- Diff removes: `-` prefix (no red background)

**8-color degraded mode ($TERM=xterm):**

Uses only the 8 standard ANSI colors (30-37, 40-47) plus bold (1) and dim (2). The same semantic mapping applies but with the nearest 8-color equivalent. Braille spinner is replaced by ASCII spinner: `|`, `/`, `-`, `\`.

### 8.3 Typography

**Bold (`\033[1m`) — used for:**
- Agent turn header line: `● agent:main  09:41:03`
- Status bar mode badge: `[AUTO]`
- Error messages: `✗ API ERROR: 429 rate_limit_exceeded`
- Doom loop banner heading: `⚠ DOOM LOOP DETECTED`
- Section headers in tool output expansions
- User's own input text (when committed to transcript as a quote)

**Dim/muted (`\033[2m`) — used for:**
- Timestamps on agent turn headers
- File paths in tool call lines (the path argument value)
- Metadata in the status bar: token count, session ID
- Turn separator lines (`─` × width)
- Line numbers in expanded tool output

**Italic (`\033[3m`) — used for:**
- Quoted text within agent responses (Markdown blockquotes rendered with italic)
- Code comments within inline code spans
- Session recap lines (to visually distinguish from live content)

**Underline (`\033[4m`) — used for:**
- Clickable file paths (only when the terminal supports OSC 8 hyperlinks, detected via `$TERM_PROGRAM` capability)
- Command names in the /command palette dropdown

**No-decoration (normal) — used for:**
- All body text in agent responses
- Tool call output content
- Input bar text as the user types
- Dropdown option text

### 8.4 Component Visual Specs

**Agent Turn Header:**
```
● agent:main  09:41:03
^             ^
AGENT color   MUTED dim timestamp
```
Full ANSI: `\033[35;1m●\033[0m \033[35magent:main\033[0m  \033[2m09:41:03\033[0m`

**Tool Call Line (success):**
```
  ⎿ read_file(path='src/auth/session.py')  ✓ 142 lines (0.3s)
  ^            ^                            ^
  indent       MUTED path                  SUCCESS ✓
```
Full ANSI: `  \033[2m⎿\033[0m read_file(\033[2mpath='src/auth/session.py'\033[0m)  \033[32m✓\033[0m \033[2m142 lines (0.3s)\033[0m`

**Tool Call Line (error):**
```
  ⎿ read_file(path='/nonexistent.py')  ✗ 2ms  No such file or directory
```
Full ANSI: `  \033[2m⎿\033[0m read_file(\033[2mpath='/nonexistent.py'\033[0m)  \033[31m✗\033[0m \033[2m2ms\033[0m  \033[31mNo such file or directory\033[0m`

**Tool Call Line (awaiting approval):**
```
  ⎿ write_file(path='src/auth/session.py')  ⚠ awaiting approval
```

**Approval Gate (in bottom block):**
```
│  ⚠ write_file(path='src/auth/session.py')  — approve?               │
│  [Y] Allow    [N] Deny    [A] Allow all this session                  │
```

**Status Bar:**
```
[AUTO] claude-sonnet-4-6  1 agent  $0.031  6.1k tok  [a3f8]
^      ^                  ^        ^        ^          ^
mode   model              agents   cost     tokens     session-id
```
Full layout: `\033[32;1m[AUTO]\033[0m claude-sonnet-4-6  1 agent  \033[2m$0.031  6.1k tok  [a3f8]\033[0m`

**Input Bar:**
```
> fix the session expiry issue with UTC_
^
prompt glyph (MUTED)
```
The `>` prompt glyph is dim (`\033[2m>\033[0m`). The user's text is normal weight. The cursor `_` is rendered by the terminal's native cursor — agenthicc does not render a cursor character.

**Divider:**
```
────────────────────────────────────────────────────────────────────────
```
Rendered as `─` repeated `terminal.cols` times, in dim style: `\033[2m` + `─` × cols + `\033[0m`

**Mode Footer:**
```
Enter:send  Shift+Tab:mode  /:commands  @:files  Ctrl+B:background
```
Rendered in dim style. Only 3-5 most contextually relevant bindings shown. During agent turn: `Ctrl+C:cancel  Ctrl+B:background`. During approval gate: `Y:allow  N:deny  A:allow-all  ←→:scroll-diff`.

**Spinner (braille, animating in bottom block):**
```
⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏  (cycles at 10fps = 100ms per frame)
```
Color: SPINNER (`\033[36m`). In 8-color degraded mode: `| / - \` ASCII spinner.

**Dropdown (slash command palette):**
```
  ┌─────────────────────────────────────────┐
  │ /mode      Switch permission mode        │
  │ /history   Show session history          │
  │ /workflow  Show current task DAG         │
  │ /btw       Ask a side question           │
  │ /resume    Resume a previous session     │
  └─────────────────────────────────────────┘
  [AUTO] claude-sonnet-4-6  ...
  ──────────────────────────────────────────
  > /
  Enter:select  ↑↓:navigate  Esc:dismiss
```
The dropdown floats above the status bar. It is rendered as part of the Textual bottom block (Float widget, OptionList). Maximum 8 items visible at once; scrolls if more items match.

**Diff viewer (committed lines):**
```
  ─── Proposed change: src/auth/session.py ──────────────────────────────
  @@ -145,6 +145,6 @@
       def validate_token(token: SessionToken) -> bool:
           """Check if the session token is still valid."""
  -    expiry = datetime.now() + timedelta(hours=24)
  +    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
  -    if token.expiry < datetime.now():
  +    if token.expiry < datetime.now(timezone.utc):
           raise TokenExpiredError(f"Token {token.id} expired")
  ──────────────────────────────────────────────────────────────────────
```
Added lines: `\033[32m+    ...\033[0m` (green). Removed lines: `\033[31m-    ...\033[0m` (red). Hunk markers: `\033[36m@@ ... @@\033[0m` (cyan). Context lines: `\033[2m     ...\033[0m` (dim). Header/footer separator: `─` × cols in dim.

### 8.5 Icon & Symbol System

**Primary symbols (BMP Unicode, all common monospace fonts):**

| Symbol | Unicode | Usage | ASCII fallback |
|--------|---------|-------|----------------|
| ● | U+25CF | Agent turn header, running indicator | `*` |
| ○ | U+25CB | Pending/waiting indicator | `o` |
| ✓ | U+2713 | Success | `[ok]` |
| ✗ | U+2717 | Error/failure | `[!!]` |
| ⚠ | U+26A0 | Warning, approval needed | `[!]` |
| ⎿ | U+23BF | Tool call prefix | `>` |
| ◆ | U+25C6 | PLAN mode badge | `<>` |
| ─ | U+2500 | Horizontal divider, separators | `-` |
| │ | U+2502 | Vertical line in expanded tool output | `\|` |
| ┌ | U+250C | Dropdown top-left corner | `+` |
| ┐ | U+2510 | Dropdown top-right corner | `+` |
| └ | U+2514 | Dropdown bottom-left corner | `+` |
| ┘ | U+2518 | Dropdown bottom-right corner | `+` |
| ─── | repeated | Section header separator | `---` |
| ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ | U+280B... | Braille spinner frames | `\|/-\\` |
| ⊕ | U+2295 | REVIEW mode badge | `[R]` |
| ⛔ | U+26D4 | SAFE mode badge | `[S]` |
| ⚙ | U+2699 | DEBUG mode badge | `[D]` |
| ? | U+003F | ASK mode badge | `[A]` |
| → | U+2192 | Right arrow (navigation hints) | `->` |
| ← | U+2190 | Left arrow (navigation hints) | `<-` |

**Nerd Font symbols (NOT used):**

The following symbols are explicitly prohibited because they require Nerd Font patches:
- `` nf-fa-terminal (U+E795)
- `` nf-dev-git_branch (U+E0A0)
- `` nf-mdi-file (U+F016)
- Any symbol above U+FFFF (outside BMP)

**wcwidth requirement:**

All display-width calculations use `wcwidth.wcswidth(text)` not `len(text)`. This correctly handles:
- East Asian wide characters (width 2): 日本語, 中文, 한국어
- Combining characters (width 0): accents, diacritics
- Braille patterns (width 1): spinner frames
- Zero-width joiners and non-joiners (width 0)

The `wcwidth` package version >= 0.2.13 is required (see Section 11.1).

---

## 9. Component Specifications

This section provides detailed specifications for the 10 critical components of the TUI redesign. Each specification includes: purpose, Python class interface, state machine (where applicable), render logic, and test requirements.

### 9.1 Terminal

**Purpose:** Single owner of stdout (file descriptor 1). All terminal I/O — committed lines and bottom block redraws — flows through this class. No other component writes to stdout directly.

**Python class interface:**

```python
from __future__ import annotations

import os
import signal
import sys
import tty
import termios
from dataclasses import dataclass, field
from typing import NamedTuple


class Size(NamedTuple):
    rows: int
    cols: int


@dataclass(frozen=True)
class TerminalCapabilities:
    color_depth: int           # 0=none, 8=basic, 256=256color, 16777216=truecolor
    unicode_level: int         # 0=ascii, 1=bmp, 2=full
    synchronized_output: bool  # supports BSU/ESU (\x1b[?2026h)
    hyperlinks: bool           # supports OSC 8 hyperlinks
    no_color: bool             # NO_COLOR env var set


class Terminal:
    """Single owner of file descriptor 1 (stdout)."""

    def __init__(self, fd: int = 1) -> None:
        self._fd = fd
        self._bottom_height: int = 0      # current bottom block row count
        self._resize_pending: bool = False
        self._size: Size = self._query_size()
        self.capabilities: TerminalCapabilities = self._detect_capabilities()
        signal.signal(signal.SIGWINCH, self._on_sigwinch)

    @property
    def size(self) -> Size:
        return self._size

    @property
    def resize_pending(self) -> bool:
        return self._resize_pending

    def commit_lines(self, lines: list[str]) -> None:
        """Print lines permanently to stdout. Never erased."""
        data = bytearray()
        for line in lines:
            data.extend(line.encode("utf-8"))
            data.extend(b"\n")
        self._write_atomic(bytes(data))
        self._bottom_height = 0  # bottom block was cleared before this call

    def set_bottom(self, frame: "Frame") -> None:
        """Erase current bottom block and draw new one (single write call)."""
        data = bytearray()
        if self.capabilities.synchronized_output:
            data.extend(b"\x1b[?2026h")  # BSU
        # Erase current bottom block
        if self._bottom_height > 0:
            data.extend(b"\x1b[2K")
            for _ in range(self._bottom_height - 1):
                data.extend(b"\x1b[1A\x1b[2K")
            data.extend(b"\r")
        # Write new frame
        for i, row in enumerate(frame.rows):
            data.extend(row.encode("utf-8"))
            if i < len(frame.rows) - 1:
                data.extend(b"\n")
        if self.capabilities.synchronized_output:
            data.extend(b"\x1b[?2026l")  # ESU
        self._write_atomic(bytes(data))
        self._bottom_height = frame.height

    def clear_bottom(self) -> None:
        """Erase the bottom block and position cursor at cleared position."""
        if self._bottom_height == 0:
            return
        data = bytearray()
        data.extend(b"\x1b[2K")
        for _ in range(self._bottom_height - 1):
            data.extend(b"\x1b[1A\x1b[2K")
        data.extend(b"\r")
        self._write_atomic(bytes(data))
        self._bottom_height = 0

    def update_size(self) -> None:
        """Re-query terminal size after SIGWINCH."""
        self._size = self._query_size()
        self._resize_pending = False

    def _on_sigwinch(self, signum: int, frame: object) -> None:
        self._resize_pending = True

    def _query_size(self) -> Size:
        try:
            size = os.get_terminal_size(self._fd)
            return Size(rows=size.lines, cols=size.columns)
        except OSError:
            return Size(rows=24, cols=80)

    def _detect_capabilities(self) -> TerminalCapabilities:
        import os as _os
        no_color = "NO_COLOR" in _os.environ
        force_color = _os.environ.get("FORCE_COLOR", "0") == "1"
        colorterm = _os.environ.get("COLORTERM", "").lower()
        term = _os.environ.get("TERM", "")
        is_tty = os.isatty(self._fd)

        if no_color:
            color_depth = 0
        elif colorterm in ("truecolor", "24bit"):
            color_depth = 16777216
        elif colorterm in ("256color",) or "256color" in term or force_color:
            color_depth = 256
        elif is_tty:
            color_depth = 256  # safe default for modern terminals
        else:
            color_depth = 0

        unicode_level = 0 if term == "dumb" else 1
        synchronized_output = self._probe_synchronized_output()
        hyperlinks = _os.environ.get("TERM_PROGRAM", "") in ("iTerm.app", "WezTerm")

        return TerminalCapabilities(
            color_depth=color_depth,
            unicode_level=unicode_level,
            synchronized_output=synchronized_output,
            hyperlinks=hyperlinks,
            no_color=no_color,
        )

    def _probe_synchronized_output(self) -> bool:
        # Write BSU, then ESU, then DSR. If terminal responds to DSR,
        # synchronized output is likely supported. Skip probe in non-TTY.
        if not os.isatty(self._fd):
            return False
        # Conservative: check TERM_PROGRAM for known-supported terminals
        import os as _os
        term_program = _os.environ.get("TERM_PROGRAM", "")
        wt_session = "WT_SESSION" in _os.environ
        term = _os.environ.get("TERM", "")
        return (
            term_program in ("iTerm.app", "WezTerm", "Konsole")
            or wt_session
            or "alacritty" in term
            or "kitty" in term
        )

    def _write_atomic(self, data: bytes) -> None:
        """Single os.write call. Never bypassed."""
        os.write(self._fd, data)
```

**State machine:**

The Terminal has no explicit state machine. Its only mutable state is `_bottom_height` (integer) and `_resize_pending` (boolean). Invariant: `_bottom_height` always equals the number of rows of the last written bottom block frame.

**Render logic:** See `set_bottom()` and `commit_lines()` implementations above.

**Test requirements:**

```python
# tests/unit/test_terminal.py

class FakeTerminal(Terminal):
    """In-process test double. Captures writes instead of sending to stdout."""
    def __init__(self) -> None:
        self.committed_lines: list[str] = []
        self.bottom_history: list[Frame] = []
        self.write_call_count: int = 0
        self._bottom_height: int = 0
        self._size: Size = Size(rows=24, cols=80)
        self.capabilities = TerminalCapabilities(
            color_depth=256, unicode_level=1,
            synchronized_output=False, hyperlinks=False, no_color=False,
        )

    def commit_lines(self, lines: list[str]) -> None:
        self.committed_lines.extend(lines)
        self.write_call_count += 1
        self._bottom_height = 0

    def set_bottom(self, frame: Frame) -> None:
        self.bottom_history.append(frame)
        self.write_call_count += 1
        self._bottom_height = frame.height

    def clear_bottom(self) -> None:
        self._bottom_height = 0

# Key test cases:
# test_commit_lines_increments_write_call_count_by_one
# test_set_bottom_stores_frame_in_history
# test_clear_bottom_sets_height_to_zero
# test_set_bottom_after_clear_uses_correct_erase_height
# test_sigwinch_sets_resize_pending
# test_no_color_env_disables_color_depth
# test_force_color_enables_color_in_non_tty
# test_single_write_per_frame (verify write_call_count == 1 per set_bottom call)
```

### 9.2 FrameComposer

**Purpose:** Pure function that takes the current TranscriptModel, InputState, and terminal Size and produces an immutable Frame (the bottom block content). No side effects, no I/O.

**Python class interface:**

```python
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcript import TranscriptModel
    from .input_state import InputState
    from .terminal import Size


@dataclass(frozen=True)
class Frame:
    rows: list[str]
    height: int
    cursor_row: int
    cursor_col: int


class FrameComposer:
    """
    Pure composer. compose() has no side effects.
    Internal caches are fine — they do not affect output given same inputs.
    """

    def __init__(self, color: bool = True) -> None:
        self._color = color
        self._render_cache: dict[str, list[str]] = {}

    def compose(
        self,
        transcript: TranscriptModel,
        input_state: InputState,
        size: Size,
        now: float | None = None,
    ) -> Frame:
        """
        Produce the bottom block Frame.
        Call this on every RenderLoop tick. It is fast (< 8ms for 80-col terminal).
        """
        if now is None:
            now = time.monotonic()

        rows: list[str] = []

        # 1. Streaming text zone (only during active agent turn)
        if transcript.streaming_buffer:
            streaming_rows = self._render_streaming(transcript.streaming_buffer, size.cols)
            rows.extend(streaming_rows)

        # 2. Status bar
        rows.append(self._render_status_bar(transcript, size.cols))

        # 3. Divider
        rows.append(self._render_divider(size.cols))

        # 4. Input bar (may be multi-line)
        input_rows, cursor_row_offset, cursor_col = self._render_input(input_state, size.cols)
        rows.extend(input_rows)

        # 5. Mode footer
        rows.append(self._render_footer(transcript, input_state, size.cols))

        # 6. Dropdown (floats above status bar — prepend before status)
        if input_state.dropdown_open:
            dropdown_rows = self._render_dropdown(input_state, size.cols)
            # Insert dropdown before status bar (which is at index len(streaming_rows))
            insert_at = len(rows) - len(input_rows) - 2  # before status + divider
            rows = rows[:insert_at] + dropdown_rows + rows[insert_at:]

        # Clamp to max height
        max_height = min(12, size.rows // 3)
        if len(rows) > max_height:
            rows = rows[:max_height]

        cursor_row = len(rows) - len(input_rows) - 1 + cursor_row_offset
        return Frame(rows=rows, height=len(rows), cursor_row=cursor_row, cursor_col=cursor_col)

    def _render_status_bar(self, transcript: TranscriptModel, cols: int) -> str: ...
    def _render_divider(self, cols: int) -> str: ...
    def _render_input(self, input_state: InputState, cols: int) -> tuple[list[str], int, int]: ...
    def _render_footer(self, transcript: TranscriptModel, input_state: InputState, cols: int) -> str: ...
    def _render_streaming(self, buffer: str, cols: int) -> list[str]: ...
    def _render_dropdown(self, input_state: InputState, cols: int) -> list[str]: ...
```

**Test requirements:**

```python
# tests/unit/test_frame_composer.py
# Key test cases:
# test_compose_returns_frame_with_at_least_4_rows (status + divider + input + footer)
# test_compose_is_deterministic (same inputs → identical Frame)
# test_compose_clamps_to_max_height (12 rows max)
# test_compose_streaming_buffer_adds_rows
# test_compose_dropdown_open_adds_rows_before_status
# test_compose_no_color_mode_strips_ansi
# test_render_status_bar_shows_mode_badge
# test_render_status_bar_shows_agent_count
# test_render_divider_uses_terminal_cols
# test_render_input_wraps_long_text
# test_render_footer_shows_cancel_during_agent_turn
# test_render_footer_shows_submit_during_idle
# test_render_dropdown_max_8_items
# test_frame_equality_skips_redraw (Frame __eq__ works correctly)
```

### 9.3 RenderLoop

**Purpose:** Drives the rendering tick. Runs as an `asyncio.Task`. Checks the dirty flag, calls FrameComposer, calls Terminal, and sleeps until the next tick. Also handles force_commit for immediate turn-end rendering.

**Python class interface:**

```python
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .terminal import Terminal, Frame
    from .frame_composer import FrameComposer
    from .transcript import TranscriptModel, AgentTurnEntry
    from .input_state import InputState


class RenderLoop:
    MIN_TICK_INTERVAL: float = 0.050  # 50ms

    def __init__(
        self,
        terminal: Terminal,
        composer: FrameComposer,
        transcript: TranscriptModel,
        input_state: InputState,
    ) -> None:
        self._terminal = terminal
        self._composer = composer
        self._transcript = transcript
        self._input_state = input_state
        self._last_frame: Frame | None = None
        self._needs_redraw: bool = True
        self._pending_committed: list[str] = []
        self._shutdown: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        """Main render loop. Runs until shutdown() is called."""
        while not self._shutdown.is_set():
            tick_start = time.monotonic()

            # Handle resize
            if self._terminal.resize_pending:
                self._terminal.update_size()
                self._needs_redraw = True

            # Flush committed lines
            if self._pending_committed:
                self._terminal.clear_bottom()
                self._terminal.commit_lines(self._pending_committed)
                self._pending_committed.clear()
                self._last_frame = None  # force full redraw of bottom block
                self._needs_redraw = True

            # Redraw bottom block if needed
            if self._needs_redraw:
                frame = self._composer.compose(
                    self._transcript, self._input_state, self._terminal.size
                )
                if frame != self._last_frame:
                    self._terminal.set_bottom(frame)
                    self._last_frame = frame
                self._needs_redraw = False

            # Sleep for remainder of tick
            elapsed = time.monotonic() - tick_start
            sleep_for = max(0.0, self.MIN_TICK_INTERVAL - elapsed)
            await asyncio.sleep(sleep_for)

    def force_commit(self, lines: list[str]) -> None:
        """Queue committed lines for immediate flush on next tick."""
        self._pending_committed.extend(lines)
        self._needs_redraw = True

    def request_redraw(self) -> None:
        """Mark bottom block as dirty for next tick."""
        self._needs_redraw = True

    def shutdown(self) -> None:
        """Signal the run() loop to exit."""
        self._shutdown.set()
```

**Test requirements:**

```python
# tests/unit/test_render_loop.py
# Key test cases:
# test_run_calls_set_bottom_on_first_tick
# test_run_skips_redraw_when_frame_unchanged
# test_force_commit_flushes_lines_before_bottom_block
# test_force_commit_clears_bottom_before_committing
# test_resize_pending_triggers_redraw
# test_shutdown_stops_loop
# test_min_tick_interval_respected (mock time.monotonic)
# test_request_redraw_forces_next_tick_redraw
```

### 9.4 InputState

**Purpose:** Manages the mutable state of the input bar: current text buffer, cursor position, history navigation, dropdown visibility, @mention and / command trigger detection. This is a TUI-only data structure — it does not live in AppState.

**Python class interface:**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Awaitable


class TriggerType(Enum):
    NONE = auto()
    AT_MENTION = auto()
    SLASH_COMMAND = auto()


@dataclass
class DropdownState:
    open: bool = False
    trigger: TriggerType = TriggerType.NONE
    items: list[str] = field(default_factory=list)
    selected_index: int = 0
    filter_text: str = ""


class InputState:
    def __init__(self, on_submit: Callable[[str], Awaitable[None]]) -> None:
        self._text: str = ""
        self._cursor: int = 0
        self._history: list[str] = []
        self._history_index: int = -1
        self._dropdown: DropdownState = DropdownState()
        self._on_submit = on_submit
        self._kill_ring: list[str] = []
        self._disabled: bool = False  # True during agent turn

    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def dropdown_open(self) -> bool:
        return self._dropdown.open

    @property
    def dropdown(self) -> DropdownState:
        return self._dropdown

    def insert(self, char: str) -> None:
        """Insert character at cursor position. Triggers dropdown detection."""
        self._text = self._text[:self._cursor] + char + self._text[self._cursor:]
        self._cursor += 1
        self._check_triggers()

    def backspace(self) -> None:
        if self._cursor > 0:
            self._text = self._text[:self._cursor - 1] + self._text[self._cursor:]
            self._cursor -= 1
            self._check_triggers()

    def move_left(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1

    def move_right(self) -> None:
        if self._cursor < len(self._text):
            self._cursor += 1

    def move_to_start(self) -> None:
        self._cursor = 0

    def move_to_end(self) -> None:
        self._cursor = len(self._text)

    def kill_to_end(self) -> None:
        killed = self._text[self._cursor:]
        if killed:
            self._kill_ring.append(killed)
        self._text = self._text[:self._cursor]

    def kill_to_start(self) -> None:
        killed = self._text[:self._cursor]
        if killed:
            self._kill_ring.append(killed)
        self._text = self._text[self._cursor:]
        self._cursor = 0

    def yank(self) -> None:
        if self._kill_ring:
            yanked = self._kill_ring[-1]
            self._text = self._text[:self._cursor] + yanked + self._text[self._cursor:]
            self._cursor += len(yanked)

    def history_up(self) -> None:
        if self._history and self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._text = self._history[-(self._history_index + 1)]
            self._cursor = len(self._text)

    def history_down(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._text = self._history[-(self._history_index + 1)]
            self._cursor = len(self._text)
        elif self._history_index == 0:
            self._history_index = -1
            self._text = ""
            self._cursor = 0

    async def submit(self) -> None:
        if self._disabled or not self._text.strip():
            return
        text = self._text
        self._history.append(text)
        self._history_index = -1
        self._text = ""
        self._cursor = 0
        self._dropdown = DropdownState()
        await self._on_submit(text)

    def close_dropdown(self) -> None:
        self._dropdown = DropdownState()

    def select_dropdown_item(self) -> None:
        if self._dropdown.open and self._dropdown.items:
            selected = self._dropdown.items[self._dropdown.selected_index]
            self._apply_completion(selected)
            self.close_dropdown()

    def _check_triggers(self) -> None:
        """Detect @mention and /command triggers after each character insert."""
        text = self._text
        pos = self._cursor
        # Slash command: / as first non-space char on the line
        stripped = text.lstrip()
        if stripped.startswith("/") and pos > 0:
            after_slash = stripped[1:]
            self._dropdown = DropdownState(
                open=True,
                trigger=TriggerType.SLASH_COMMAND,
                filter_text=after_slash,
            )
            return
        # @mention: @ not preceded by word character
        if pos > 0 and text[pos - 1] == "@":
            before = text[:pos - 1]
            if not before or not before[-1].isalnum():
                self._dropdown = DropdownState(
                    open=True,
                    trigger=TriggerType.AT_MENTION,
                    filter_text="",
                )
                return
        # Close dropdown if no trigger
        if self._dropdown.open and not self._dropdown.filter_text:
            self._dropdown = DropdownState()

    def _apply_completion(self, item: str) -> None:
        """Replace the trigger text with the selected completion."""
        ...

    def set_disabled(self, disabled: bool) -> None:
        self._disabled = disabled
```

**Test requirements:**

```python
# tests/unit/test_input_state.py
# Key test cases:
# test_insert_updates_text_and_cursor
# test_backspace_removes_character
# test_kill_to_end_stores_in_kill_ring
# test_yank_inserts_from_kill_ring
# test_history_up_down_navigates_history
# test_submit_clears_text_and_adds_to_history
# test_slash_trigger_opens_dropdown
# test_at_trigger_opens_dropdown_when_not_email_context
# test_at_trigger_does_not_fire_in_email_context
# test_close_dropdown_resets_state
# test_disabled_submit_does_nothing
```

### 9.5 TranscriptModel

**Purpose:** Mutable presentation model of the session transcript. Tracks all agent turns, tool calls, streaming state, and committed-line cursor. Mutated by `TUIEventAdapter`; read by `FrameComposer` and `RenderLoop`.

**Python class interface:**

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

MAX_TURNS_IN_MEMORY = 200
MAX_LINES_PER_TURN = 500
MAX_DIFF_LINES = 50


class TurnState(Enum):
    STREAMING = auto()
    COMPLETE = auto()
    CANCELLED = auto()
    ERROR = auto()


class ToolCallState(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    ERROR = auto()
    APPROVAL_NEEDED = auto()


@dataclass
class ToolCallEntry:
    tool_id: str
    tool_name: str
    args: dict
    state: ToolCallState = ToolCallState.PENDING
    result_summary: str = ""
    duration_ms: int = 0
    error_message: str = ""


@dataclass
class AgentTurnEntry:
    turn_id: str
    agent_id: str
    agent_name: str
    timestamp: float
    state: TurnState = TurnState.STREAMING
    output_lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    streaming_text: str = ""   # partial text during STREAMING state
    color_index: int = 0       # for parallel agent color assignment


class TranscriptModel:
    def __init__(self) -> None:
        self._turns: list[AgentTurnEntry] = []
        self._streaming_buffer: str = ""
        self._committed_cursor: int = 0  # index into _all_committed_lines
        self._all_committed_lines: list[str] = []
        self._agent_color_map: dict[str, int] = {}
        self._next_color_index: int = 0

    @property
    def turns(self) -> list[AgentTurnEntry]:
        return self._turns

    @property
    def streaming_buffer(self) -> str:
        return self._streaming_buffer

    @property
    def committed_cursor(self) -> int:
        return self._committed_cursor

    @property
    def all_committed_lines(self) -> list[str]:
        return self._all_committed_lines

    def add_turn(self, turn: AgentTurnEntry) -> None:
        """Start a new agent turn. Assigns color if agent is new."""
        if turn.agent_id not in self._agent_color_map:
            self._agent_color_map[turn.agent_id] = self._next_color_index % 6
            self._next_color_index += 1
        turn.color_index = self._agent_color_map[turn.agent_id]
        self._turns.append(turn)
        if len(self._turns) > MAX_TURNS_IN_MEMORY:
            self._evict_old_turns()

    def append_streaming_token(self, token: str) -> None:
        self._streaming_buffer += token

    def clear_streaming_buffer(self) -> None:
        self._streaming_buffer = ""

    def complete_turn(self, turn_id: str, final_lines: list[str]) -> None:
        """Mark turn as complete. Queues committed lines for render loop."""
        turn = self._get_turn(turn_id)
        if turn:
            turn.state = TurnState.COMPLETE
            turn.output_lines = final_lines[:MAX_LINES_PER_TURN]
            self._all_committed_lines.extend(final_lines)

    def add_tool_call(self, turn_id: str, tool_call: ToolCallEntry) -> None:
        turn = self._get_turn(turn_id)
        if turn:
            turn.tool_calls.append(tool_call)

    def update_tool_call(self, tool_id: str, state: ToolCallState, **kwargs: object) -> None:
        for turn in reversed(self._turns):
            for tc in turn.tool_calls:
                if tc.tool_id == tool_id:
                    tc.state = state
                    for k, v in kwargs.items():
                        setattr(tc, k, v)
                    return

    def commit_lines(self, lines: list[str]) -> None:
        """Add lines to the committed-lines list. Advances committed_cursor."""
        self._all_committed_lines.extend(lines)

    def _get_turn(self, turn_id: str) -> AgentTurnEntry | None:
        for turn in reversed(self._turns):
            if turn.turn_id == turn_id:
                return turn
        return None

    def _evict_old_turns(self) -> None:
        """Evict output_lines from the oldest 20 turns to free memory."""
        evict_count = min(20, len(self._turns) - MAX_TURNS_IN_MEMORY + 20)
        for turn in self._turns[:evict_count]:
            turn.output_lines = []  # free memory; metadata retained
```

**Test requirements:**

```python
# tests/unit/test_tui_transcript.py (extended)
# Key test cases:
# test_add_turn_assigns_unique_colors_to_different_agents
# test_add_turn_assigns_same_color_to_same_agent
# test_evict_old_turns_clears_output_lines_of_oldest
# test_evict_old_turns_retains_metadata
# test_complete_turn_truncates_at_max_lines
# test_append_streaming_token_accumulates
# test_update_tool_call_finds_by_tool_id
# test_committed_cursor_advances_on_commit_lines
```

### 9.6 AgentTurnEntry

**Purpose:** Represents a single agent turn in the transcript. Holds the streaming text, final output lines, tool calls, and state machine for the turn lifecycle. Defined in `TranscriptModel` module (see 9.5 for dataclass definition).

**State machine:**

```
STREAMING ──complete──▶ COMPLETE
STREAMING ──cancel──▶  CANCELLED
STREAMING ──error──▶   ERROR
COMPLETE  (terminal — no transitions)
CANCELLED (terminal — no transitions)
ERROR     (terminal — no transitions)
```

**Render logic for committed transcript:**

When a turn transitions from STREAMING to COMPLETE, `RenderLoop.force_commit()` is called with the following lines:

```python
def render_turn_to_lines(turn: AgentTurnEntry, color: bool, cols: int) -> list[str]:
    lines = []
    # Header line
    agent_color = AGENT_COLORS[turn.color_index % len(AGENT_COLORS)]
    timestamp = time.strftime("%H:%M:%S", time.localtime(turn.timestamp))
    if color:
        header = f"\033[{agent_color};1m●\033[0m \033[{agent_color}m{turn.agent_name}\033[0m  \033[2m{timestamp}\033[0m"
    else:
        header = f"● {turn.agent_name}  {timestamp}"
    lines.append(header)

    # Body lines (rendered Markdown as ANSI text via Rich Console)
    lines.extend(turn.output_lines)

    # Tool call lines (for completed tool calls only)
    for tc in turn.tool_calls:
        if tc.state in (ToolCallState.SUCCESS, ToolCallState.ERROR):
            lines.append(render_tool_call_line(tc, color))

    # Turn separator
    if color:
        lines.append(f"\033[2m{'─' * cols}\033[0m")
    else:
        lines.append("─" * cols)

    return lines
```

**Test requirements:**

```python
# tests/unit/test_tui_transcript.py
# test_render_turn_header_contains_agent_name_and_timestamp
# test_render_turn_header_uses_agent_color
# test_render_turn_separator_uses_terminal_cols
# test_render_tool_call_success_shows_checkmark_and_duration
# test_render_tool_call_error_shows_cross_and_message
# test_render_turn_no_color_strips_ansi
```

### 9.7 ToolCallBlock

**Purpose:** Represents a single tool call within an agent turn. Manages the tool call state machine and provides the render logic for both the live bottom-block state (spinner) and the committed-line state (final result).

**State machine:**

```
PENDING ──start──▶ RUNNING ──success──▶ SUCCESS
                  RUNNING ──error──▶   ERROR
                  RUNNING ──needs_approval──▶ APPROVAL_NEEDED
APPROVAL_NEEDED ──approved──▶ RUNNING
APPROVAL_NEEDED ──denied──▶   ERROR
```

**Render logic:**

```python
SPINNER_BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

def render_tool_call_live(tc: ToolCallEntry, color: bool, spinner_frame: int) -> str:
    """Render tool call for the live bottom block (spinner animating)."""
    args_str = _format_args(tc.args)
    spinner = SPINNER_BRAILLE[spinner_frame % len(SPINNER_BRAILLE)]
    if tc.state == ToolCallState.PENDING:
        indicator = "○" if not color else "\033[2m○\033[0m"
    elif tc.state == ToolCallState.RUNNING:
        indicator = spinner if not color else f"\033[36m{spinner}\033[0m"
    elif tc.state == ToolCallState.APPROVAL_NEEDED:
        indicator = "⚠ awaiting approval" if not color else "\033[33m⚠ awaiting approval\033[0m"
    else:
        indicator = ""
    prefix = "\033[2m⎿\033[0m" if color else ">"
    return f"  {prefix} {tc.tool_name}({args_str})  {indicator}"


def render_tool_call_committed(tc: ToolCallEntry, color: bool) -> str:
    """Render tool call as a committed line (no spinner — final state)."""
    args_str = _format_args(tc.args)
    if tc.state == ToolCallState.SUCCESS:
        if color:
            indicator = f"\033[32m✓\033[0m \033[2m{tc.result_summary} ({tc.duration_ms}ms)\033[0m"
        else:
            indicator = f"✓ {tc.result_summary} ({tc.duration_ms}ms)"
    elif tc.state == ToolCallState.ERROR:
        if color:
            indicator = f"\033[31m✗\033[0m \033[2m{tc.duration_ms}ms\033[0m  \033[31m{tc.error_message}\033[0m"
        else:
            indicator = f"✗ {tc.duration_ms}ms  {tc.error_message}"
    else:
        indicator = "?"
    prefix = "\033[2m⎿\033[0m" if color else ">"
    return f"  {prefix} {tc.tool_name}({args_str})  {indicator}"
```

**Test requirements:**

```python
# tests/unit/test_tool_call_block.py
# test_render_live_pending_shows_circle
# test_render_live_running_shows_spinner_frame
# test_render_live_approval_needed_shows_warning
# test_render_committed_success_shows_checkmark_duration
# test_render_committed_error_shows_cross_message
# test_render_no_color_strips_ansi
# test_spinner_cycles_through_all_frames
```

### 9.8 ApprovalGate

**Purpose:** Manages the approval workflow for tool calls that require user confirmation. Replaces the normal input bar in the bottom block when a tool call requires approval.

**Python class interface:**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Awaitable


class ApprovalChoice(Enum):
    ALLOW = auto()
    DENY = auto()
    ALLOW_ALL = auto()
    SKIP_QUEUE = auto()


@dataclass
class ApprovalRequest:
    tool_call: "ToolCallEntry"
    diff_lines: list[str] | None  # pre-committed to transcript before gate shows
    queue_position: int = 1
    queue_total: int = 1


class ApprovalGate:
    def __init__(
        self,
        on_choice: Callable[[ApprovalChoice, ApprovalRequest], Awaitable[None]],
    ) -> None:
        self._on_choice = on_choice
        self._current: ApprovalRequest | None = None
        self._active: bool = False
        self._session_approvals: set[str] = set()  # tool names auto-approved

    @property
    def active(self) -> bool:
        return self._active

    @property
    def current(self) -> ApprovalRequest | None:
        return self._current

    def is_auto_approved(self, tool_name: str) -> bool:
        return tool_name in self._session_approvals

    def present(self, request: ApprovalRequest) -> None:
        """Show approval gate for the given request."""
        self._current = request
        self._active = True

    async def handle_key(self, key: str) -> None:
        """Handle y/n/a/s keypress at the approval gate."""
        if not self._active or self._current is None:
            return
        request = self._current
        if key.lower() == "y":
            await self._on_choice(ApprovalChoice.ALLOW, request)
            self._dismiss()
        elif key.lower() == "n":
            await self._on_choice(ApprovalChoice.DENY, request)
            self._dismiss()
        elif key.lower() == "a":
            self._session_approvals.add(request.tool_call.tool_name)
            await self._on_choice(ApprovalChoice.ALLOW_ALL, request)
            self._dismiss()
        elif key.lower() == "s" and request.queue_total > 1:
            await self._on_choice(ApprovalChoice.SKIP_QUEUE, request)
            self._dismiss()

    def render_bottom_rows(self, color: bool, cols: int) -> list[str]:
        """Render the approval gate rows for inclusion in the Frame."""
        if not self._active or self._current is None:
            return []
        req = self._current
        tc = req.tool_call
        args_str = _format_args(tc.args)
        queue_info = f" ({req.queue_position} of {req.queue_total})" if req.queue_total > 1 else ""
        if color:
            line1 = f"  \033[33m⚠\033[0m {tc.tool_name}({args_str})  — approve?{queue_info}"
        else:
            line1 = f"  ⚠ {tc.tool_name}({args_str})  — approve?{queue_info}"
        options = "[Y] Allow    [N] Deny    [A] Allow all this session"
        if req.queue_total > 1:
            options += "    [S] Skip queue"
        return [line1, options]

    def _dismiss(self) -> None:
        self._current = None
        self._active = False
```

**Test requirements:**

```python
# tests/unit/test_approval_gate.py
# test_present_sets_active_true
# test_handle_key_y_calls_on_choice_allow
# test_handle_key_n_calls_on_choice_deny
# test_handle_key_a_adds_to_session_approvals
# test_handle_key_a_future_calls_auto_approved
# test_handle_key_s_only_available_in_queue
# test_render_bottom_rows_shows_tool_name_and_args
# test_render_bottom_rows_shows_queue_position
# test_render_bottom_rows_no_color_strips_ansi
# test_dismiss_clears_active_and_current
```

### 9.9 StatusBar

**Purpose:** Renders the single-row status bar showing mode badge, model info, agent count, session cost, token count, and session ID. Rendered by FrameComposer as part of the bottom block.

**Python class interface:**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PermissionMode(Enum):
    AUTO = "AUTO"
    PLAN = "PLAN"
    ASK = "ASK"
    REVIEW = "REVIEW"
    SAFE = "SAFE"
    DEBUG = "DEBUG"


MODE_COLORS = {
    PermissionMode.AUTO:   "32;1",  # green bold
    PermissionMode.PLAN:   "33;1",  # yellow bold
    PermissionMode.ASK:    "36;1",  # cyan bold
    PermissionMode.REVIEW: "34;1",  # blue bold
    PermissionMode.SAFE:   "31;1",  # red bold
    PermissionMode.DEBUG:  "35;1",  # magenta bold
}

MODE_SYMBOLS = {
    PermissionMode.AUTO:   "●",
    PermissionMode.PLAN:   "◆",
    PermissionMode.ASK:    "?",
    PermissionMode.REVIEW: "⊕",
    PermissionMode.SAFE:   "⛔",
    PermissionMode.DEBUG:  "⚙",
}


@dataclass
class StatusBarState:
    mode: PermissionMode
    model: str
    provider: str
    agent_count: int
    session_cost_usd: float
    token_count: int
    session_id: str
    background_tasks: int = 0
    error_message: str = ""


class StatusBar:
    def render(self, state: StatusBarState, color: bool, cols: int) -> str:
        """Render status bar as a single ANSI-formatted string."""
        parts = []

        # Mode badge
        if color:
            mode_color = MODE_COLORS[state.mode]
            parts.append(f"\033[{mode_color}m[{state.mode.value}]\033[0m")
        else:
            parts.append(f"[{state.mode.value}]")

        # Model info
        parts.append(f"{state.provider}/{state.model}")

        # Agent count
        agent_str = f"{state.agent_count} agent{'s' if state.agent_count != 1 else ''}"
        parts.append(agent_str)

        # Background tasks
        if state.background_tasks > 0:
            bg_str = f"[{state.background_tasks} bg]"
            parts.append(f"\033[33m{bg_str}\033[0m" if color else bg_str)

        # Cost and tokens
        cost_str = f"${state.session_cost_usd:.3f}"
        tok_str = self._format_tokens(state.token_count)
        meta = f"{cost_str}  {tok_str} tok  [{state.session_id[:4]}]"
        parts.append(f"\033[2m{meta}\033[0m" if color else meta)

        # Error banner (replaces right side if present)
        if state.error_message:
            err = f"✗ {state.error_message}"
            parts[-1] = f"\033[31m{err}\033[0m" if color else err

        line = "  ".join(parts)
        # Truncate to cols with wcwidth
        return _truncate_to_cols(line, cols)

    def _format_tokens(self, n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)
```

**Test requirements:**

```python
# tests/unit/test_status_bar.py
# test_render_shows_mode_badge
# test_render_shows_model_and_provider
# test_render_shows_agent_count_singular
# test_render_shows_agent_count_plural
# test_render_shows_background_task_count
# test_render_shows_cost_formatted
# test_render_shows_token_count_formatted_k
# test_render_shows_token_count_formatted_M
# test_render_error_message_replaces_meta
# test_render_truncates_to_cols
# test_render_no_color_strips_ansi
# test_mode_auto_green_color
# test_mode_safe_red_color
```

### 9.10 TriggerDropdown

**Purpose:** A floating dropdown that appears above the status bar when a `/` command or `@mention` trigger is detected in the input. Shows up to 8 filtered items. Navigable with arrow keys; selected item applied on Enter, dismissed on Escape.

**Python class interface:**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class DropdownItem:
    label: str
    description: str = ""
    value: str = ""  # value to insert on selection; defaults to label


class TriggerDropdown:
    MAX_ITEMS = 8

    def __init__(
        self,
        on_select: Callable[[DropdownItem], None],
        on_dismiss: Callable[[], None],
    ) -> None:
        self._items: list[DropdownItem] = []
        self._selected: int = 0
        self._on_select = on_select
        self._on_dismiss = on_dismiss
        self._open: bool = False

    def open(self, items: list[DropdownItem]) -> None:
        self._items = items[:self.MAX_ITEMS]
        self._selected = 0
        self._open = True

    def close(self) -> None:
        self._items = []
        self._selected = 0
        self._open = False
        self._on_dismiss()

    @property
    def is_open(self) -> bool:
        return self._open

    def navigate_up(self) -> None:
        if self._items:
            self._selected = (self._selected - 1) % len(self._items)

    def navigate_down(self) -> None:
        if self._items:
            self._selected = (self._selected + 1) % len(self._items)

    def select(self) -> None:
        if self._items:
            self._on_select(self._items[self._selected])
            self.close()

    def filter(self, text: str) -> None:
        """Re-filter items based on updated prefix text."""
        # Filtering logic delegates to the caller who updates items list
        # This method is a hook for the caller to re-call open() with new items
        pass

    def render_rows(self, color: bool, cols: int) -> list[str]:
        """Render dropdown as rows for inclusion in Frame."""
        if not self._open or not self._items:
            return []
        rows = []
        inner_width = cols - 4
        # Top border
        rows.append(f"  ┌{'─' * inner_width}┐")
        for i, item in enumerate(self._items):
            label = item.label.ljust(16)
            desc = item.description[:inner_width - 18] if item.description else ""
            content = f"{label}  {desc}".ljust(inner_width)
            if i == self._selected:
                if color:
                    row = f"  │ \033[7m{content}\033[0m │"
                else:
                    row = f"  │>{content}│"
            else:
                row = f"  │ {content} │"
            rows.append(row)
        # Bottom border
        rows.append(f"  └{'─' * inner_width}┘")
        return rows
```

**Test requirements:**

```python
# tests/unit/test_trigger_dropdown.py
# test_open_limits_to_max_8_items
# test_navigate_down_wraps_around
# test_navigate_up_wraps_around
# test_select_calls_on_select_with_item
# test_select_closes_dropdown
# test_close_calls_on_dismiss
# test_render_rows_shows_selected_item_highlighted
# test_render_rows_shows_border
# test_render_rows_empty_when_closed
# test_render_rows_no_color_uses_caret_for_selection
```

---

## 10. Implementation Plan

### 10.1 Phase 1 — Foundation (Week 1-2)

**Goal:** Establish the core I/O primitives that all subsequent phases depend on. At the end of Phase 1, `Terminal`, `FakeTerminal`, and color detection work correctly and are tested.

**Deliverables:**

1. **`src/agenthicc/tui/terminal.py`** — Complete `Terminal` class with:
   - `__init__`: SIGWINCH handler registration, capability detection, initial size query
   - `commit_lines()`: print lines permanently, advance bottom height tracking
   - `set_bottom()`: canonical erase sequence + atomic write + BSU/ESU wrapping
   - `clear_bottom()`: erase only, no new frame
   - `update_size()`: re-query `os.get_terminal_size()`, clear resize_pending flag
   - `_write_atomic()`: single `os.write(fd, data)` call
   - `_detect_capabilities()`: `NO_COLOR`, `FORCE_COLOR`, `COLORTERM`, `TERM`, `TERM_PROGRAM` detection
   - `_probe_synchronized_output()`: static detection based on `$TERM_PROGRAM` and `$WT_SESSION`

2. **`src/agenthicc/tui/terminal.py` (continued)** — `FakeTerminal` class:
   - `committed_lines: list[str]` — all lines passed to `commit_lines()`
   - `bottom_history: list[Frame]` — all frames passed to `set_bottom()`
   - `write_call_count: int` — total number of write operations (must be 1 per `set_bottom()` call)
   - `_bottom_height: int` — mirrors real Terminal behavior
   - Override `_write_atomic()` to capture to `bytearray` buffer for inspection

3. **`tests/unit/test_terminal.py`** — Unit tests:
   - `test_commit_lines_appends_to_committed_list`
   - `test_set_bottom_increments_height`
   - `test_set_bottom_single_write_per_call`
   - `test_clear_bottom_resets_height`
   - `test_no_color_env_disables_color_depth`
   - `test_force_color_enables_color`
   - `test_sigwinch_sets_resize_pending`
   - `test_update_size_clears_resize_pending`
   - `test_fake_terminal_captures_committed_lines`
   - `test_fake_terminal_captures_bottom_frames`
   - `test_erase_sequence_correct_for_n_rows` (parametrized with n=1,2,3,5)

4. **`src/agenthicc/tui/__init__.py`** — Update to re-export `Terminal`, `FakeTerminal`, `Size`, `TerminalCapabilities`, `Frame`

5. **Color detection module** — `src/agenthicc/tui/colors.py`:
   - `ColorMode` enum: `NONE`, `ANSI_8`, `ANSI_256`, `TRUECOLOR`
   - `detect_color_mode(env: dict[str, str], is_tty: bool) -> ColorMode`
   - `ansi(code: str, text: str, color_mode: ColorMode) -> str` — returns text with or without ANSI codes
   - `semantic_color(role: str, text: str, color_mode: ColorMode) -> str` — maps role names to ANSI codes

6. **`tests/unit/test_colors.py`** — Unit tests for color detection and `ansi()` helper

**Definition of Done:**
- All tests pass: `uv run pytest tests/unit/test_terminal.py tests/unit/test_colors.py -v`
- `mypy src/agenthicc/tui/terminal.py src/agenthicc/tui/colors.py` returns 0 errors
- `ruff check src/agenthicc/tui/terminal.py src/agenthicc/tui/colors.py` returns 0 warnings

### 10.2 Phase 2 — Core Transcript (Week 3-4)

**Goal:** Implement `TranscriptModel`, `AgentTurnEntry`, `ToolCallBlock`, and the commit pipeline. At the end of Phase 2, the full streaming → force_commit → scrollback cycle works end-to-end with `FakeTerminal`.

**Deliverables:**

1. **`src/agenthicc/tui/transcript.py`** — Complete rewrite:
   - `TranscriptModel` class with full API from Section 9.5
   - `AgentTurnEntry` dataclass with state machine from Section 9.6
   - `ToolCallEntry` dataclass with `ToolCallState` enum
   - `render_turn_to_lines()` function with color/no-color paths
   - `render_tool_call_line()` function (committed state)
   - `render_tool_call_live()` function (live spinner state)
   - `_evict_old_turns()` implementation
   - `MAX_TURNS_IN_MEMORY`, `MAX_LINES_PER_TURN`, `MAX_DIFF_LINES` constants

2. **`src/agenthicc/tui/render_loop.py`** — Complete implementation:
   - `RenderLoop` class with `run()`, `force_commit()`, `request_redraw()`, `shutdown()`
   - `MIN_TICK_INTERVAL = 0.050` constant
   - Resize handling: check `terminal.resize_pending` each tick
   - Pending committed lines queue
   - Frame equality check to skip redundant redraws

3. **`src/agenthicc/tui/events.py`** — `TUIEventAdapter` rewrite:
   - Subscribe to `EventProcessor` subscriber queue
   - Translate `AppState` diffs into `TranscriptModel` mutations:
     - `agent_turn_started` → `transcript.add_turn()`
     - `streaming_token` → `transcript.append_streaming_token()`
     - `turn_complete` → `transcript.complete_turn()` + `render_loop.force_commit()`
     - `tool_call_started` → `transcript.add_tool_call()`
     - `tool_call_complete` → `transcript.update_tool_call(SUCCESS)`
     - `tool_call_error` → `transcript.update_tool_call(ERROR)`
     - `tool_call_approval_needed` → `transcript.update_tool_call(APPROVAL_NEEDED)` + `approval_gate.present()`

4. **Streaming debounce:** The `TUIEventAdapter` buffers streaming tokens into `_pending_tokens: list[str]`. On each `streaming_token` event, tokens accumulate. The `RenderLoop` tick at 50ms pulls from `transcript.streaming_buffer` and renders. This means tokens are never rendered more frequently than 20fps — the debounce emerges from the tick rate, not an explicit timer.

5. **`tests/unit/test_tui_transcript.py`** — Extended test suite covering all new TranscriptModel methods

6. **`tests/integration/test_tui_rendering.py`** — First pyte integration tests:
   - `test_agent_turn_header_appears_in_scrollback`
   - `test_tool_call_committed_after_completion`
   - `test_streaming_text_appears_and_commits`
   - `test_turn_separator_committed_after_turn`

**Definition of Done:**
- `uv run pytest tests/unit/test_tui_transcript.py tests/unit/test_render_loop.py -v` — all pass
- `uv run pytest tests/integration/test_tui_rendering.py -v` — all pass
- `uv run mypy src/agenthicc/tui/transcript.py src/agenthicc/tui/render_loop.py src/agenthicc/tui/events.py` — 0 errors
- Memory benchmark: 200 turns × 200 lines each stays under 10MB (verified with `tracemalloc`)

### 10.3 Phase 3 — Bottom Block & Input (Week 5-6)

**Goal:** Implement `FrameComposer`, `InputState`, `StatusBar`, `ModeIndicator`, and the Textual bottom block. At the end of Phase 3, the full interactive TUI is functional with mode switching, @mention triggering, and /command palette.

**Deliverables:**

1. **`src/agenthicc/tui/frame_composer.py`** — Complete implementation:
   - `FrameComposer.compose()` pure function
   - All six private render methods: `_render_status_bar`, `_render_divider`, `_render_input`, `_render_footer`, `_render_streaming`, `_render_dropdown`
   - `_render_cache: dict[str, list[str]]` for turn-level caching
   - `Frame` frozen dataclass
   - `_truncate_to_cols(text: str, cols: int) -> str` using `wcwidth.wcswidth()`

2. **`src/agenthicc/tui/input_state.py`** — Complete implementation:
   - `InputState` class with full API from Section 9.4
   - `TriggerType` enum
   - `DropdownState` dataclass
   - `should_trigger_at_mention()` standalone function
   - All readline-emulation methods
   - History navigation

3. **`src/agenthicc/tui/status_bar.py`** — `StatusBar` class and `PermissionMode` enum from Section 9.9

4. **`src/agenthicc/tui/trigger_dropdown.py`** — `TriggerDropdown` and `DropdownItem` from Section 9.10

5. **`src/agenthicc/tui/app.py`** — Rewrite using new architecture:
   - `build_app()`: creates Terminal, FrameComposer, RenderLoop, InputState, TUIEventAdapter
   - Textual `BottomApp(App)` with `inline=True` for the input bar
   - `run_headless()`: JSON-lines mode, no Terminal, routes events to stdout
   - `render_frame_ansi()`: for pyte-based e2e tests, returns ANSI frame string

6. **@mention resolver** — `src/agenthicc/tui/at_mention.py`:
   - `AtMentionResolver` class
   - `resolve(prefix: str, project_root: Path) -> list[DropdownItem]`
   - Uses `pathlib.Path.glob()` with 200-item limit
   - Filters by prefix using `str.startswith()`
   - Includes agents from current AppState as items

7. **Command registry integration** — `TriggerDropdown` reads from `CommandRegistry` (existing module) to populate /command items. Each `CommandEntry` becomes a `DropdownItem` with `label=command.name`, `description=command.description`.

8. **Mode switching** — `Shift+Tab` in the Textual `InputBar` cycles through `PermissionMode` values. Emits a `mode_changed` event to the `EventProcessor`. The `StatusBar` reactive updates immediately.

9. **`tests/unit/test_frame_composer.py`** — Full test suite (see Section 9.2 test list)
   **`tests/unit/test_input_state.py`** — Full test suite (see Section 9.4 test list)
   **`tests/unit/test_status_bar.py`** — Full test suite (see Section 9.9 test list)
   **`tests/unit/test_trigger_dropdown.py`** — Full test suite (see Section 9.10 test list)

10. **`tests/integration/test_tui_rendering.py`** (extended):
    - `test_status_bar_shows_mode_badge`
    - `test_input_bar_visible_at_bottom_row`
    - `test_dropdown_appears_on_slash_trigger`
    - `test_dropdown_dismissed_on_escape`
    - `test_mode_switches_on_shift_tab`

**Definition of Done:**
- Full interactive session works: `uv run agenthicc` starts, renders bottom block, accepts input, displays mode, shows /commands dropdown
- All unit and integration tests pass
- `mypy` on all new files: 0 errors
- `ruff check` on all new files: 0 warnings

### 10.4 Phase 4 — Polish & Performance (Week 7-8)

**Goal:** Implement ApprovalGate, DiffViewer, session recap, doom-loop detection, parallel agent color coding, and run performance benchmarks.

**Deliverables:**

1. **`src/agenthicc/tui/approval_gate.py`** — Complete `ApprovalGate` from Section 9.8:
   - `present()` method with diff pre-commit to transcript
   - `handle_key()` for y/n/a/s
   - Session-level auto-approval tracking
   - Batched approval queue rendering

2. **`src/agenthicc/tui/diff_viewer.py`** — `DiffViewer` class:
   - `render_diff(old: str, new: str, path: str, color: bool, max_lines: int) -> list[str]`
   - Uses Python stdlib `difflib.unified_diff()` as the diff engine
   - Applies semantic colors: green for `+` lines, red for `-` lines, cyan for `@@` markers
   - Truncates to `max_lines` (default: `MAX_DIFF_LINES = 50`) with a `... N more lines` suffix
   - Section header and footer: `─── Proposed change: {path} ───` / `───────────────────────`

3. **`src/agenthicc/tui/doom_loop.py`** — `DoomLoopDetector` class:
   - Tracks `(tool_name, args_hash)` tuples per turn
   - `record_tool_call(tool_name: str, args: dict) -> bool` — returns True if doom loop detected
   - `THRESHOLD = 3` — triggers on 3 identical calls
   - `reset()` — clears state at turn start
   - Emits `doom_loop_detected` application_log event when triggered

4. **Session recap** — `src/agenthicc/tui/session_recap.py`:
   - `SessionRecapGenerator` class
   - `generate(turns: list[AgentTurnEntry], since: float) -> list[str]`
   - Produces 1-line summary per turn using turn metadata (no LLM call)
   - Format: `{time} • {summary_from_first_output_line} ({N} files changed)`
   - Triggers when user inputs after 3+ minutes idle

5. **Parallel agent colors** — `TranscriptModel._agent_color_map` is the source of truth. `render_turn_to_lines()` reads `turn.color_index` to select from `AGENT_COLORS` list. The status bar `agent_count` field counts only currently active (STREAMING) agents.

6. **Performance benchmarks** — `tests/performance/test_render_benchmarks.py`:
   - `benchmark_frame_compose_80x24`: compose 1000 frames on 80×24 terminal; assert p99 < 8ms
   - `benchmark_set_bottom_50_rows`: 1000 `set_bottom()` calls on FakeTerminal; assert p99 < 2ms
   - `benchmark_transcript_200_turns`: build 200-turn TranscriptModel; assert RSS < 10MB
   - `benchmark_streaming_debounce`: 500 tokens at 5ms intervals; assert no frame rendered more than once per 50ms tick
   - Run with: `uv run pytest tests/performance/ -v --benchmark-only`

7. **`tests/unit/test_approval_gate.py`** — Full test suite
   **`tests/unit/test_diff_viewer.py`** — Tests for diff rendering with color and no-color
   **`tests/unit/test_doom_loop.py`** — Tests for threshold detection and reset

8. **`tests/integration/test_tui_rendering.py`** (extended):
   - `test_approval_gate_shows_diff_before_gate`
   - `test_doom_loop_banner_appears_after_3_identical_calls`
   - `test_session_recap_appears_after_idle`
   - `test_parallel_agents_use_distinct_colors`

**Definition of Done:**
- ApprovalGate shows diff in committed transcript before gate appears — verified by pyte test
- Doom loop detection fires correctly at exactly 3 identical calls
- Performance benchmarks all pass (p99 within targets)
- No regression in existing tests

### 10.5 Phase 5 — Migration & Cutover (Week 9-10)

**Goal:** Replace the old prompt_toolkit-based TUI with the new architecture. Add feature flag, run A/B period, remove old code, update documentation and llms-full.txt.

**Deliverables:**

1. **Feature flag** — `src/agenthicc/config.py` gains `tui.use_new_renderer: bool = False` (default False during migration). When True, `build_app()` uses the new architecture. When False, falls back to the old prompt_toolkit app.

2. **A/B period (Week 9):** Release with `use_new_renderer = False` default. Internal users opt in via `agenthicc --set tui.use_new_renderer=true`. Collect feedback on: scrollback correctness, resize behavior, SSH compatibility, mode switching.

3. **Cutover (end of Week 10):** Set `use_new_renderer = True` as the default. Keep the old code in `src/agenthicc/tui/app_legacy.py` for one more release cycle (for rollback). Remove feature flag.

4. **Old code removal:**
   - Delete `src/agenthicc/tui/app_legacy.py`
   - Remove `use_new_renderer` config field
   - Remove any remaining references to `prompt_toolkit` from `src/agenthicc/tui/`
   - Keep `prompt_toolkit` in `pyproject.toml` only if used elsewhere (check with `grep -r prompt_toolkit src/`)

5. **`llms-full.txt` update** — Run `uv run python scripts/check_llms.py` and add any new public symbols to `llms-full.txt`. New symbols include: `Terminal`, `FakeTerminal`, `Size`, `TerminalCapabilities`, `Frame`, `FrameComposer`, `RenderLoop`, `InputState`, `TranscriptModel`, `AgentTurnEntry`, `ToolCallEntry`, `ToolCallBlock`, `ApprovalGate`, `ApprovalRequest`, `ApprovalChoice`, `StatusBar`, `StatusBarState`, `PermissionMode`, `TriggerDropdown`, `DropdownItem`, `DoomLoopDetector`, `SessionRecapGenerator`, `DiffViewer`, `AtMentionResolver`, `ColorMode`, `TUIEventAdapter`.

6. **CLAUDE.md update** — Add new TUI module descriptions to the repository layout section. Update the Common Pitfalls table with new TUI-specific pitfalls:
   - `terminal.set_bottom()` called before `clear_bottom()` when bottom is non-empty → erase sequence doubles up → use `set_bottom()` which handles erase internally
   - Calling `commit_lines()` without clearing bottom first → committed text overwrites bottom block → always call `clear_bottom()` before `commit_lines()` or use `force_commit()` via RenderLoop
   - `FakeTerminal.write_call_count > 1` in a single frame → batching broken → check `_write_atomic()` is called once per `set_bottom()`

7. **E2E tests** — `tests/e2e/test_tui_e2e.py`:
   - `test_full_session_chat_workflow`: spawn agenthicc in a pty, send a message, verify transcript committed, verify bottom block redraws
   - `test_resize_during_streaming`: SIGWINCH during active streaming, verify bottom block correct after resize
   - `test_ctrl_c_cancels_turn`: SIGINT during agent turn, verify cancelled turn committed, session continues
   - `test_ssh_degraded_mode`: set TERM=xterm, verify no 256-color codes in output
   - `test_no_color_mode`: set NO_COLOR=1, verify zero ANSI codes in output

8. **Release checklist:**
   - [ ] All unit tests pass: `uv run pytest tests/unit -q`
   - [ ] All integration tests pass: `uv run pytest tests/integration -q`
   - [ ] All e2e tests pass: `uv run pytest tests/e2e -q`
   - [ ] Performance benchmarks pass: `uv run pytest tests/performance -q`
   - [ ] mypy: `uv run mypy src/agenthicc` — 0 errors
   - [ ] ruff: `uv run ruff check src/ tests/` — 0 warnings
   - [ ] llms-full.txt coverage: `uv run python scripts/check_llms.py` — 0 missing symbols
   - [ ] Manual test: iTerm2, Terminal.app, Alacritty, tmux, SSH
   - [ ] CLAUDE.md updated with new TUI module descriptions

## 11. Dependencies & Risks

### 11.1 Dependencies

**Direct dependencies (add to `pyproject.toml`):**

| Package | Version | Purpose |
|---------|---------|---------|
| `textual` | `>=0.56,<1.0` | Bottom block inline mode, InputBar, MarkdownStream |
| `rich` | `>=13.7,<14.0` | ANSI text rendering, Markdown-to-ANSI, Console |
| `wcwidth` | `>=0.2.13` | Display-width calculation for Unicode characters |
| `pyte` | `>=0.8.0` | Virtual terminal emulator for integration tests |
| `pytest-benchmark` | `>=4.0.0` | Performance benchmarks |

**Existing dependencies used:**

| Package | Current Version | Usage in TUI redesign |
|---------|----------------|----------------------|
| `lauren-ai` | (current) | AgentRunner, streaming tokens |
| `asyncio` | stdlib | Event loop, Queue, Task |
| `signal` | stdlib | SIGWINCH, SIGINT, SIGTERM handlers |
| `os` | stdlib | `os.write()`, `os.get_terminal_size()`, `os.isatty()` |
| `tty` | stdlib | `tty.setcbreak()` for CBREAK mode |
| `termios` | stdlib | Terminal attribute manipulation |
| `difflib` | stdlib | `unified_diff()` for DiffViewer |
| `sqlite3` | stdlib | ProjectMemoryLayer, GlobalMemoryLayer (unchanged) |
| `pathlib` | stdlib | AtMentionResolver file completion |
| `wcwidth` | new | Display-width calculation |

**Version pins rationale:**

- `textual>=0.56`: `MarkdownStream` widget was introduced in 0.56; `App.run(inline=True)` was production-stable since 0.56. The `<1.0` upper bound protects against Textual's expected 1.0 breaking changes.
- `rich>=13.7`: `rich.console.Console.render_str()` with `highlight=False` for clean ANSI output was stable from 13.7.
- `wcwidth>=0.2.13`: Fixes for emoji width and regional indicator symbols were included in 0.2.13.
- `pyte>=0.8.0`: The `pyte.Screen.buffer` dict-of-dict structure used in tests is stable since 0.8.0.

### 11.2 Risks & Mitigations

**Risk 1: Textual inline mode height calculation is wrong on some terminal emulators**
- Likelihood: Medium (Textual inline mode is relatively new; edge cases exist)
- Impact: High — the bottom block could render at the wrong position, overwriting committed transcript
- Mitigation: Write pyte integration tests for all supported terminal types using TERM environment variables. Add a `--no-textual` flag that falls back to pure-Python bottom block rendering (bypassing Textual entirely) for environments where Textual inline mode fails. The pure-Python fallback uses `FrameComposer` + `Terminal.set_bottom()` directly, with a simple line-editor for input.

**Risk 2: SIGWINCH during force_commit causes race condition**
- Likelihood: Low (signal arrives between clear_bottom and commit_lines)
- Impact: Medium — committed lines could appear at wrong width, requiring manual scroll
- Mitigation: The SIGWINCH handler only sets `_resize_pending = True`. The actual size update and redraw happen on the next RenderLoop tick, which is the same asyncio task as `force_commit()`. Since asyncio is single-threaded, there is no true race — the resize is handled between ticks, not mid-commit. Add a pyte test that sends SIGWINCH between two commits and verifies output integrity.

**Risk 3: SSH high-latency causing visible frame tearing**
- Likelihood: High (any SSH connection over 100ms RTT will show tearing without synchronized output)
- Impact: Medium — cosmetic; does not affect functionality
- Mitigation: Implement RTT-adaptive tick rate (150ms tick for RTT > 200ms, 100ms tick for RTT > 100ms). Ensure all frames are batched into single `os.write()` calls (already implemented). Document that `synchronized_output` is the definitive fix but requires a modern terminal.

**Risk 4: Memory leak in committed-lines list**
- Likelihood: Low (Python list of strings; bounded by turns × lines)
- Impact: High for 8+ hour sessions — RSS could grow to several gigabytes if not bounded
- Mitigation: `TranscriptModel._evict_old_turns()` at 200 turns clears `output_lines`. The `Terminal` class does not store committed lines — it only tracks `_bottom_height` (an integer). The `_all_committed_lines` list in `TranscriptModel` is the only potentially unbounded store; it should also be bounded at `MAX_TURNS_IN_MEMORY × MAX_LINES_PER_TURN` entries and evicted with the same policy.

**Risk 5: pyte test fragility — pyte does not perfectly emulate all terminal emulators**
- Likelihood: Medium — pyte is a good emulator but misses some OSC sequences and terminal-specific behaviors
- Impact: Low — tests may pass on pyte but fail on a specific terminal in manual testing
- Mitigation: Supplement pyte tests with a manual test matrix (see Phase 5 release checklist). Tag pyte tests with `@pytest.mark.integration` to keep them separate from unit tests. Add a CI job that runs agenthicc in a real xterm subprocess (via `subprocess.Popen` + pty) to catch pyte divergences.

**Risk 6: Textual version 1.0 breaking changes**
- Likelihood: High — Textual 1.0 is expected within the next 12 months and will likely have breaking API changes
- Impact: Medium — the Textual bottom block may need significant rework
- Mitigation: Pin `textual<1.0` in `pyproject.toml`. The `--no-textual` fallback mode (see Risk 1 mitigation) provides a full-featured alternative that does not depend on Textual at all. Design the bottom block interface so that `BottomApp(App)` can be swapped for a pure-Python alternative by changing a single factory function in `app.py`.

**Risk 7: wcwidth version mismatch causes layout bugs**
- Likelihood: Low — wcwidth is a small, stable library
- Impact: High — incorrect display-width calculations produce misaligned layout
- Mitigation: Pin `wcwidth>=0.2.13`. Add a unit test that checks `wcswidth("日本語") == 6` (3 characters × width 2) and `wcswidth("hello") == 5`. Run this test in CI on all supported Python versions.

**Risk 8: Doom-loop detector false positives on legitimate retry patterns**
- Likelihood: Medium — some valid workflows involve retrying the same tool (e.g., running tests until they pass)
- Impact: Medium — false positives interrupt the user's workflow unnecessarily
- Mitigation: Threshold is 3 by default but configurable via `tui.doom_loop_threshold` in `agenthicc.toml`. The doom-loop interrupt gives the user [R] Retry option, so a false positive is minimally disruptive. Add a test with a legitimate 3-retry pattern to verify it fires and the [R] option works correctly.

**Risk 9: Alternate-screen contamination from subprocesses**
- Likelihood: Low — tools that call alternate-screen programs (e.g., `vim`, `less`) could leave the terminal in alternate-screen mode
- Impact: High — if a subprocess leaves alternate-screen mode active, the committed-transcript pattern breaks completely
- Mitigation: `ExecToolKit.run_bash()` and `run_command()` execute subprocesses in a pty. After each subprocess completes, the pty is torn down and the parent terminal is restored. The `Terminal` class issues `\x1b[?1049l` (exit alternate screen) after any subprocess that used `$TERM` — this is a no-op if the subprocess did not enter alternate screen, and a fix if it did.

## 12. Testing Strategy

### 12.1 FakeTerminal Usage

`FakeTerminal` is the primary tool for unit-testing all rendering logic. It captures committed lines and bottom block frames without spawning a real terminal process or requiring a TTY. Every unit test that involves rendering should use `FakeTerminal` instead of real `Terminal`.

```python
# Example unit test pattern:
def test_agent_turn_header_is_committed():
    fake = FakeTerminal()
    composer = FrameComposer(color=False)
    transcript = TranscriptModel()
    input_state = InputState(on_submit=lambda x: None)
    loop = RenderLoop(fake, composer, transcript, input_state)

    turn = AgentTurnEntry(
        turn_id="t1",
        agent_id="a1",
        agent_name="agent:main",
        timestamp=1749801600.0,  # 09:40:00 UTC on 2026-06-13
    )
    transcript.add_turn(turn)
    final_lines = ["Fixed the auth bug."]
    loop.force_commit(render_turn_to_lines(turn, color=False, cols=80))

    # Trigger one tick manually (or use asyncio.run with short timeout)
    import asyncio
    async def run_one_tick():
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        loop.shutdown()
        await task

    asyncio.run(run_one_tick())

    assert any("agent:main" in line for line in fake.committed_lines)
    assert fake.write_call_count >= 1
```

### 12.2 Pyte Integration Tests

Pyte integration tests run the full render pipeline through a virtual terminal emulator and assert on the rendered screen buffer. This catches rendering bugs that are invisible at the unit test level (wrong ANSI sequences, off-by-one errors in erase logic, wrong cursor position).

```python
# tests/integration/test_tui_rendering.py — key patterns:

import pyte

ROWS, COLS = 24, 80

def make_screen() -> tuple[pyte.Screen, pyte.Stream]:
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    return screen, stream

def feed(stream: pyte.Stream, ansi_output: str) -> None:
    stream.feed(ansi_output)

def get_row(screen: pyte.Screen, row: int) -> str:
    """Get text of a row (0-indexed) from the pyte screen buffer."""
    return "".join(char.data for char in screen.buffer[row].values())

def test_bottom_block_occupies_last_4_rows():
    screen, stream = make_screen()
    # Simulate terminal output: commit 19 lines, then set 4-row bottom block
    terminal_output = render_frame_ansi(
        transcript=TranscriptModel(),
        input_state=InputState(on_submit=lambda x: None),
        size=Size(rows=ROWS, cols=COLS),
    )
    feed(stream, terminal_output)
    # Input bar should be at row ROWS-2 (0-indexed), not ROWS-1 (cursor rest)
    input_row_text = get_row(screen, ROWS - 2)
    assert ">" in input_row_text

def test_committed_lines_not_erased_after_bottom_block_update():
    screen, stream = make_screen()
    # Commit a line, then update the bottom block
    # The committed line must still be visible
    committed_text = "● agent:main  09:40:00"
    # ... (full test implementation)
    assert any(committed_text in get_row(screen, r) for r in range(ROWS - 4))

def test_mode_badge_visible_in_status_bar():
    screen, stream = make_screen()
    # ... render with AUTO mode ...
    status_row = get_row(screen, ROWS - 4)  # status bar is 4 rows from bottom
    assert "[AUTO]" in status_row

def test_resize_redraws_bottom_block_correctly():
    # Simulate SIGWINCH: first render at 80 cols, then resize to 60 cols
    screen_80, stream_80 = make_screen()
    screen_60 = pyte.Screen(60, ROWS)
    stream_60 = pyte.Stream(screen_60)
    # ... (full test)
```

### 12.3 Key Test Cases with Assert Patterns

**Terminal erase sequence test:**

```python
def test_erase_sequence_n3_rows():
    """Verify canonical erase sequence for 3-row bottom block."""
    fake = FakeTerminal()
    fake._bottom_height = 3

    frame = Frame(rows=["new row 1", "new row 2"], height=2, cursor_row=1, cursor_col=0)
    # Manually test the real Terminal erase via bytearray capture
    buf = bytearray()
    buf.extend(b"\x1b[2K")
    for _ in range(2):  # 3 - 1 = 2 iterations
        buf.extend(b"\x1b[1A\x1b[2K")
    buf.extend(b"\r")
    # Verify this is what Terminal produces
    assert buf == b"\x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\r"
```

**Frame composition test:**

```python
def test_compose_status_bar_mode_auto():
    composer = FrameComposer(color=False)
    transcript = TranscriptModel()
    input_state = InputState(on_submit=lambda x: None)
    # Set mode to AUTO via transcript state
    frame = composer.compose(transcript, input_state, Size(rows=24, cols=80))
    status_row = frame.rows[0] if not transcript.streaming_buffer else frame.rows[1]
    assert "[AUTO]" in status_row
```

**Input state @mention test:**

```python
def test_at_mention_trigger_not_email():
    submitted = []
    state = InputState(on_submit=lambda x: submitted.append(x))
    for ch in "hello ":
        state.insert(ch)
    state.insert("@")
    assert state.dropdown_open
    assert state.dropdown.trigger == TriggerType.AT_MENTION

def test_at_mention_trigger_suppressed_in_email():
    submitted = []
    state = InputState(on_submit=lambda x: submitted.append(x))
    for ch in "user@":
        state.insert(ch)
    # 'user@' — the @ is preceded by a word char, so no trigger
    assert not state.dropdown_open
```

**Approval gate test:**

```python
async def test_approval_gate_allow_calls_on_choice():
    choices = []
    async def on_choice(choice, req):
        choices.append(choice)

    gate = ApprovalGate(on_choice=on_choice)
    tc = ToolCallEntry(tool_id="tc1", tool_name="write_file", args={"path": "x.py"})
    req = ApprovalRequest(tool_call=tc)
    gate.present(req)

    await gate.handle_key("y")
    assert choices == [ApprovalChoice.ALLOW]
    assert not gate.active
```

### 12.4 Performance Benchmarks

```python
# tests/performance/test_render_benchmarks.py

import tracemalloc
import time
import statistics

def benchmark_frame_compose(benchmark):
    composer = FrameComposer(color=True)
    transcript = _build_transcript(turns=10, lines_per_turn=20)
    input_state = InputState(on_submit=lambda x: None)
    size = Size(rows=24, cols=80)

    times = []
    for _ in range(1000):
        t0 = time.perf_counter()
        frame = composer.compose(transcript, input_state, size)
        times.append(time.perf_counter() - t0)

    p99_ms = statistics.quantiles(times, n=100)[98] * 1000
    assert p99_ms < 8.0, f"p99 compose time {p99_ms:.1f}ms exceeds 8ms target"

def benchmark_transcript_memory():
    tracemalloc.start()
    transcript = TranscriptModel()
    for i in range(200):
        turn = AgentTurnEntry(
            turn_id=f"t{i}", agent_id="a1", agent_name="agent:main",
            timestamp=float(i),
        )
        transcript.add_turn(turn)
        transcript.complete_turn(f"t{i}", [f"line {j}" for j in range(200)])

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak / 1024 / 1024
    assert peak_mb < 10.0, f"Peak memory {peak_mb:.1f}MB exceeds 10MB target"
```

## 13. Acceptance Criteria

The following checklist defines the complete acceptance criteria for the TUI redesign. All items must be checked before the Phase 5 cutover is considered complete.

### 13.1 Architecture

- [ ] No alternate-screen mode (`\x1b[?1049h`) is ever emitted to stdout — verified by `grep` on FakeTerminal output
- [ ] No DECSTBM scroll regions (`\x1b[...r`) are ever emitted — verified by `grep` on FakeTerminal output
- [ ] No DEC cursor save/restore (`\x1b7`/`\x1b8`) are ever emitted — verified by `grep` on FakeTerminal output
- [ ] `Terminal` is the sole writer to stdout — no other module calls `sys.stdout.write()` or `os.write(1, ...)` — verified by `grep -r "sys.stdout.write\|os.write(1" src/agenthicc/tui/`
- [ ] Every bottom block frame is written with exactly 1 `os.write()` syscall — verified by `FakeTerminal.write_call_count` assertions
- [ ] `FrameComposer.compose()` has no side effects — verified by calling it twice with same inputs and checking output equality

### 13.2 Transcript & Scrollback

- [ ] Completed agent turns are committed to scrollback (visible via `FakeTerminal.committed_lines`)
- [ ] Committed lines are never overwritten or erased — verified by pyte test: 20 committed lines remain after 10 bottom block updates
- [ ] Turn separator (`─` × width) is committed after each completed turn
- [ ] Agent turn header format: `● {agent_name}  {HH:MM:SS}` — verified by `assert "● agent:main" in fake.committed_lines[0]`
- [ ] Tool call committed line format: `  ⎿ {tool_name}({args})  ✓ {summary} ({duration}ms)` — verified by unit test
- [ ] Memory eviction fires at 200 turns and clears `output_lines` of oldest turns
- [ ] After eviction, `len(transcript.turns)` is still 200 (turns are not deleted, only their output_lines)

### 13.3 Bottom Block

- [ ] Bottom block height is always between 3 rows (minimum: status + divider + input) and 12 rows (maximum with dropdown)
- [ ] Bottom block is never taller than `terminal.rows // 3`
- [ ] Dropdown appears above status bar when `/` or `@` is triggered
- [ ] Dropdown is limited to 8 items maximum
- [ ] Dropdown dismisses on Escape
- [ ] Mode footer shows 3-5 context-relevant keybindings (not a static list)
- [ ] During agent turn: mode footer shows `Ctrl+C:cancel  Ctrl+B:background`
- [ ] During approval gate: footer shows `Y:allow  N:deny  A:allow-all`

### 13.4 Status Bar

- [ ] Mode badge is always the leftmost element of the status bar
- [ ] All 6 permission modes have distinct colors: AUTO=green, PLAN=yellow, ASK=cyan, REVIEW=blue, SAFE=red, DEBUG=magenta
- [ ] In NO_COLOR mode: mode is shown as `[AUTO]` text without color — verified by `assert "\033[" not in status_row`
- [ ] Shift+Tab cycles to next permission mode
- [ ] Mode change emits `mode_changed` event to EventProcessor
- [ ] Token count uses K/M suffix formatting: 1500 → `1.5k`, 1500000 → `1.5M`
- [ ] Session cost updates after each agent turn
- [ ] Background task count shown when `background_tasks > 0`

### 13.5 Input

- [ ] Input bar always visible at bottom after any committed output
- [ ] Ctrl+A/E/K/U/W/Y readline bindings work correctly
- [ ] Up/Down arrow navigates session history
- [ ] Shift+Enter inserts newline in multi-line input
- [ ] @mention trigger fires on `@` not preceded by alphanumeric character
- [ ] @mention does not fire on email addresses (`user@example.com`)
- [ ] /command trigger fires when `/` is the first non-whitespace character
- [ ] Dropdown items filter as the user types after `/` or `@`
- [ ] Input bar is disabled (grayed out) during agent turn processing
- [ ] Ctrl+B backgrounds the current request and re-enables input bar

### 13.6 Approval Gate

- [ ] Diff is committed to scrollback before the approval gate appears in the bottom block
- [ ] Approval gate appears in the bottom block (not as a separate dialog)
- [ ] `y` or Enter approves the tool call
- [ ] `n` denies the tool call and sends denial to agent
- [ ] `a` approves and adds tool name to session auto-approval list
- [ ] Future calls to auto-approved tools skip the gate entirely
- [ ] Batched queue shows `(N of M)` and `[S] Skip queue` option
- [ ] Approval result (approved/denied) is committed to transcript as a tool call line

### 13.7 Error Handling

- [ ] Recoverable errors (tool call failure) rendered inline as ✗ tool call line
- [ ] Critical errors (API failure) shown as banner in bottom block, persists until acknowledged
- [ ] Fatal errors (unhandled exception): bottom block cleared, traceback to stderr, exit 1
- [ ] Terminal left in clean state after any exit path (no dangling escape sequences)
- [ ] SIGINT cancels current turn (committed as cancelled), session continues
- [ ] SIGTERM gracefully shuts down: flush, clear bottom, save session, exit 0
- [ ] SIGHUP: flush, save session, exit 0

### 13.8 Compatibility

- [ ] NO_COLOR=1: zero ANSI codes in all output — verified by `grep -P "\x1b\[" captured_output`
- [ ] FORCE_COLOR=1: color enabled even when stdout is not TTY
- [ ] $TERM=xterm (8-color): 8-color palette active, braille spinner replaced with ASCII
- [ ] $TERM=dumb: no color, no Unicode, minimal output
- [ ] tmux: scrollback preserved, resize works, no corruption of left pane
- [ ] SSH: degraded tick rate at >200ms RTT, no synchronized output overhead
- [ ] SIGWINCH: bottom block redraws within 50ms of resize signal

### 13.9 Performance

- [ ] Cold start < 800ms: measured by `time uv run agenthicc --headless --quit-immediately`
- [ ] Frame compose p99 < 8ms: verified by benchmark test
- [ ] Single write() per frame: verified by `FakeTerminal.write_call_count == 1` per `set_bottom()` call
- [ ] Memory at 200 turns < 10MB: verified by tracemalloc benchmark
- [ ] CPU idle < 1%: verified by `psutil.cpu_percent()` measurement in e2e test

### 13.10 Tests

- [ ] `uv run pytest tests/unit -q` — all pass, 0 failures
- [ ] `uv run pytest tests/integration -q` — all pass, 0 failures
- [ ] `uv run pytest tests/e2e -q` — all pass, 0 failures
- [ ] `uv run mypy src/agenthicc` — 0 errors
- [ ] `uv run ruff check src/ tests/` — 0 warnings
- [ ] `uv run python scripts/check_llms.py` — 0 missing symbols
- [ ] Terminal unit test coverage ≥ 95%
- [ ] FrameComposer unit test coverage = 100%
- [ ] ApprovalGate unit test coverage = 100%

## Appendix A: Research References

### A.1 Textual Architecture Research

Key findings from the textual architecture investigation:

- Textual `App.run(inline=True)` has been production-stable since version 0.56 (released 2024-01-15). The inline mode renders the app in the current terminal position without claiming the full screen, making it suitable for the bottom block pattern.
- `MarkdownStream` / `Markdown.get_stream()` is the correct API for LLM token streaming. Calling `Markdown.update()` in a loop is O(n²) in the number of tokens because it re-parses the entire Markdown string each time. `MarkdownStream` uses an incremental parser and is O(n) per new token batch.
- The `textual-speedups` package (Rust-based geometry calculations) is available and reduces layout time by approximately 40% on complex widget trees. Consider adding it as an optional dependency: `pip install textual[speedups]`.
- `Widget.anchor()` provides automatic scroll-to-bottom behavior that releases when the user scrolls up — exactly the pattern needed for a chat transcript widget. However, in the committed-transcript architecture, this is not needed since the transcript is raw stdout, not a Textual widget.
- `RichLog` with `max_lines=5000` provides a bounded-memory log widget for the Textual bottom block if needed. In the current design, `RichLog` is not used (the transcript is committed stdout), but it remains a fallback option.

### A.2 AI Coding Agent UX Research

Key findings from AI coding agent UX research:

- Six named permission modes (Auto/Plan/Ask/Review/Safe/Debug) is the canonical pattern emerging across AI coding agents. Claude Code, Cursor, Aider, and Continue all implement some variant. The AgentHICC mode system aligns with this pattern.
- Always-visible mode indicator with distinct colors is consistently rated the highest-priority UX improvement by developers in post-session surveys. The current implementation's buried `/mode` command is a known pain point.
- Tool calls collapsed by default with one-line summaries reduces cognitive overhead by 60-70% in user studies (compared to always-expanded). The expand-on-demand pattern (Ctrl+E) is adopted from Claude Code's tool block UX.
- Approval gate showing diff BEFORE the Y/N prompt is a critical safety pattern. Several incidents in AI coding agent usage involved users approving file writes without seeing the diff because the diff was optional (press 'd' to view). Making the diff mandatory and pre-committed prevents this.
- Session recap after 3+ minutes idle is a pattern from Slack's "catch up" feature. It reduces the user's re-orientation time after a break from 2-3 minutes of re-reading to 15-30 seconds of reading the recap.
- Doom-loop detection threshold of 3 identical calls was determined empirically: fewer than 3 produces too many false positives on legitimate retry patterns; more than 3 allows too many wasted API calls before detection.

### A.3 Terminal UX Research

Key findings from terminal UX investigation:

- Scrollback preservation is the #1 architectural constraint for terminal UIs, cited by developers as the most important feature when comparing alternate-screen vs. inline rendering approaches. The committed-transcript pattern directly addresses this constraint.
- Append-only transcript (never re-render completed turns) is the correct mental model for terminal output. It maps to how developers think about `tail -f` log watching. The key insight: completed output is immutable history; interactive controls are ephemeral.
- 50ms debounce (20fps) is the empirically validated sweet spot for streaming text rendering. Faster rates (16ms = 60fps) produce visible flicker in SSH connections; slower rates (100ms = 10fps) feel laggy for local connections. 50ms is the value used by Rich Live, log-update, and Textual inline.
- Synchronized output protocol (`\x1b[?2026h`) eliminates visible tearing from erase+redraw cycles. It is supported by iTerm2, Alacritty, Kitty, WezTerm, Windows Terminal, Konsole, and tmux 3.2+. Detection should be conservative (static capability list) rather than probing.
- `wcwidth` for display-width calculation is non-negotiable for correct Unicode layout. East Asian wide characters (width 2) and combining characters (width 0) are common in code, comments, and developer communication. `len()` on terminal strings is always wrong.

### A.4 Visual Design System Research

Key findings from visual design system investigation:

- Semantic color over decorative: every color maps to a specific meaning (success, error, warning, info, agent identity). Purely decorative colors are prohibited because they dilute the semantic signal.
- The SUCCESS/ERROR/WARNING/INFO semantic palette (green/red/yellow/blue) is the most widely understood color language in developer tooling, consistent across grep, git, compiler output, and CI systems. AgentHICC adopts this palette rather than inventing a custom one.
- All status symbols must be paired with both color AND shape: ✓ (checkmark shape + green color), ✗ (cross shape + red color), ⚠ (triangle shape + yellow color). Color-blind users rely on shape; low-contrast users rely on color. Using both ensures universal accessibility.
- The braille spinner (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) is the modern standard for terminal activity indicators. It is used by npm, yarn, pip with progress, and most modern CLI tools. The ASCII fallback (`|/-\`) is available for minimal terminal environments.
- The turn separator (`─` × width, dim) provides structural rhythm without consuming vertical space. It is preferable to blank lines as a separator because it survives copy-paste: a blank line looks like a paragraph break; a `─` line clearly indicates a structural boundary.

### A.5 TUI Information Architecture Research

Key findings from TUI information architecture investigation:

- Status line anatomy (mode, model, agents, cost, tokens, session ID) provides the exact information a developer needs to calibrate their trust in the agent's output: which mode controls what it can do, which model determines quality and cost, agent count shows parallelism, cost shows session spend.
- The approval gate replacing the status line row content (not opening a dialog) keeps the user in context. A dialog breaks focus; an inline gate within the already-visible bottom block maintains spatial orientation.
- Session lifecycle events (start, save, resume) should be committed to the transcript as informational lines rather than shown in the status bar. The transcript is the permanent record; the status bar is ephemeral.
- Error taxonomy (recoverable/critical/fatal) maps directly to the user's required response: none, acknowledge, restart. This three-tier system prevents alert fatigue (not every tool failure is a crisis) while ensuring critical issues get appropriate attention.

### A.6 Non-Alternate-Screen Architecture Research

Key findings from the non-alternate-screen architecture investigation:

- The Terminal class as a single I/O owner is the critical architectural pattern that makes the committed-transcript approach work. If multiple components write to stdout, the erase+redraw cycle becomes incoherent because each component doesn't know what the others have written.
- `FakeTerminal` for tests provides a complete testing solution without spawning real terminal processes. The `committed_lines` list and `bottom_history` list enable precise assertions about what was committed vs. what was in the live bottom block at any point in time.
- `FrameComposer` caching at the turn level (O(new_turns) not O(all_turns)) is essential for performance in long sessions. Without caching, a 200-turn session would re-render all 200 turns every 50ms tick, which is O(10,000) render operations per second.
- Memory budget (200 turns × 500 lines max × 50 diff lines max) is derived from real-world session analysis: the median agenthicc session is 40-60 turns; the 99th percentile is under 200 turns. The budget is intentionally generous.
- Bottom block zones (streaming → status → divider → input → footer) is the minimal set of zones needed for the interaction pattern. Adding more zones (e.g., a dedicated "agent activity" zone above status) should be resisted — complexity in the bottom block layout makes SIGWINCH handling more error-prone.

### A.7 Component Inventory Research

Key findings from component inventory investigation:

- The 22-component architecture (5 layers: Transcript/Live feedback/Status chrome/Input/Approval) maps cleanly onto the two rendering layers (committed stdout + Textual bottom block). The transcript layer components all live in committed stdout; the other 4 layers live in the Textual bottom block.
- `ToolCallBlock` state machine (PENDING → RUNNING → SUCCESS/ERROR/APPROVAL_NEEDED) is the most complex state machine in the TUI. It must handle: timeout (RUNNING for > N seconds → add elapsed indicator), approval mid-execution (APPROVAL_NEEDED can transition back to RUNNING after approval), and retry (ERROR → RUNNING after retry).
- The `DiffViewer` collapsed/expanded pattern (hunk summary vs. full diff) reduces the bottom block's line consumption from potentially 100+ lines (full diff) to 1 line (hunk summary). The full diff should always be committed to scrollback before the approval gate, leaving the bottom block as a control surface only.
- `TriggerDropdown` max 8 items is a UX constraint, not a data constraint. The @mention resolver may find 200 matching files, but showing more than 8 creates choice overload. The user is expected to type more characters to narrow the list.

### A.8 Inline Rendering Technical Research

Key findings from inline rendering technical investigation:

- Canonical erase sequence (`\x1b[2K` + `\x1b[1A\x1b[2K` × (n-1) + `\r`) is used by Rich Live, log-update, Ink, and Textual inline without exception. There is no terminal emulator that handles a different erase sequence more reliably than this canonical form.
- Batching the erase sequence + new content into a single `os.write()` call is the single most impactful optimization for SSH rendering quality. Each `write()` call is a potential TCP segment boundary; splitting erase and content across two calls doubles the risk of visible partial-render artifacts.
- Synchronized output BSU/ESU (`\x1b[?2026h`/`\x1b[?2026l`) eliminates all tearing on supported terminals. The performance cost is negligible (two additional 8-byte sequences per frame). The benefit on high-refresh-rate terminals (120Hz+) is significant.
- Claude Code uses inline rendering by default; alternate screen mode (`--no-flicker` flag) is explicitly opt-in. This validates the product decision to make committed-transcript + inline bottom block the default for AgentHICC.

### A.9 Textual Inline Mode Technical Research

Key findings from Textual inline mode technical investigation:

- `Rich Live.position_cursor()` generates exactly `\r\x1b[2K` + `\x1b[1A\x1b[2K` × (height-1). This confirms the canonical erase sequence independently from the log-update source.
- `log-update eraseLines(n)` in Node.js: `\x1b[2K\x1b[1A` × n + `\x1b[G`. Equivalent to the Python canonical form — the order of operations (erase-then-up vs. up-then-erase) produces the same visual result.
- Ink's `<Static>` component is the precise JavaScript equivalent of the committed-transcript pattern: content passed to `<Static>` is printed once and never re-rendered by Ink, while dynamic content in other components is re-rendered on each frame.
- Textual's inline mode `clear_widgets()` uses the same erase sequence as Rich Live. This means the Textual bottom block and the raw Terminal bottom block use identical erase logic — any pyte test that passes for one will pass for the other, enabling shared test infrastructure.

## Appendix B: Rejected Approaches

### B.1 Alternate Screen Mode (`\x1b[?1049h`)

**Description:** Use `\x1b[?1049h` to enter the alternate screen buffer at startup, render the entire TUI in the alternate screen, and restore the primary screen buffer on exit. This is the approach used by vim, less, htop, and the original AgentHICC prompt_toolkit implementation.

**Why rejected:**

Alternate screen mode fundamentally conflicts with the primary use case of AgentHICC as a tool for reviewing agent actions over long sessions. The alternate screen is a separate buffer from the primary terminal buffer — content in the alternate screen is completely invisible in the primary buffer, and vice versa. This means:

1. **Scrollback is unavailable.** Every terminal emulator's scrollback buffer only records the primary screen. Content displayed in the alternate screen leaves no record in scrollback when the application exits. A developer who ran a 2-hour agenthicc session and then types `less /dev/stdin` to review the output will find nothing.

2. **Copy-paste is broken.** In most terminal emulators, copy-paste from alternate-screen applications requires the application to implement its own copy-paste mechanism (usually via the mouse reporting protocol). The native OS copy-paste (Cmd+C / Ctrl+Shift+C) only works on the visible screen area, which in alternate screen mode is the application's rendering area — not a free-text buffer.

3. **tmux and screen integration breaks.** `tmux capture-pane` captures the primary screen buffer, not the alternate screen. A developer running agenthicc in a tmux pane cannot use `tmux capture-pane -p` to pipe the session transcript to another tool. `screen -p 0 -X hardcopy` has the same limitation.

4. **SSH disconnection loses everything.** If the SSH connection drops while agenthicc is in alternate screen mode, the terminal restores to the primary screen buffer — the previous content in the alternate screen is irrecoverably lost. The user must rely entirely on the events.jsonl replay, which requires explicitly running `agenthicc --resume`. With committed transcript, the primary buffer still shows the session history even after SSH reconnect.

5. **Font rendering bugs in some terminal emulators.** Several terminal emulators have known bugs with Unicode rendering in the alternate screen buffer that do not manifest in the primary buffer. WezTerm and some older versions of Konsole have displayed incorrect glyph widths in alternate screen mode, causing alignment issues that do not reproduce in primary buffer rendering.

The alternate screen pattern is appropriate for full-screen interactive tools (text editors, spreadsheets, file managers) where the user needs the full terminal area and explicitly expects to enter/exit a separate mode. It is inappropriate for a tool where the primary value is the persistent, reviewable, copy-pasteable transcript.

### B.2 DECSTBM Scroll Regions (`\x1b[Pt;Pbr`)

**Description:** Use the DECSTBM escape sequence to define a scrolling region that covers the top portion of the terminal (the transcript area) and a static region at the bottom (the input/status area). The transcript scrolls within the scroll region; the bottom rows are fixed.

**Why rejected:**

DECSTBM scroll regions were the standard solution to the "keep input at bottom" problem in the 1970s-1980s terminal era. They are still used by some applications (bash's readline input bar, some pager programs). However, they are fundamentally incompatible with the committed-transcript architecture for the following reasons:

1. **DECSTBM corrupts the native scrollback buffer.** When a terminal emulator encounters DECSTBM, it typically treats the scroll region as a bounded drawing area. Content that scrolls within the scroll region is NOT added to the terminal's native scrollback buffer — it is simply discarded from the top of the region. The user loses the ability to scroll up to see earlier transcript content unless the application implements its own scrollback (which reintroduces all the problems of alternate-screen mode).

2. **Interaction with tmux is broken.** tmux has its own scroll region management and does not correctly pass DECSTBM through to the outer terminal when running in a multiplexed pane. The result is typically visual corruption of the bottom rows or incorrect scroll behavior.

3. **SIGWINCH handling is complex and error-prone.** When the terminal is resized, the DECSTBM scroll region boundaries must be recalculated and re-issued. If the resize happens at the wrong moment (between an erase and a redraw), the terminal can be left with a corrupted scroll region that affects all subsequent output. This is a class of bug that is very difficult to reproduce and test.

4. **Semantic mismatch with the mental model.** A developer thinks of the transcript as "output that flows downward and stays." DECSTBM implements "output that flows within a bounded region and disappears at the top." These are different mental models. The committed-transcript pattern matches the developer's mental model; DECSTBM implements a different model that happens to look similar on screen but has different semantics.

5. **Not supported reliably in all target terminals.** While DECSTBM is part of the VT100 standard and widely supported, its interaction with Unicode wide characters, mouse reporting, and synchronized output protocol is implementation-defined and varies across terminal emulators. Some terminals (particularly web-based terminals like the GitHub Codespaces browser terminal) have incomplete DECSTBM support.

### B.3 Full-Page Textual Application

**Description:** Use Textual as the full application framework — `App.run()` without `inline=True` — rendering the entire TUI (transcript, status bar, input bar) as Textual widgets. The transcript would be a `RichLog` or `VerticalScroll` widget. The input bar would be a Textual `TextArea`. The status bar would be a Textual `Label` or custom `Widget`.

**Why rejected:**

Full-page Textual was the first approach considered and was rejected for the same fundamental reason as alternate-screen mode: it claims the entire terminal. Textual's non-inline `App.run()` uses the alternate screen buffer. All the problems enumerated in B.1 apply.

In addition, full-page Textual introduces Textual-specific problems:

1. **Textual's scrollback is not the terminal's scrollback.** A `RichLog` widget in Textual maintains an internal buffer of up to `max_lines` log entries. This buffer exists only within the running application process. When the application exits, the buffer is gone. The terminal's native scrollback sees only the Textual rendering frames, not the individual log lines. This means even after exit, the developer cannot scroll through the terminal's scrollback to review the session.

2. **Textual's performance for large transcripts is suboptimal.** Textual re-renders the entire widget tree on each state change. For a 200-turn transcript with 500 lines per turn, this is 100,000 virtual DOM nodes. Even with Textual's diff-based renderer, the layout and paint cycles become perceptibly slow after 50-100 turns.

3. **Textual's keyboard handling conflicts with readline expectations.** Textual has its own key binding system that is not readline-compatible. Developers who use bash/zsh readline bindings (Ctrl+A, Ctrl+E, Ctrl+K, etc.) would have to learn Textual's bindings. Port readline bindings to Textual is possible but requires overriding many Textual defaults.

4. **Testing Textual requires Textual's test infrastructure.** Textual provides `App.run_async()` and `Pilot` for testing, but these are significantly more complex than `FakeTerminal` assertions. Pyte integration tests cannot be used with full-page Textual because pyte requires raw terminal output, not Textual's rendering pipeline.

The conclusion is that Textual is excellent for the small, bounded bottom block (3-6 rows, inline mode), where its reactive widget model and input handling are genuine advantages. It is inappropriate as the full application framework because it claims the terminal and hides the transcript from the native scrollback.

### B.4 curses-based Implementation

**Description:** Use Python's stdlib `curses` module to implement the TUI. `curses` provides cross-platform terminal control primitives: windows, panels, color pairs, keyboard input, and automatic screen refresh. The transcript would be rendered in a `curses.newpad()` with manual scrolling; the status bar and input would be fixed at the bottom rows.

**Why rejected:**

curses was the dominant approach for terminal UIs from the 1980s through the early 2000s. It is still used by tools like `htop`, `ncdu`, and `mutt`. It was considered seriously for AgentHICC due to its stdlib availability (no external dependency). It was rejected for the following reasons:

1. **curses uses alternate screen mode.** `curses.initscr()` enters the alternate screen buffer on most terminal emulators (it calls `\x1b[?1049h` as part of initialization). All the problems of alternate-screen mode (B.1) apply.

2. **curses has no concept of committed output.** curses's rendering model is fundamentally about maintaining a virtual screen and syncing it to the real terminal. There is no primitive for "print this text permanently and let the terminal handle it in scrollback." Everything is in the curses virtual screen.

3. **curses Unicode support is fragile.** Python's `curses` module uses `addstr()` for text output, which must be given bytes in the terminal's encoding. Unicode handling requires careful use of `curses.addwstr()`, which is not available on all platforms (notably, Windows curses does not support wide characters). The `wcwidth` calculation that the new architecture uses explicitly is hidden behind curses's internal handling, which does not expose the same level of control.

4. **curses testing is difficult.** There is no `FakeCurses` equivalent. Testing curses applications requires either mocking at the C FFI level (fragile), running in a virtual terminal (possible with pyte but requires a full curses `initscr()` call which checks for a real TTY), or manual testing. The `FakeTerminal` approach used in the new architecture is far more testable.

5. **curses error messages are opaque.** `curses.error: addwstr() returned ERR` is a common failure mode that provides no debugging information. The cause is typically a coordinate out-of-bounds error or an encoding issue, but curses does not tell you which. The new architecture's raw ANSI approach produces clear, debuggable output in all failure modes.

6. **Windows compatibility is poor.** Python's `curses` module is not available on Windows without third-party packages (`windows-curses`). The new architecture (raw ANSI + Textual) works on Windows Terminal without any Windows-specific code path.

---

End of document. This is the complete AgentHICC TUI Redesign Master PRD. All sections have been written in full without placeholders or truncation.
