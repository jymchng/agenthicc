---
title: "PRD-143: Safe Commands During Active LPM Runs"
status: Implemented
version: 1.0.0
created: 2026-07-24
related_prds:
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-141  # Background Sessions and Session Manager TUI
  - PRD-142  # Dollar-Prefixed Skill Triggers
  - PRD-36   # Slash Command Trigger
  - PRD-39   # Input Trigger System
  - PRD-44   # Unified Command System
  - PRD-74   # Capability-Based Input Dispatch
tags:
  - commands
  - tui
  - streaming
  - lpm
  - responsiveness
  - safety
supersedes: []
---

# PRD-143 — Safe Commands During Active LPM Runs

Study date: 2026-07-24. This PRD evaluates which commands may execute while
the LPM/LLM is responding, which must remain queued until the active run ends,
and which should be rejected while the session is busy. The policy contract is
implemented in the canonical command model, the TUI session interception point,
and the existing input/dispatcher pipeline.

## 1. Executive summary

The interactive session currently treats input submitted while an agent run is
active as queued work. That protects the active run, but it also delays
commands that only inspect local state or control the running session. A user
should be able to type `/usage` and immediately inspect usage/cost information
without waiting for a long response to finish. Cancellation and background
session controls must also remain responsive.

PRD-143 introduces a centrally evaluated busy-state policy:

- safe, bounded, read-only commands execute immediately;
- run-control commands such as cancel or background execute immediately through
  an explicit control lane;
- commands that mutate session/run configuration or start another agent action
  remain queued; and
- commands that cannot safely be defined during an active run are rejected with
  an actionable explanation.

Unknown and user-defined commands default to queueing unless their trusted
command contract explicitly opts into a reviewed immediate policy. The policy
is evaluated before the existing streaming queue accepts input. Immediate
commands use the existing command registry and dispatcher, so this feature
does not create a second command execution path.

## 2. Problem statement and evidence to audit

When the LPM is generating a response:

1. every submitted command appears to wait behind the active run;
2. a user cannot inspect usage, status, or other read-only state when needed;
3. cancellation and backgrounding feel unreliable if they share the queue;
4. users cannot tell whether a command executed, was queued, or was refused; and
5. allowing all commands immediately could change workflow, memory, model,
   configuration, or agent state in the middle of a run.

The desired behaviour is selective responsiveness, not unrestricted concurrent
mutation.

Before implementation, confirm these current ownership boundaries:

| Concern | Source to audit | Question |
|---|---|---|
| Interactive submission | `src/agenthicc/runners/tui_session.py`, `tui/input/unified_session.py` | Where can a command be intercepted before busy input is queued? |
| Commands | `src/agenthicc/commands/command.py`, `builtins.py`, plugin loading | Which commands read, mutate, control, or start work? |
| Dispatch | `commands/dispatcher.py`, `TUISession.route()` | Can classification and execution use one dispatcher? |
| Busy state | `tui/input/capabilities.py` and session task state | What is authoritative for “LPM responding”? |
| Queue | Current session queue/runtime command bus | Are original text, ordering, and status preserved? |
| Usage | Existing token/cost/usage owner and `/usage`, if present | Can a consistent local snapshot be read without touching the LPM task? |
| Controls | Interrupt path and PRD-141 background lifecycle | Which controls bypass the ordinary message queue? |
| Rendering | Workspace/appender/session-log owner | How is immediate output rendered without corrupting a stream? |

Historical paths must not be introduced during this audit.

### 2.1 Terminology

- **Active run** — the session has an LPM task generating, streaming, awaiting
  a provider, or executing the current agent turn.
- **Immediate** — complete before the next queued user turn and do not alter
  the active request, except for explicit run-control operations.
- **Queued** — retain the original command and dispatch it after the active run
  reaches a terminal state, in FIFO order.
- **Rejected** — do not retain or dispatch it; explain why and what to do next.
- **LPM** — the active language-model response/run in the current lauren-ai
  runtime.

## 3. Goals and non-goals

### Goals

- Make approved read-only commands responsive while the LPM is responding.
- Keep cancellation and background-session controls responsive.
- Preserve queueing for commands affecting the active run or future session
  semantics.
