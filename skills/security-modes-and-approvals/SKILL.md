---
name: security-modes-and-approvals
version: 1.0.0
tags: [security, modes, approvals, capabilities, permissions, safe, plan, yolo]
description: >-
  Control what the agent may do: the Safe, Plan, and Yolo runtime modes, the
  ToolCapability taxonomy, the two ordered tool gates (hard capability block
  then soft approval), approval responses and scope grants, and the CLI flags.
---

# Skill: Security Modes and Approvals

Every tool call passes through two ordered gates. The first is a hard capability
block driven by the active mode; the second is a soft approval prompt. This skill
explains the taxonomy, the modes, both gates, and the approval bookkeeping that
decides whether the user is asked at all.

## When to use this skill

Use this skill when you need to:

- Decide which mode a task should run in
- Add a tool and declare its capabilities correctly
- Understand why a tool was blocked rather than prompted
- Explain why a second call was not prompted after the first was allowed
- Configure unattended runs deliberately rather than accidentally

Do **not** use this skill for the event/state kernel — see
`kernel-events-and-reducers` — or for headless output contracts, which are in
`headless-mode`.

---

## Two types named `AppState`

This trips people up, so state it once. The gates read
`agenthicc.tui.conversation_store.AppState`, which carries
`active_mode: Signal[RuntimeMode]` (`src/agenthicc/tui/conversation_store.py:648`).
That is a different class from the immutable kernel
`agenthicc.kernel.AppState` (`src/agenthicc/kernel/state.py:143`). Both files
import it under the name `AppState`
(`src/agenthicc/tools/capability_gate.py:17`,
`src/agenthicc/tools/approval.py:25`), so the import path is the only
disambiguator. Kernel work takes the `agenthicc.kernel` one; mode and approval
work takes the TUI one.

---

## Capabilities

A tool declares what it can do with a capability tag.
`ToolCapability` (`src/agenthicc/tools/capabilities.py:52`) is a `str` enum with
nine members:

| Capability | Value | Meaning |
|---|---|---|
| `READ` | `"read"` | Reads files or data; no persistent side effect |
| `WRITE` | `"write"` | Creates, modifies, or deletes files or data |
| `EXECUTE` | `"execute"` | Runs shell commands or arbitrary code |
| `GIT_READ` | `"git_read"` | Reads git history, diffs, status, blame |
| `GIT_WRITE` | `"git_write"` | Modifies git state (add, commit, checkout, stash) |
| `NETWORK` | `"network"` | Makes outbound network calls |
| `SEARCH` | `"search"` | Searches content without state changes |
| `CONTROL` | `"control"` | Advances an internal workflow or session state machine |
| `UNDECLARED` | `"undeclared"` | Internal fail-closed marker for missing metadata |

It inherits `str` deliberately so frozenset members serialise as plain strings
and compare correctly against `RuntimeMode.blocked_capabilities`.

Attach a capability with the single-capability decorators — `tool_read`,
`tool_write`, `tool_execute`, `tool_git_read`, `tool_git_write`, `tool_network`,
`tool_search` (`tools/capabilities.py:72-78`) — or a combination such as
`tool_read_search` and `tool_network_read` (`tools/capabilities.py:82-92`).

A tool with **no** declared capabilities is classified `UNDECLARED`, and that is
a fail-closed default: `UNDECLARED` requires approval in Safe and is blocked in
Plan (`tools/capability_gate.py:6-8`). Forgetting the decorator does not make a tool
invisible — it makes it maximally restricted.

---

## The three selectable modes

`RuntimeMode` (`src/agenthicc/tui/runtime/mode_manager.py:50`) is the policy
object:

| Field | Meaning |
|---|---|
| `name` | `Safe`, `Plan`, or `Yolo` |
| `badge` / `color` | Display only |
| `description` | Shown in listings |
| `system_prompt_suffix` | Text appended to the mode's prompt |
| `blocked_capabilities` | Hard-blocked set |
| `approval_required` | Soft-prompt set |
| `default_workflow` | Workflow to run on submit |
| `workflows` | Workflows available in this mode |

The canonical constants are `DEFAULT_MODE_NAME = "Safe"`
(`tui/runtime/mode_manager.py:20`), `SELECTABLE_MODE_NAMES = ("Safe", "Plan", "Yolo")`
(`tui/runtime/mode_manager.py:21`), and `INTERNAL_MODE_NAMES = ("Replay",)`
(`tui/runtime/mode_manager.py:22`).

