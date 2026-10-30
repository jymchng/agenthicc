# CLI reference

The entry point is `agenthicc.__main__:main`. Commands are discovered by a
decorator-based registry (`src/agenthicc/cli/registry.py`) and wired into
`argparse` by `src/agenthicc/cli/parser.py`. Because discovery is dynamic,
project-local plugins can add commands; this page documents the **built-in**
surface that is present before any project extension loads.

Run `agenthicc --help` for the generated list, and
`agenthicc <group> <command> --help` for one command's arguments.

## Global options

Global options are accepted before the subcommand and are also parsed on their
own (`agenthicc --version`).

| Option | Meaning |
|---|---|
| `--headless` | Run without the TUI; emit JSON-lines to stdout |
| `--goal GOAL` | Select `goal_flow` and start one durable goal run with GOAL as its initial user intent |
| `--detach` | Return after a `--goal` run is accepted by the background supervisor; the response includes the detached worker PID |
| `--json` | Emit machine-readable goal/control output |
| `--workflow NAME` | Start the TUI with `NAME` selected, or run `NAME` for each stdin line in headless mode; cannot be combined with `--goal` |
| `--mode MODE` | Start with `MODE` selected (for example `Safe`, `Plan`, or `Yolo`); an explicit CLI mode overrides a persisted session mode |
| `--agent NAME` | Start with primary agent `NAME` selected; see `agenthicc agents definitions list` |
| `--config PATH` | Path to `agenthicc.toml` |
| `--version` | Print the package CLI version string (currently `agenthicc 0.1.0`) |
| `--continue` | Continue the most recent session for this directory; a busy latest session fails with `session_already_active` |
| `--resume ID` | Resume the session with the given ID; the conflict exit status is `3` |
| `--record-cassette [DIR]` | Record LLM calls and approvals to `DIR/<session-id>/`; omit `DIR` for `~/.agenthicc/cassettes` |
| `--set KEY=VALUE` | Override a config key (`section.key=value`); repeatable |
| `--set-secret KEY=ENV_VAR` | Set a secret config key from an environment variable; repeatable |
| `--dangerously-skip-permissions` | Disable all tool approval prompts for this session; intentionally not settable in `agenthicc.toml` |

`--set` accepts a literal value. `--set-secret` instead stores a **symbolic
environment-variable reference**, so the secret never appears in the command
arguments or in shell history:

```bash
agenthicc --set-secret execution.default_headers.Modal-Key=MODAL_KEY config show
```

`--dangerously-skip-permissions` overrides Safe-mode approval requirements but
**not** Plan mode: Plan mode hard-blocks side effects even with this flag.

### Detached goal output

`agenthicc --goal GOAL --detach` returns after one durable `goal_flow` run and
one background worker have been accepted. An explicit global `--mode MODE` is
persisted in the worker request and applied through normal session startup;
the background session detail view shows the canonical effective mode. On
resume, stored mode metadata wins unless that invocation explicitly provides a
new `--mode`. Human output includes `Run ID`,
`Goal`, `Status`, `Main`, `PID`, `Track`, and `Attach`. JSON output contains
`detached: true`, `pid`, and `worker_pid` (the latter two identify the child
worker, never the short-lived launcher). The PID is retained for inspection
after process exit; lifecycle status and the session lease are authoritative.

Only the detached worker owns the terminal finalization path. It persists the
workflow outcome, exit code/reason, and worker-exit event before returning.
Attached `--goal` runs do not install this finalizer and remain normal,
interactive TUI sessions.

## Command groups

Ten groups have subcommands. Each group name is also callable on its own
(`agenthicc mcp`, `agenthicc config`, `agenthicc perf`) and prints that group's help;
`agenthicc agents` alone opens the agents hub instead.

| Group | Purpose |
|---|---|
| `agents` | The agents hub and its scriptable commands (background agents, their inbox) |
| `config` | Manage configuration |
| `mcp` | Manage configured MCP servers |
| `perf` | Run offline performance diagnostics |
| `schedules` | Manage durable repository-scoped workflow automations |
| `session` | Inspect, control, and attach to client-neutral sessions |
| `sessions` | Manage saved sessions |
| `skills` | Install and manage skill definitions |
| `trust` | Manage trust for project-local plugins |
| `workflows` | Discover and run workflow plugins |