- Give every busy command a deterministic immediate/queue/reject decision.
- Keep one command registry, dispatcher, and metadata source.
- Make executed-now, queued, and rejected states visible.
- Support subcommand-aware policies such as `/skills` versus `/skills reload`.
- Keep interactive, non-interactive, and headless semantics explicit.

### Non-goals

- Concurrent tools, skills, workflows, or agent turns during an LPM response.
- Changing provider cancellation, retry, timeout, or streaming semantics.
- Making model, workflow, memory, configuration, MCP, or reload mutations
  immediately concurrent by default.
- Adding a second registry, dispatcher, queue, or execution pipeline.
- Treating a command description as a security guarantee.
- Changing PRD-142 `$skill-name` behaviour; skills remain agent actions.
- Changing headless JSON-lines semantics without a separate contract.

## 4. User-facing contract

### 4.1 Busy outcomes

| Outcome | User experience | Queue effect |
|---|---|---|
| Run now | Output or confirmation appears immediately. | No queue entry. |
| Queued | A notice says it will run after the current response, with position when available. | Original text is retained once. |
| Rejected | A reason and alternative such as retry, `/cancel`, or `/background` are shown. | Nothing is retained. |

The outcome must be observable through the existing notification, queue, or
status surface; it must not be inferred from a redraw.

### 4.2 Recommended immediate read-only set

Confirm this matrix against the actual registry before implementation. Add
`/usage` as a built-in if no authoritative usage command currently exists.

| Command/surface | Policy | Conditions |
|---|---|---|
| `/usage` | Run now | Local consistent token/cost/usage snapshot; no provider call. |
| `/status` | Run now | Read-only session/run status; no reset or mutation. |
| `/help` | Run now | Static help or read-only command list. |
| `/commands` | Run now | List the registry; reload is a different policy. |
| `/skills` | Run now | List discovered/allowed skills; reload is a different policy. |
| `/mcp` | Run now for status | Connect/disconnect/discovery are not read-only. |
| `/history` | Run now | Browse local history; never replay or mutate the active transcript. |
| `/expand` | Run now if view-only | Expand existing local output; no tool/agent work. |
| `/clear` | Run now if UI-only | Redraw only; never erase durable or active-run state. |

### 4.3 Recommended immediate control set

| Command/surface | Policy | Requirement |
|---|---|---|
| `/cancel` or `/interrupt` | Run now | Existing cancellation path; idempotent. |
| `/bg` or `/background` | Run now | PRD-141 lifecycle; detach without duplicating the run. |
| Session-manager controls | Run now when explicitly scoped | Use the control plane, not a user message injected into the run. |

Controls may affect the active task by design and must publish their state
transition before queued work is released.

### 4.4 Queued and rejected surfaces

Keep these queued while busy: workflow changes, provider/model changes,
configuration edits, `/skills reload`, `/commands reload`, MCP actions,
compaction, replay, initialization, file/tool/command creation, and anything
that starts a tool, workflow, skill, or agent turn. `$skill-name` remains queued.

Reject only operations with no safe deferred meaning, idle-only modal
interactions that cannot preserve the active input context, malformed busy-only
controls, or plugins that explicitly declare themselves non-deferable. A
command with a safe deferred meaning should queue rather than reject.

### 4.5 Picker and manual input

- The `/` picker marks immediate commands available now.
- Queued commands remain discoverable and say “queues while busy”.
- Rejected commands may be disabled or hidden, but manual submission has the
  same deterministic reason.
- `/usage` output must not become a user message or reach the LPM.
- Normal prose and `$skill-name` retain existing queue behaviour.

## 5. Policy model

### 5.1 Typed central decision

Add a typed busy-policy contract to the canonical command model or its owning
policy module. The equivalent contract is:

```text
BusyPolicy = immediate-read-only | immediate-control | queue | reject
BusyDecision = policy + reason + optional queue label
```

The pure resolver receives the parsed command token and arguments plus a
read-only runtime-phase snapshot. It cannot perform I/O, mutate state, or call
a handler. The handler remains responsible for normal execution; the resolver
only determines when the existing dispatcher may invoke it.