| Mode | `blocked_capabilities` | `approval_required` |
|---|---|---|
| `Safe` | none | the restricted set |
| `Plan` | the restricted set | none |
| `Yolo` | none | none |

The restricted set is built by `_restricted_capabilities`
(`tui/runtime/mode_manager.py:186`): `WRITE`, `GIT_WRITE`, `EXECUTE`, `NETWORK`,
`UNDECLARED`. So Safe prompts for anything that writes, executes, touches the
network, or lacks metadata; Plan hard-blocks the same set; Yolo allows all of it
without prompting.

`build_safe_mode()` (`tui/runtime/mode_manager.py:200`) exists so any default-state boundary
constructs the identical Safe policy rather than a hand-rolled approximation. Use
it rather than building your own Safe mode.

### Aliases

`MODE_ALIASES` (`tui/runtime/mode_manager.py:26`) maps `auto` -> `Yolo`, `guard` -> `Safe`,
`ask` -> `Safe`, and `review` -> `Plan`. Aliases are resolved at the registry
boundary and are documented as never appearing in `all()`, so they cannot become
duplicate mode-cycle entries (`tui/runtime/mode_manager.py:24-25`).

`ModeRegistry.resolve` raises `UnknownModeError` (`tui/runtime/mode_manager.py:35`)
for an unrecognised name, and its docstring states the accepted set in the error
text. `ModeManager.set_by_name` (`tui/runtime/mode_manager.py:454`) is the
selection entry point used by `/mode` and `--mode`; it accepts a canonical name
or an alias and returns `None` for unknown input or for an internal mode rather
than raising. Resolution is case-insensitive.

### `Replay` is not a mode you can pick

`Replay` is registered with `selectable=False` (`tui/runtime/mode_manager.py:289-298`) and
blocks **all** capabilities, including `READ`. The comment is explicit that it is
a trusted internal replay state, not a permission profile, and it is intentionally
absent from the mode cycle and `/mode` listing.

---

## Gate 1: the hard capability block

`ToolCapabilityGate` (`src/agenthicc/tools/capability_gate.py:24`) is registered
as a global tool hook so it fires for every tool call. On each invocation it
reads `RuntimeMode.blocked_capabilities` **live** from the active mode, which is
why changing mode mid-turn takes effect on the very next tool call
(`tools/capability_gate.py:27-29`).

If any capability intersects the blocked set, it returns
`BeforeToolHookDecision.abort()`. Three things then hold
(`tools/capability_gate.py:31-34`):

1. The model receives `{"ok": False, "error": "..."}` as the tool result.
2. The tool function **never executes**.
3. The second gate never runs.

A block is therefore final for that call. This is a hard block, not a prompt: in
Plan mode no amount of user intent turns a write into an approval prompt.

---

## Gate 2: the soft approval prompt

`ApprovalGate` (`src/agenthicc/tools/approval.py:158`) is the second global hook,
registered after `ToolCapabilityGate`. Its module docstring lays out the flow
(`tools/approval.py:1-13`):

1. `ToolCapabilityGate` runs first; if it aborts, this module never fires.
2. `ApprovalGate.before_tool_call()` compares the tool's capabilities against
   `mode.approval_required`.
3. On an intersection it calls `ApprovalService.request_approval()`, and the
   calling coroutine suspends on `asyncio.Event.wait()` so the event loop stays
   free.
4. The approval overlay is shown.
5. The overlay calls `ApprovalService.respond()`, which sets the event.
6. The gate returns `proceed()` or `abort()` from the response.

### Requests and responses

`ApprovalRequest` (`tools/approval.py:38`) carries `tool_name`, `tool_use_id`,
`tool_input`, the `capabilities` that triggered it, an `event`, a `kind`, the
`mode_options` for a plan-review overlay, and any exact outside-workspace
`workspace_access` requests. `kind` is `"tool"` or `"plan_review"` and selects
which overlay is shown.

`ApprovalResponse` (`tools/approval.py:52`) carries `allowed`, `remember`,
`remember_all`, a free-text `message` (plan review only), an optional `mode`, and
`scope_grant`.

### Remembering an approval

`ApprovalService` (`tools/approval.py:61`) is session-scoped and keeps four pieces of
bookkeeping: `_remembered_turn`, `_remembered_all`, `_scope_turn`, and
`_scope_session` (`tools/approval.py:71-84`). Concurrent approvals are serialised
through an `asyncio.Lock`, because parallel tool calls would otherwise race on
the single pending-approval slot.

The fast paths are what stop the user being asked twice:

- `remember=True` adds the capabilities to the per-turn set, so the rest of that
  turn is silent.