## Commands

The table below lists every built-in leaf command. `[--flag]` means optional;
`ARG` in upper case is a required positional. The **Purpose** column is the
help string the command registers.

| Command | Purpose |
|---|---|
| `agents [SESSION_ID] [--tab TAB] [--run RUN_ID] [--json]` | Open the agents hub (Running, Needs you, History, Library); `SESSION_ID` opens one agent's detail, `--tab` picks `running`, `needs-you`, `history` or `library`, `--run` shows one goal run's agents; `--json` lists agents instead. Without a terminal it prints the tab |
| `agents actions WORKER_SESSION_ID [--json]` | List named actions in one worker worktree |
| `agents action WORKER_SESSION_ID ACTION_ID [--approve] [--json]` | Review or run one worker action |
| `runs` | Inspect and control durable goal runs |
| `schedules create WORKFLOW PROFILE MODE RECURRENCE TIMEZONE PROMPT [options]` | Create a disabled schedule and preview its first run |
| `schedules list [--repository-root PATH] [--all] [--json]` | List schedules and whether they are armed, paused, blocked, or awaiting the service |
| `schedules show SCHEDULE_ID [--json]` | Inspect a schedule, revisions, and recent occurrences |
| `schedules pause SCHEDULE_ID` / `resume SCHEDULE_ID [--confirm-next-run TIME]` | Pause or preview/confirm schedule execution |
| `schedules run-now SCHEDULE_ID [--json]` | Queue an immediate auditable occurrence without moving the recurrence cursor |
| `schedules update SCHEDULE_ID FIELD VALUE --confirm` | Create a revised, paused definition after explicit confirmation |
| `schedules retry SCHEDULE_ID OCCURRENCE_ID` | Retry a failed/blocked occurrence as a new attempt |
| `schedules history SCHEDULE_ID [--limit N]` | Inspect bounded run history |
| `schedules delete SCHEDULE_ID --confirm` | Tombstone a schedule while preserving prior runs and artifacts |
| `schedules service-status` | Check whether an Agenthicc service owns the schedule scheduler |
| `attach SESSION_ID [--json]` | Attach the normal TUI to one exact background session |
| `attach --run RUN_ID [--json]` | Explicit compatibility form for attaching a goal run |
| `init [--write] [--force]` | Create `AGENTS.md` and a commented `.agenthicc` configuration template |
| `run [--background] [--workflow NAME] [--intent TEXT] [--title TEXT]` | Start an agent turn or workflow |
| `login` | Authenticate with agenthicc.ai |
| `logout` | Log out and revoke stored tokens |
| `whoami` | Show the currently authenticated user |
| `config show` | Print the merged effective configuration |
| `config validate` | Validate the effective provider configuration |
| `config profiles` | List configured provider profiles |
| `config init [--force]` | Create a template `agenthicc.toml` in `.agenthicc/` |
| `agents list [--json] [--all] [--trash]` | List background agents, with the hub's status labels |
| `agents definitions list [--json]` | List all agent definitions and their source, state, and policy metadata |
| `agents definitions show NAME` | Show all candidates for a definition name and each valid prompt |
| `agents definitions validate` | Report invalid or untrusted definitions (exit status 1 when attention is required) |
| `agents status SESSION_ID [--json]` | Show one background agent |
| `agents cancel SESSION_ID` | Stop a background agent |
| `agents resume SESSION_ID` | Resume a background agent |
| `agents retry SESSION_ID` | Retry a failed background agent |
| `agents priority SESSION_ID PRIORITY` | Change a background agent's queue priority (`urgent`, `normal`, `low`) |
| `agents approve SESSION_ID` | Approve a waiting background agent |
| `agents reject SESSION_ID` | Reject a waiting background agent |
| `agents input SESSION_ID VALUE` | Provide input to a waiting background agent |
| `agents inbox list [--project PATH] [--type TYPE] [--older-than-s SECONDS] [--status STATUS] [--json]` | List exact background approvals/questions/input requests and their versions |
| `agents inbox answer ITEM_ID RESPONSE VERSION IDEMPOTENCY_KEY` | Answer an exact question/input request; multiple answers use a JSON map |
| `agents inbox approve ITEM_ID VERSION IDEMPOTENCY_KEY [options]` | Approve the exact tool request |
| `agents inbox reject ITEM_ID VERSION IDEMPOTENCY_KEY [--message TEXT]` | Reject the exact tool request |
| `agents inbox expire ITEM_ID VERSION IDEMPOTENCY_KEY` | Expire one exact pending interaction |
| `agents notifications [--session-id ID] [--status STATUS] [--json]` | Inspect background notification delivery receipts |
| `agents rename SESSION_ID TITLE` | Rename a background agent |
| `agents labels SESSION_ID [--labels TEXT]` | Set comma-separated labels on a background agent |
| `agents archive SESSION_ID` | Archive a background agent |
| `agents delete SESSION_ID` | Move a background agent to recoverable trash |
| `agents restore SESSION_ID` | Restore a deleted background agent |
| `agents purge` | Permanently remove expired background trash |
| `mcp add NAME URL [--project \| --global] [--transport KIND] [--token-env ENV_VAR] [--no-auto-connect] [--reconnect-attempts N] [--reconnect-delay-seconds N]` | Add an MCP server to configuration |
| `mcp list [--project \| --global] [--json]` | List configured MCP servers |
| `mcp get NAME [--project \| --global] [--json]` | Show one configured MCP server |
| `mcp remove NAME [--project \| --global]` | Remove one configured MCP server |
| `mcp connect NAME` | Connect to one MCP server and inspect its catalog |
| `mcp disconnect NAME` | Disconnect an MCP server in the current process |
| `mcp refresh NAME` | Refresh one MCP server's tool catalog |
| `mcp auth NAME` | Check configured MCP authentication |
| `mcp logout NAME` | Remove stored MCP authentication |
| `mcp doctor [NAME] [--json]` | Validate and diagnose MCP server connectivity |
| `perf report` | Run offline CLI, TUI startup, composer, spinner and idle-render diagnostics beside their budgets |
| `sessions list [--page N] [--page-size N]` | On a terminal, open the agents hub's History tab (saved sessions with finished agents); piped, print one page of saved sessions |
| `sessions show SESSION_ID` | Show detail for one session |
| `sessions inspect SESSION_ID [--json]` | Inspect one session's durable state |
| `sessions export SESSION_ID [--output PATH]` | Export one session as a redacted JSON document |
| `session create [--project-root PATH] [--agent NAME] [--workflow NAME]` | Create a client-neutral session |
| `session list [--project-root PATH] [--json]` | List client-neutral sessions |
| `session show SESSION_ID [--json]` | Show a client-neutral session snapshot |
| `session events SESSION_ID [--after N]` | Replay durable session events as JSON-lines |
| `session export SESSION_ID [--output PATH]` | Export a redacted client-neutral session |
| `session send SESSION_ID TEXT` | Submit a message to a client-neutral session |
| `session control SESSION_ID KIND [--payload JSON]` | Submit a JSON session control command |
| `session serve [--host HOST] [--port PORT] [--auth-token TOKEN]` | Serve local session snapshots and events over HTTP/SSE |
| `skills add SOURCE [--project \| --global] [--name NAME] [--skill NAME[,NAME]] [--all]` | Download and install a skill |
| `trust list` | Show this project's Python extensions under `.agenthicc/` and whether each is trusted |
| `trust add [PATH]` | Trust the current content of PATH (a file, or every Python file under a directory); without PATH, every project extension |
| `trust revoke PATH` | Revoke trust for a file or every file under a directory |
| `workflows list [--json]` | List available workflow plugins |
| `workflows run WORKFLOW_NAME --intent TEXT [--json]` | Run a workflow headlessly for one intent |
| `worktrees trust [REPOSITORY] [--confirm]` | Preview or trust the current worktree environment manifest digest |
| `worktrees dispatch ORCHESTRATION_ID TASK_ID [--approve] [--json]` | Dispatch a task, optionally approving setup for this run |
| `worktrees actions WORKER_SESSION_ID [--json]` | List named actions in one worker worktree |
| `worktrees action WORKER_SESSION_ID ACTION_ID [--approve] [--json]` | Review or run one worker action |
| `worktrees setup WORKER_SESSION_ID [--approve] [--json]` | Run worker setup explicitly |