### 5.2 Defaults and trust

- Built-ins receive explicit reviewed policies.
- User/project command plugins default to `queue`.
- Plugin opt-in to immediate read-only execution requires a typed contract
  documenting side effects and latency; trust and capability checks remain
  authoritative.
- Plugins cannot claim immediate-control without an explicit control contract.
- Missing/malformed metadata, resolver exceptions, and unknown commands fail
  closed to queue or existing unknown-command behaviour.
- An immediate read-only handler must not receive active-run mutation callbacks.

### 5.3 Subcommand policy

| Surface | Read-only form | Action form |
|---|---|---|
| `/skills` | Run now | `/skills reload` queues |
| `/commands` | Run now | `/commands reload` queues |
| `/mcp` | Status runs now | Connect/disconnect/discovery queues or rejects |
| `/model` | Pure query may run now | Provider/model change queues |
| `/config` | Future pure view may run now | Editor/apply queues |

Unknown or malformed subcommands use the existing command error path and
cannot bypass the busy policy.

## 6. Runtime design

### 6.1 Intercept before the ordinary queue

At the point where submitted input becomes a `SendMessageCommand` or equivalent:

1. parse the first token without changing original text;
2. obtain the authoritative active-run phase snapshot;
3. classify through the pure resolver;
4. execute immediate commands through the existing dispatcher/control path;
5. enqueue queueable commands exactly once; or
6. emit rejection and discard the submission.

The idle path continues using the same dispatcher. Queued commands are
revalidated at release; changed registry/policy state produces a deterministic
failure rather than stale execution.

### 6.2 Immediate lanes

Read-only commands run on the event loop using bounded local operations. They
must not await the active LPM, start a second agent task, or write an agent/user
message. They may update an existing notification, read-only overlay, usage
panel, status region, workspace, or session log through its owning renderer.

Cancellation/backgrounding use the existing task/control owner, are idempotent,
and publish their transition before releasing queued work. Backgrounding must
not create a second runner or persistence system outside PRD-141.

### 6.3 Queue and rendering

Queue entries preserve original text, session id, enqueue time, and a stable
reason. Immediate commands consume no queue position. FIFO ordering and
exactly-once release are mandatory. Cancellation/background rules for queued
work must be explicit in the PRD-141 integration.

Immediate output uses the existing workspace/appender/session-log owner; it
never writes raw terminal output into a provider stream. Usage values are read
as one consistent snapshot of tokens, cost, and active-run state.

## 7. Command inventory deliverable

Before coding, produce a checked-in inventory from the current registry and
handlers. For every command record it must capture canonical name, aliases,
source/trust, arguments, reads/mutations, tool/network/filesystem use, task
lifecycle effects, latency/I/O, busy policy, rationale, and test coverage.

Explicitly audit `/usage`, `/status`, `/help`, `/commands`, `/skills`, `/mcp`,
`/history`, `/expand`, `/clear`, `/cancel`, `/interrupt`, `/bg`, `/background`,
`/workflow`, `/model`, `/config`, `/compact`, `/replay`, and all reload/action
subcommands if they exist. Do not assume a command exists because a PRD names
it. The inventory is a review artifact, not a second runtime registry.

### 7.1 Implemented inventory

This is the checked-in review inventory for the current built-in registry. A
plugin command not listed here uses the `queue` default. `/bg` and
`/background` are added by the PRD-141 foreground bridge when that integration
is installed.