- `remember_all=True` adds them for the session.
- `reset_turn_memory()` (`tools/approval.py:152`) clears the per-turn set and turn
  scopes at the start of each new agent turn.

There is a subtle guard worth knowing about. The fast paths require a **non-empty**
`capabilities` set before matching, and the source explains why
(`tools/approval.py:73-76`): `frozenset() <= frozenset()` is `True` in Python, so
without that check an empty-capability request would auto-approve before the
overlay was ever shown. Plan reviews have no capabilities, so they depend on this
guard to always reach the user.

Workspace scope grants are tracked per `(canonical path, operation)` pair, and
`scope_grant` is `target_once`, `target_turn`, or `target_session`
(`tools/approval.py:58`).

---

## Choosing a mode

| Situation | Mode | Why |
|---|---|---|
| Unfamiliar repository, want to see each write | `Safe` | Prompts for writes, executes, and network |
| Analysis or planning with no side effects | `Plan` | Hard-blocks side effects rather than prompting |
| Trusted automation in a controlled sandbox | `Yolo` | No prompts, all capabilities |
| Replaying a recorded session | `Replay` (internal) | All capabilities blocked; not user-selectable |

Switch with `/mode <name>` (`src/agenthicc/commands/builtins.py:1041`, hint
`[Safe|Plan|Yolo]`), cycle with Shift+Tab, or start a process with `--mode MODE`
(`src/agenthicc/cli/parser.py:32`). Aliases are accepted in all three paths.

### Unattended runs

For a process with no operator, Safe will deadlock on the first prompt — the gate
suspends on an event no one can set. The explicit opt-in is
`--dangerously-skip-permissions` (`src/agenthicc/cli/parser.py:89`), which sets
`CLIFlags.dangerously_skip_permissions`. In headless mode the runner instead
supplies a deny-by-default approval service, which is the safer default; that
mechanism is documented in `headless-mode`.

Note there is no `--break-system-packages`-style flag here, and no way to make
`Plan` unblock a capability: choosing Plan is choosing a hard block.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tool returns `{"ok": false, ...}` and never runs | Hard block by `ToolCapabilityGate` in Plan, or `UNDECLARED` | Switch mode or declare capabilities on the tool |
| Tool is blocked in Plan but only prompts in Safe | Expected: Plan blocks the restricted set, Safe prompts for it | Use Safe when you want a prompt |
| A custom tool is unexpectedly blocked | No capability decorator, so it is `UNDECLARED` | Add `@tool_read`/`@tool_write`/etc. |
| Prompt appears again after an earlier allow | The turn or session memory was reset | Use `remember_all` to cover the session |
| Plan review never appears | An empty-capability request matching a fast path | Not possible: the guard requires non-empty capabilities — check the mode instead |
| Parallel tool calls interleave prompts oddly | They are serialised by design | Expect one prompt at a time |
| A headless run hangs waiting for approval | Safe mode with no operator | Use `--dangerously-skip-permissions` in a sandbox you control, or rely on the headless deny-by-default service |
| `UnknownModeError` from `resolve` | Name is neither canonical nor a known alias | Use `Safe`, `Plan`, `Yolo`, or an alias like `auto`/`guard`/`ask`/`review` |
| `set_by_name` returns `None` and the mode did not change | Unknown name, or an internal mode such as `Replay` | Pass a selectable name or alias |
| `Replay` cannot be selected | Internal state, `selectable=False` | Expected |

---

## Key points

- Two ordered gates: `ToolCapabilityGate` hard-blocks
  (`tools/capability_gate.py:24`), then `ApprovalGate` soft-prompts
  (`tools/approval.py:158`). A hard block means the second gate never runs.
- Capabilities are declared with decorators; **no decorator means `UNDECLARED`**,
  which prompts in Safe and is blocked in Plan.
- The restricted set is `WRITE`, `GIT_WRITE`, `EXECUTE`, `NETWORK`, `UNDECLARED`
  (`tui/runtime/mode_manager.py:186`).
- Safe prompts for the restricted set; Plan blocks it; Yolo allows everything.
- Blocked capabilities are read live on every tool call, so a mode change applies
  from the next call even inside one turn.
- Empty-capability requests bypass the approval fast paths by design, so plan
  reviews always reach the user.
- `remember` covers the turn, `remember_all` the session, and
  `reset_turn_memory()` clears the turn set.
- `Replay` is internal, unselectable, and blocks every capability.
- `--dangerously-skip-permissions` is the only deliberate unattended opt-in.