See [worktree environments and actions](../guides/worktree-environments.md)
for manifest format, trust, cache isolation, and action execution behavior.
See [scheduled workflows](../guides/scheduled-workflows.md) for recurrence,
timezone/DST, scheduler-service, coalescing, revision, and recovery semantics.

## Offline performance diagnostics

`agenthicc perf report` runs the fixture-free local probes for CLI CPU and
imports, interactive startup, composer rendering, spinner output and idle TUI
work. It uses temporary homes, does not contact a provider, and prints the
measurements beside the checked-in budgets. The report includes Python,
platform, CPU and load context; wall time is diagnostic and is not gated by the
shared-runner CI scope. `PASS`, `OVER`, and `STALE` distinguish budget status;
`DIAG` marks metrics not gated in the selected scope.

Maintainers can run the complete CI probe set (including the 1,000-session
agents profile and hermetic 510-step goal-flow replay) and save its report:

```bash
uv run python scripts/perf_budget.py --scope ci --check --report .nox/perf-report.json
```

The weekly scope also runs the full synthetic 10,000-session / 1,000,000-event
agents profile and retains the raw benchmark results in the JSON report. It gates
CPU-time budgets for first-page access and search, along with idle CPU and zero
appended events; change-to-visible wall time is diagnostic:

```bash
uv run nox -s perf_weekly
```