| Surface | Source/trust | Reads or mutates | Busy policy | Coverage |
|---|---|---|---|---|
| `/usage` | built-in | local token/cost/run snapshot; no provider | immediate read-only | unit, integration, E2E |
| `/status` | built-in | local session/model labels | immediate read-only | built-in dispatch + policy |
| `/help`, `/commands` | built-in | command registry/overlay; reload mutates registry | immediate; reload queues | policy + picker |
| `/skills` | built-in | discovered skills; reload mutates registry | immediate; reload/action queues | policy + existing reload tests |
| `/mcp` | built-in | status is local; connect/discovery are actions | status immediate; actions queue | policy |
| `/history`, `/expand`, `/clear` | built-in | local view/UI state | immediate read-only | policy + picker |
| `/cancel`, `/interrupt` | built-in control | active task cancellation | immediate control | TUI + existing interrupt tests |
| `/bg`, `/background` | PRD-141 built-in bridge | background handoff/control | immediate control | background edges + policy path |
| `/model` | built-in | no-arg query is local; provider/model args mutate future state | query immediate; action queues | subcommand policy |
| `/config`, `/mode`, `/workflow` | built-in | overlay or future session/workflow state | queue | policy |
| `/compact`, `/replay`, `/init` | built-in | memory, transcript, or filesystem/workflow actions | queue | policy |
| `$skill-name`/`$alias` | skill-owned | starts an agent action | queue | policy + skill tests |
| project/user commands | plugin | unknown until reviewed | queue by default | policy unit test |

Policy is resolved from the command record and parsed arguments before a busy
submission is queued. The inventory is documentation only; it is not a second
command list.

## 8. Security and failure handling

- “Read-only” is reviewed capability metadata, not a command-name string match.
- Immediate commands cannot bypass mode, agent, plugin trust, tool,
  filesystem, network, or approval boundaries.
- Immediate failures become structured local errors and never fall through to
  an LPM user message.
- Policy failure fails closed without logging secrets or prompt contents.
- Repeated cancel/interruption/background submissions create at most one
  lifecycle transition.
- Registry reload cannot race command classification; use the existing atomic
  registry boundary.
- Usage snapshots tolerate partial updates without impossible values or crashes.

## 9. Rollout

### Phase 1 — Inventory and policy contract

Audit the registry, add typed policy/default queue behaviour, document the
initial matrix, and add local policy-decision diagnostics without telemetry.

### Phase 2 — Read-only responsiveness

Intercept busy submissions before queueing. Enable `/usage`, `/status`, and the
smallest reviewed read-only set. Add picker labels, notices, and snapshot tests.

### Phase 3 — Control and subcommands

Enable cancellation/background controls, add subcommand-aware policies, and
verify reload and lifecycle races.

### Phase 4 — Trusted extensions

Consider reviewed plugin opt-in only after built-ins are stable. Unclassified
plugins remain queue-only.

## 10. Acceptance criteria

- **A1** — Every canonical command has a deterministic busy decision or uses
  documented queue-by-default behaviour.
- **A2** — The inventory covers all current built-ins, aliases, subcommands,
  plugin defaults, and policy rationales.
- **A3** — Classification is pure and cannot invoke handlers, I/O, providers,
  or agent operations.
- **A4** — `/usage` runs during an active LPM response, shows a consistent
  local snapshot immediately, is not queued, and never reaches the LPM.
- **A5** — Approved read-only commands, including `/status`, have the same
  immediate/no-mutation guarantees.
- **A6** — Cancel/interruption and `/bg`/`/background`, when present, remain
  responsive and use existing control ownership.
- **A7** — Normal messages and mutating/action commands remain FIFO queued.
- **A8** — Workflow/config/model changes, reloads, compaction, replay, skills,
  and agent-starting operations cannot use the immediate lane.
- **A9** — Run-now, queued, and rejected outcomes are distinguishable to users.
- **A10** — Queued commands execute once after terminal completion, revalidate,
  and never disappear silently.
- **A11** — Busy picker labels and manual submissions agree.
- **A12** — No policy bypasses trust, mode, agent, tool, filesystem, network,
  or approval boundaries; headless behaviour remains unchanged.

## 11. Verification plan

### Unit tests

- Policy types, defaults, pure classification, subcommands, and fail-closed
  resolver errors.
- `/usage` snapshot consistency, no mutation, and no provider call.
- Immediate/queued/rejected dispatcher outcomes.
- Queue position, FIFO ordering, exactly-once release, and revalidation.
- Idempotent cancel/background decisions and busy picker labels.

### Integration tests

- A fake active LPM task plus `/usage`: immediate output, empty command queue,
  and no agent-message event.
