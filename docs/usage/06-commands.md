# Slash commands

The canonical registry is `BUILTIN_COMMANDS` in
`src/agenthicc/commands/builtins.py` and contains **25** entries:

| Command | Argument hint | Purpose |
|---|---|---|
| `/replay` | `[session-id]` | Replay a session |
| `/cancel` | — | Cancel the active turn (alias `/interrupt`) |
| `/loop` | `[interval] <prompt> \| status \| pause \| resume \| stop` | Schedule and control a recurring session prompt |
| `/loops` | — | Open the table of all persisted schedule jobs |
| `/schedules` | — | Manage repository-scoped scheduled workflows |
| `/schedule` | `create ... \| run-now ID \| pause ID \| resume ID NEXT-RUN \| delete ID --confirm` | Create or control a repository scheduled workflow |
| `/agents` | `[running\|needs-you\|history\|library]` | Open the agents hub over the conversation, optionally on one tab (also Ctrl+G, or ← on an empty prompt) |
| `/resume` | — | Resume a saved session: opens the agents hub on its History tab, where Enter resumes one |
| `/release-notes` | — | Page the packaged `CHANGELOG.md`: the installed version, every version since the last one you read, and unreleased work |
| `/inbox` | — | Open the agents hub's Needs you tab: pending approvals, questions and input requests |
| `/subagents` | — | Show the turn's subagents and the session's earlier ones; Enter opens a worker's transcript (also Ctrl+O while the panel is shown) |
| `/clear` | — | Clear the transcript |
| `/commands` | `[reload]` | List commands |
| `/tools` | `[reload]` | List tools |
| `/workflows` | `[runs\|reload]` | List workflows or runs |
| `/config` | — | Open the configuration overlay |
| `/expand` | `[tool-id-or-@path]` | Expand a collapsed tool group |
| `/help` | `[/command]` | Show help |
| `/history` | `[repair]` | Browse transcript history; `/history repair` retries safe local tool-history repair |
| `/logs` | — | Show the tail of this session's diagnostics log (`~/.agenthicc/logs/<session>.log`): stray stderr and Python warnings captured while the TUI was live |
| `/init` | `[write] [--force]` | Create the AGENTS.md / config scaffold |
| `/mcp` | `[status\|reload\|connect NAME\|disconnect NAME\|refresh NAME\|doctor [NAME]]` | MCP status and control |
| `/model` | `[provider] [model] [--save]` | Open the model picker, or switch to a provider and model at once; applies from the next turn, no restart (alias `/models`) |
| `/skills` | `[reload]` | Inspect and reload skills |
| `/status` | — | Show the session, model, usage and the pre-flight check's findings (credentials, model, workspace, tools) |
| `/startup` | — | Print startup milestones (process start → first frame → agent runtime ready, modules at first frame) and the bounded phase report |
| `/keybindings` | — | Show the effective key bindings and where each comes from; create `~/.agenthicc/keybindings.toml` with every action listed |
| `/perf` | — | Print live-block rendering diagnostics: frames drawn, full redraws, render and write time percentiles, characters per frame, whether synchronized output is on, event-loop lag (p50, p95, max) and the last five loop stalls with the code that caused them |
| `/ps` | `[terminal-id] [--json]` | List processes (alias `/processes`) |
| `/background` | — | Detach the active session into a background agent and open the agents hub with it selected (aliases `/bg`, `/detach`) |
| `/workers` | `[--json]` | Open the agents hub's Running tab, where this session's parallel workers are nested under it; `--json` prints their worker manifests |
| `/stop` | `[terminal-id\|all] [--force]` | Stop a terminal (alias `/stop-terminal`) |
| `/usage` | — | Show the local token/cost snapshot |
| `/mode` | `[Safe\|Plan\|Yolo]` | Open the mode picker, or switch directly |
| `/workflow` | `<name> \| resume [run-id] \| reset [run-id]` | Select or resume a workflow |
| `/compact` | — | Compact the conversation |

!!! note "Commands outside `BUILTIN_COMMANDS`"
    `/create-tools` and `/create-commands` come from bootstrap skills, not the
    registry. `/background` (aliases `/bg` and `/detach`) is a registry entry
    that `TUISession` intercepts to hand the conversation to the background
    service.

!!! note "`/workflow` and `/compact` carry `handler=None`"
    Both are intercepted in `TUISession.route()` so they can reach
    session-local state. They exist in the registry purely so the trigger
    picker can display and complete them, which is why invoking them by a
    non-TUI path does not work.

## Trigger pickers

- `/` — command picker: fuzzy over name, aliases and description; grouped when
  nothing is typed; shows each command's key
- `$` — skill-only picker
- `@` — project file/mention picker: directories and files with sizes, changed
  and recently mentioned files first
- Ctrl+P on an empty prompt — the command palette (every command, key action
  and theme)

See the [TUI guide](../guides/tui.md#pickers-and-the-command-palette) for how
rows are ranked and drawn.

## Recurring session prompts

`/loop` is a local scheduler owned by the current interactive session. Create
one with an optional strict duration (`s`, `m`, `h`, or `d`) followed by an
ordinary prompt or a registered slash command:

```text
/loop 5m Check the deployment and fix a failing health check.
/loop 30m /status
```

The first iteration is due immediately, but creation waits behind an active
turn. Later iterations never interrupt a tool call, workflow phase, approval,
question, or plan review. If several intervals elapse while the session is
busy, they collapse into one pending iteration. Only one loop can be active in
a session; creating another replaces the definition without cancelling a
currently running turn.

Control the loop with:

```text
/loop status   # state, cadence, counters, and a redacted payload preview
/loop pause    # retain the definition without dispatching it
/loop resume   # reactivate and make one iteration due now
/loop stop     # stop future iterations
```

Use `/loops` to manage every valid persisted loop record below the user
session store. The table shows job ID, state, cadence, next due time, owning
session, and a redacted payload preview. Use `↑`/`↓` (or `j`/`k`) to select a
row, `Enter` to make it due immediately, and `d` followed by `Enter` to delete
the selected schedule permanently. `Esc` closes the table. Entering a job from
the current live session wakes its scheduler immediately; a job owned by a
different live session cannot be raced and instructs you to resume that
session. An unowned foreign job is marked due for its next explicit attach.

Loop state is persisted in
`~/.agenthicc/sessions/<session-id>/loop.json`. It is not automatically
executed by a newly started process: use `agenthicc --resume <session-id>` or
`agenthicc --continue` to explicitly reattach the session. Rehydration
coalesces missed intervals rather than replaying a burst. Loops expire after
the configured finite lifetime (72 hours by default), and repeated scheduler
failures enter a terminal state with bounded backoff.

Payloads use the normal message/command path, so the existing conversation,
workflow checkpoints, ownership lease, permissions, approvals, network and
browser policy, and cancellation rules still apply. Nested `/loop` commands,
unknown slash commands, arbitrary shell commands, and oversized payloads are
rejected. `/loop` is interactive-TUI functionality; headless stdin returns a
structured `loop_interactive_required` error.

See [loop configuration](02-configuration.md#recurring-loop-prompts) and the
[storage reference](../reference/storage.md#session-files) for bounds and
durability details.

## Local versus queued

`/usage`, `/config`, `/status`, `/loops`, loop status/stop/pause/resume, and the run
controls execute immediately even while the agent is responding. Loop creation
and ordinary requests queue in FIFO order until the session is safe to run.

## Next

- [Sessions →](07-sessions.md)
- [Tools →](09-tools.md)
- Depth: [User-defined commands](../guides/commands.md)