The Nox session uses the CI Python 3.12 interpreter and writes
`.nox/perf-weekly-report.json`.

`--update` lowers two-way exact-count budgets after a measured improvement; it
refuses to raise them. A budget increase requires explicit review under the
repository's performance-budget policy.

!!! warning "The 10k benchmark is resource intensive"
    The weekly profile builds a temporary 1,000,000-event fixture and can take
    several minutes. It does not overwrite user session data.

!!! warning "`session send` takes a positional message"
    The message is the second positional argument, not a `--text` flag:

    ```bash
    agenthicc session send 8f3c1a2b "run the test suite"
    ```

    Earlier revisions of this page documented `session send SESSION_ID --text
    TEXT`. No such flag exists in the parser; `argparse` rejects it.

## Two session surfaces

The singular `session` group and the plural `sessions` group are deliberately
different, and neither replaces the other.

- **`sessions`** manages the durable TUI sessions stored under
  `~/.agenthicc/sessions/`: the picker, durable-state inspection, and support
  export. See [Storage](storage.md).
- **`session`** is the client-neutral projection served from
  `~/.agenthicc/session-service/`. Its commands read the service projection and
  event cursor rather than writing kernel or conversation journals directly.

`session serve` binds to loopback by default, does not start an agent runner,
and requires `--auth-token` before a non-loopback bind is accepted.

## Session ownership