- Normal text, `/workflow`, reload, and mutating plugin command: queued until
  terminal completion and then dispatched once.
- `/cancel` and `/background`: immediate task/control transition and documented
  queued-work lifecycle.
- Subcommand and registry-reload race coverage.
- Real workspace/appender rendering while a response is streamed.

### End-to-end tests

- Slow fake provider response with interactive `/usage` output visible before
  response completion.
- Interactive queued command and normal message preserving active response and
  FIFO order.
- Busy picker labels matching manual-submission outcomes.
- Live cancellation/backgrounding without duplicate runs or lost persistence.
- Repository unit, integration, E2E, Ruff, mypy, type-audit, and documentation
  checks from the current contributor instructions.

## 12. Implementation ownership

| Change | Owner |
|---|---|
| Policy type/metadata | `src/agenthicc/commands/command.py` or current command-policy owner |
| Pure classification | `src/agenthicc/commands/` beside registry/dispatcher |
| Busy interception | `src/agenthicc/runners/tui_session.py` and current runtime command path |
| Busy picker state | `src/agenthicc/tui/input/`, trigger handlers, and `TriggerContext` if needed |
| Usage snapshot | Existing token/cost/usage owner; no second counter |
| Queue/order | Current TUI session queue/runtime command-bus owner |
| Rendering | `src/agenthicc/tui/workspace/` and existing appender/session-log owner |
| Background/cancel | Existing control owner and PRD-141 lifecycle |
| Inventory/docs | `prds/`, command/TUI guides, and README where user-visible |

Do not introduce historical `tui/app.py`, `tui/events.py`, `tools/hooks.py`, or
`tools/executor.py` ownership boundaries.

## 13. Implementation decisions and evidence

1. `/usage` was absent, so it now reads the existing reactive conversation
   counters and reports input, output, total, cost, run state, and queue depth.
2. `/clear`, `/history`, `/expand`, `/model` query, and the other local query
   surfaces in the inventory are immediate; mutating forms remain queued.
3. Backgrounding and cancellation continue to use PRD-141 and the existing
   task/control owners. Queued work remains in the current foreground queue and
   is revalidated when released.
4. The picker keeps rejected/queued commands discoverable and labels the
   outcome; manual submission uses the same resolver.
5. Project/user commands default to queue. Immediate-control is reserved for
   built-in controls until a reviewed plugin control contract exists.
6. Immediate output uses the existing Rich console/notification owners; no
   second transcript, usage counter, dispatcher, or persistence path exists.

Implementation evidence:

- Policy model/classifier: `src/agenthicc/commands/command.py` and
  `src/agenthicc/commands/busy_policy.py`.
- Busy interception, control callbacks, usage snapshot, queue revalidation:
  `src/agenthicc/runners/tui_session.py`.
- Picker labels: `src/agenthicc/tui/trigger.py`,
  `src/agenthicc/tui/triggers/slash_command.py`, and the current trigger
  overlay/input session.
- Tests: `tests/unit/test_busy_command_policy.py`,
  `tests/unit/test_busy_commands_tui.py`,
  `tests/integration/test_busy_commands_integration.py`, and
  `tests/e2e/test_busy_commands_e2e.py`.
- Verification for this implementation: `uv run pytest tests/ -q` passed with
  2243 passed, 15 skipped, and 4 pre-existing warnings; Ruff check/format,
  mypy, type-audit, and `uv run nox -s llms_check` also passed.

## 14. Related documentation

- [PRD-138 — Repository Improvement Roadmap](prd-138-repository-improvement-roadmap.md)
- [PRD-139 — OpenCode-Inspired Product Expansion and Privacy-First Advertisements](prd-139-opencode-inspired-features-and-privacy-first-ads.md)
- [PRD-141 — Background Sessions and Session Manager TUI](prd-141-background-sessions-and-session-manager-tui.md)
- [PRD-142 — Dollar-Prefixed Skill Triggers](prd-142-dollar-prefixed-skill-triggers.md)
- [Commands guide](../docs/guides/commands.md)
- [TUI guide](../docs/guides/tui.md)
- [Background sessions guide](../docs/guides/background-sessions.md)