Session attachment is single-owner. The owner lease is acquired before
transcript, journal, provider, tool, or workflow startup. The sessions picker
shows `available`, `active`, `recoverable`, or `unknown` owner state without
claiming a row; Enter uses the same coordinator as `--resume`. There is no
force-unlock command. A crashed owner is reclaimed only when local process
liveness proves it is dead. See
[Session ownership and resume races](storage.md#session-ownership-and-resume-races).

## `skills add` sources

`agenthicc skills add SOURCE` installs into the current project's
`.agenthicc/skills/` by default. Use `--global` for `~/.agenthicc/skills/` or
`--project` to make the project target explicit.

`SOURCE` may be:

- a local skill directory or `SKILL.md` file,
- a local repository,
- a direct HTTPS `SKILL.md` URL,
- a GitHub repository URL (including a `.git` suffix),
- GitHub `owner/repo` shorthand, or
- a GitHub `/tree/<revision>/<path>` or `/blob/<revision>/<path>/SKILL.md` link.

Repository sources discover all valid skills by default. `--skill NAME[,NAME]`
selects specific skills and `--all` explicitly selects the full discovered set.
Use `--name` only when installing one skill and overriding its directory name.
Existing skills are never overwritten.

## `mcp add` behaviour

`agenthicc mcp add NAME URL` appends a validated `[[tools.mcp_servers]]` entry
to the project configuration by default. Use `--global` for the user config and
`--project` to make project scope explicit.

- `--transport` selects `stdio`, `ws`, `websocket`, `streamable`, or `http`.
- `--token-env ENV_VAR` stores an environment-variable reference such as
  `${MCP_TOKEN}`; the command never accepts or prints a raw token.
- `--no-auto-connect`, `--reconnect-attempts`, and `--reconnect-delay-seconds`
  configure the existing MCP bridge.

The command only updates configuration; it does not start or connect to the
server. When the stdio URL is an existing local directory (or a `.py` server
file), the CLI stores a `uv run --project ... lmcp run ... --stdio` launcher.

Runtime lifecycle commands are intentionally bounded. `connect`, `refresh`, and
`doctor` create a short-lived manager for CLI diagnostics and always close it;
`/mcp connect NAME`, `/mcp disconnect NAME`, and `/mcp refresh NAME` control the
manager owned by the active TUI session. `list`, `get`, and `doctor --json`
never print bearer tokens, secret headers, environment values, or raw command
secrets. `mcp auth` and `mcp logout` cover environment-backed and
provider-supported credential references; they do not mutate the caller's
environment.

## TUI slash commands

TUI commands are a separate registry from CLI subcommands. The built-ins are
`/help`, `/commands`, `/tools [reload]`, `/workflows [runs|reload]`, `/status`,
`/history [repair]`, `/mode`, `/workflow`, `/init`, `/model`, `/models`,
`/skills [reload]`, `/mcp`, `/config`, `/compact`, `/replay`, `/cancel`,
`/loop`, `/loops`, `/clear`, and `/expand`.

`/loop [interval] <prompt-or-registered-command>` schedules a recurring local
session prompt. `/loop status`, `/loop pause`, `/loop resume`, and `/loop stop`
control it. The scheduler is available only in the interactive TUI; it does
not add a CLI subcommand or a headless background daemon. See the [slash
command usage page](../usage/06-commands.md#recurring-session-prompts) for
syntax, safe-dispatch, restart, and payload rules.

`/loops` opens the durable schedule-job table. It shows all valid loop records
under the session store; Enter requests an immediate run of the selected job,
and `d` then Enter permanently deletes it after confirmation. Ownership rules
prevent a live foreign session from being raced.

Default project-authoring skills also provide `/create-tools <instructions>`
and `/create-commands <instructions>`. They send the supplied instructions to
the lauren-ai agent with repository-specific implementation, testing, and
security guidance; generated Python remains executable project code and must be
reviewed. See the [user-defined commands guide](../guides/commands.md) and the
[user-defined tools guide](../guides/tools.md).

`/workflow` and `/compact` are intercepted by `TUISession` because they need
session-local state. Both must remain visible in picker completion as well as
executable when submitted.

To author a workflow interactively, submit `/workflow create_workflow`, then
enter the intent as the next ordinary input. Its phases are
`design → generate → validate → summarize`: the design is gated on your
approval, the generate phase writes a complete package directly to
`.agenthicc/workflows/<name>/runner.py` (with workflow-specific helpers in the
same directory), and the validate phase imports that package and loops back to
generate until it loads cleanly. There is no staging directory or publish
phase. Run `/workflows reload` after the run completes. The authoring run uses
the same durable checkpoint contract as every other workflow:
`/workflow resume [run-id]` continues its typed state after a pause or process
restart. `/workflow reset` returns to the active mode's default workflow, and
`/workflow reset [run-id]` explicitly discards a saved run.

Project tools and commands remain available through the `/create-tools` and
`/create-commands` skills and their respective reload commands; they are not
workflow selectors.

`/tools` and `/workflows` are registry overlays. `/workflows runs` opens the
paginated newest-first paused-run selector, and Enter resumes the selected run
through the guarded `/workflow resume <run-id>` path. Adding `reload` rescans
their respective live registries without restarting. `/tools` shows the
effective built-in, project, and MCP tools for the session and labels each one
`builtin` or `plugin`. `/workflows` shows loaded workflow sources, phases, mode
bindings, and whether the plugin provides a custom runner. Press Enter on a
details page to place the selected command, skill, or workflow invocation in
the input panel without submitting it.

`/init` is a local project guidance command. It previews by default and uses
`/init write` or `/init write --force` for explicit writes; it does not invoke
the model or inspect arbitrary source files.

Workflow recovery notices wrap rather than truncate run IDs. When multiple runs
are available, the complete IDs remain visible so one can be copied into
`/workflow resume <run-id>`.
