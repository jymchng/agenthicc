---
title: "SUBAGENT-16: Non-blocking subagents by default — background spawn, wake on completion, and a main session that keeps working"
status: Implemented
version: 1.0.0
date: 2026-11-23
repository: jymchng/agenthicc
priority: P0
scope: "stop `spawn_subagents` from holding the main session: make a spawned pool run in the background by default, finish the half-built completion path so a finished pool wakes the session (idle) or reaches the running turn (busy) without a user message, make that wake durable and reconciled, and keep the person informed and in control — emulating how Claude Code runs subagents, without its lost-wake failure mode"
area: A-16
related_prds:
  - SUBAGENT-07
  - SUBAGENT-08
  - SUBAGENT-11
  - SUBAGENT-12
  - SUBAGENT-15
  - WFC-01
  - WFC-03
  - AGX-08
  - AGX-10
  - TUIR-04
  - TUIR-05
tags:
  - subagents
  - background
  - tool-surface
  - tui
  - session-state
  - wake
---

# SUBAGENT-16: Non-blocking subagents by default

> Studied at commit `bebe522` (2026-11-23). File and line references are for
> that commit; re-verify each before implementation. Claude Code behaviour is
> cited with its evidence tier (§3, Appendix A).

## 1. Executive summary

When the main agent calls `spawn_subagents`, **the whole session waits for the
pool**: the tool call is awaited to completion (up to one hour per worker by
default), the model's next request is withheld, and anything the person types
waits behind it. A background mode exists, but it is opt-in, and it is only
half built: when a background pool finishes, **nothing starts a turn**, so the
promised "you will be notified" does not happen until the person sends another
message.

Claude Code solves this the other way round. A subagent it spawns from the main
conversation **starts in the background by default**; the main conversation
keeps talking to the person, and the finished subagent's report arrives as an
automated completion message that re-invokes the main agent. Waiting is
something an operator opts into, not something the model chooses.

This PRD brings that behaviour to agenthicc, in four moves:

1. **Background by default.** A spawn from a turn that can receive a completion
   returns a receipt at once and the turn keeps going. The model's `background`
   parameter is removed; the runtime decides. An operator can restore blocking
   spawns with one setting.
2. **Completion reaches the model without a user message.** An idle session is
   woken into a turn; a running turn receives the notice at its next tool
   boundary; the existing turn-start delivery stays as the fallback.
3. **The wake is durable.** The journal is the truth and the wake is only a
   hint; a reconciler recovers a lost wake. This is the lesson of Claude Code's
   open "idle subagent never re-woken" bug.
4. **The person can see and stop it.** The notice renders as an automated event,
   running pools stay visible, and Esc during a wake turn pauses automatic
   wakes.

No new subagent limit is introduced. Foreground behaviour remains available and
is used where blocking is the contract (workflow phases, runners that cannot
receive a wake).

## 2. Problem statement

The reported failure: *the subagent tool blocks the main session, so the main
session cannot make progress on other tasks while subagents run.* It decomposes
into eight observed facts. Each is code-reading evidence from this checkout;
§9 (A1) requires a baseline measurement before any change.

| # | Finding | Evidence |
|---|---|---|
| P1 | **Foreground is the default, and nothing steers the model away from it.** `spawn_subagents(background: bool = False)`. Only the parameter's own docstring mentions backgrounding; no text in the main system prompt does. SUBAGENT-11 kept this on purpose (goal 10: "Defaults change nothing a user does today"). | `subagents/tool.py:302`, `:321-324`; `prds/SUBAGENT-11-…md` §4 goal 10; `rg 'background' runners/agent_turn.py` finds no prompt text |
| P2 | **A foreground call awaits the whole pool.** The tool waits on the pool task and then returns its aggregate. Workers now run until they finish (PR #30 removed the turn, iteration, tool-call, budget and pool wall-clock ceilings); the only wall-clock bound left is the per-call `timeout_s`, default 3,600 s, so one call can hold a turn for up to an hour or more. | `tool.py:832-838` (`asyncio.wait(..., FIRST_COMPLETED)`), `:920` (`result = await pool_task`); `subagents/types.py:44`; commit `1383170` |
| P3 | **It stalls the whole tool batch, not just itself.** The main session runs with `parallel_tool_calls=True`. lauren-ai gathers a response's sibling tool calls but returns only when **all** have finished, and only then commits the results. A `read_file` or `edit` the model issued beside the spawn has its result withheld, and so does the next model request. | `runners/agent_turn.py:3067`; lauren-ai 1.6.1 `_agents/_runner.py::_execute_tools` (`asyncio.gather`) |
| P4 | **Whatever the person types waits as well.** Queued input is claimed only at a completed tool boundary, i.e. after `super()._execute_tools` returns. During a foreground pool the person can wait, press **Ctrl+B** (offered only while a pool is active), or press **Esc**, which cancels the pool and loses its work. | `agent_turn.py:1775-1814` (`_QueuedInputRunner`); `runners/tui_session.py:4638` (`_next_queued_message`); `tool.py:921-925`; `tui/actions.py:355-363` |
| P5 | **Background is half-wired: nothing runs when a background pool finishes.** Completion notices are delivered by `_deliver_completed_background_subagents`, called once at the start of a new turn (`agent_task_body`, only when `resume is None`). No code starts a turn on completion, and none injects the notice into a turn already running. The guide says so: it "is not inserted into an already-running provider loop". Meanwhile the receipt tells the model "You will be notified when it finishes", and the model, told not to poll, goes quiet. With an idle session the notice waits for the person's next keystroke. | `tui_session.py:5516`, `:5921`; `subagents/delivery.py`; `docs/guides/subagents.md` § Background pools; `tool.py:854-859` |
| P6 | **The notice looks like something the person typed.** It is appended as a `user_message` event with `source: "subagent_completion"`, but the renderer ignores `source` and draws every `user_message` as a `❯` user band. A turn that starts by itself would show a message the person never wrote. | `tui/workspace/appender.py:1067-1088`; `delivery.py` (`deliver_background_pool_notices`) |
| P7 | **Background exists only in the TUI.** One call site binds the supervised task owner. Every other runner's turn gets `background_unavailable` and must run foreground. | `tui_session.py:5922-5925`; `tool.py:585-594` |
| P8 | **The remaining wait primitive cannot be interrupted by the person.** `collect_subagent_results(wait=True)` blocks for up to `collect_wait_max_s` (60 s); queued input is claimed after it returns. | `config.py` `SubagentSettings.collect_wait_max_s`; `subagents/communication.py` collect path |

**Consequence.** The model chooses foreground almost every time (P1), so a
single delegation freezes the agent and the composer for the length of the
slowest worker (P2–P4). The escape hatches are either invisible (Ctrl+B) or
destructive (Esc). The background path that would fix it does not deliver its
result on its own (P5), so even a model that opts in leaves the person staring
at an idle session that has, in fact, finished.

**What is not broken.** The pool, its lifecycle journal, collection, stop and
retry, worktree isolation (SUBAGENT-15), supervision (`SupervisedTasks`) and the
runtime-authored, worker-text-free notice (SUBAGENT-11) all work. This PRD adds
no worker machinery; it changes *when the parent is held* and *how the parent is
told*.

## 3. How Claude Code does it

Evidence tiers: **B** = vendor documentation, fetched 2026-11-23; **C** =
first-hand tool contracts of the Claude Code build that authored this PRD;
**D** = public issue tracker. Appendix A lists sources.

| Dimension | Claude Code | Tier |
|---|---|---|
| Default mode | "With nothing configured, a subagent other than `web-fetch` that Claude spawns from your main conversation starts in the background." Per the docs, fork mode (which is what stops Claude waiting) is on by default in an interactive terminal from v2.1.232 and everywhere from v2.1.302; a third-party guide dates background-by-default to v2.1.198. | B |
| Can the model block? | Not in interactive sessions. With fork mode on (the default) "Claude can't wait for a subagent". An operator turns waiting on with `CLAUDE_CODE_FORK_SUBAGENT=0`, or turns all backgrounding off with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (which also makes Ctrl+B inert). `claude -p` and the SDK let Claude wait. | B |
| Foreground semantics | "Foreground subagents block the main conversation until complete." The person can press **Ctrl+B** (or click *Run in background* in VS Code) to detach one. | B |
| How the result returns | "A background subagent's results reach Claude as a completion notification in a later turn." It is "marked as an automated event rather than a message from you". Claude "waits for that notification before reporting the subagent's results, and if you ask about progress first, it reports that the subagent is still running." | B |
| Idle re-invocation | The Bash tool's `run_in_background` "keeps running across turns and re-invokes you when it exits… do not poll". The Agent tool says subagents "run in the background; you'll be notified when one completes", and forbids fabricating a pending result. A tool for self-pacing says: "when harness-tracked work finishes, you are re-invoked automatically". | C |
| Visibility | Panel below the prompt; `/tasks`; a finished successful row disappears, a failed or stopped row stays 30 s. | B |
| Needing the person | A background subagent's permission prompt "surfaces in your main session and names the subagent that is asking"; Esc denies that one call without stopping the subagent. A session-wide grant applies to the main conversation too. | B |
| Background tool set | Smaller than foreground: interactive and control tools (`AskUserQuestion`, `ExitPlanMode`, `ScheduleWakeup`, `Workflow`, …) are removed. | B |
| Failure | A failed background subagent's message "names the API error and includes the subagent's last output, so partial work isn't lost". | B |
| Steering | `SendMessage` resumes a finished subagent in the background; `TaskStop` stops one. | B, C |
| Concurrency | 20 concurrent; the refusal tells the model not to retry. No cap on total spawned. | B |
| **Failure mode** | Issue #108836 (closed as a duplicate of #77578, 2026-11-20): an idle background subagent was **never woken** when its own background command finished, and sat idle for ~11 hours until the main session messaged it. "Nothing in the main session distinguishes a stuck subagent from a busy one." | D |

### What to emulate

1. **Non-blocking is the default and the model does not get to choose
   otherwise.** The blocking path is an operator setting.
2. **Completion is an event that re-invokes the main agent.** The person never
   has to type to collect a result.
3. **An honesty contract.** The model must not poll, sleep, redo the workers'
   work, or state a result it has not received; when asked, it says the work is
   still running.
4. **The person sees it and stays in control.** Visible running state, an
   automated-event rendering, prompts that name the worker, and a way to stop.

### What not to copy

1. **A fire-once, in-memory wake.** Claude Code's lost wake is silent and
   unbounded (§3, last row). agenthicc already has a durable terminal record per
   pool; the wake must be a *hint* over that record, with a reconciler (§6.5).
2. **Process-wide environment switches.** The operator setting is a typed
   `subagents.spawn_mode`, validated and shown by `config show`.
3. **Silent removal of tools from background workers.** agenthicc workers
   already never inherit `control` tools (`SUBAGENT-12`); that rule is named,
   tested, and left unchanged.

## 4. Goals

1. **A spawn does not hold the turn.** In a turn that can receive a completion,
   `spawn_subagents` returns a receipt as soon as the pool has journaled its
   start; the model continues the same turn or ends it; the composer stays
   usable.
2. **Completion reaches the model without a user message** — by waking an idle
   session, or at the next tool boundary of a running turn — exactly once.
3. **The wake cannot be silently lost.** A dropped callback, a crash between the
   terminal record and delivery, or a missed idle transition is recovered, and
   the recovery is observable.
4. **The model's contract is honest and in one place.** Receipt text, tool
   description and a short stable system-prompt block say: the call is
   non-blocking; do not poll; end the turn if nothing else is useful; do not
   state results before they arrive.
5. **The person can tell running from stuck, and can stop it.** Running pools
   stay visible while the session is idle; the automated notice is drawn as an
   automated event; Esc during a wake turn pauses further automatic wakes.
6. **Blocking survives where it is the contract.** Workflow phases, and any
   runner that cannot receive a completion, resolve to foreground; an operator
   can choose foreground everywhere; Ctrl+B keeps working on a foreground pool.
7. **Every remaining wait yields to the person.** A bounded wait ends early when
   input is queued.
8. **No new limit, no second mechanism.** One scheduler (`advance()`) decides
   what runs next; one journal fold says what is undelivered; one supervised
   owner holds the pool task.

## 5. Non-goals

- Changing pool, worker, retry, isolation, policy or journal semantics
  (SUBAGENT-01…15), or the worker-text-free notice (SUBAGENT-11 goal 5).
- Background *sessions* (`/background`, PRD-141) and detached orchestration
  (PRD-204). They are a different feature and are not conflated here.
- Wake support for headless, service-driven and background-session runners. They
  resolve to foreground (§6.8); a JSON-lines `subagent_completed` event and a
  synthetic turn for them is a follow-up (§12, Q3).
- Letting a phased workflow's phase spawn in the background (§12, Q2).
- Any new limit, or any change to the existing ones. PR #30 removed the turn,
  budget and pool-time ceilings and deliberately kept `max_workers`,
  `max_concurrent`, `max_tasks`, `max_active_pools`, `max_depth` and the
  per-call `timeout_s`; this PRD adds no cap on pools, workers, wakes or waits
  and loosens none of those.
- Auto-approving anything. A background worker's approval request still goes to
  the person.
- A new event schema. Wakes ride on the existing `turn_queued` event and the
  existing pool lifecycle journal records.

## 6. Design

### 6.1 Overview

```text
 model turn ──spawn_subagents──▶ resolve_spawn_mode()
      │                              │ background (default)      │ foreground
      │                              ▼                           ▼
      │                 pool starts under SupervisedTasks   today's blocking call
      │                 receipt {status:"running"} ◀── returns at once
      │ keeps working / ends turn
      ▼
 pool terminal ─▶ journal terminal record (truth) ─▶ PoolContinuationRegistry.on_terminal
                                                        │ (hint only)
                                                        ▼
                                            TUISession.advance()   ◀── reconciler (idle, and every 30 s while any pool live
                                            single scheduler            or any notice undelivered)
                              ┌─────────────────┼──────────────────────┐
                        session idle       turn running             turn starting
                              │                 │                      │
                  wake turn (notice is   tool-boundary injection   existing turn-start
                  its only user input)   via _QueuedInputRunner    delivery (fallback)
                              └──── model calls collect_subagent_results(pool_id) ────┘
```

### 6.2 Spawn-mode resolution

One pure function decides, at tool construction **and** again at call time:

```python
SpawnMode = Literal["background", "foreground"]

def resolve_spawn_mode(
    settings: SubagentSettings,
    *,
    owner: SupervisedTasks | None,
    journal: ConversationJournal | None,
    notices: NoticeSource | None,
    in_workflow_phase: bool,
) -> SpawnMode: ...
```

Background only if **all** hold; otherwise foreground:

| Condition | Why |
|---|---|
| `settings.spawn_mode == "background"` | operator choice (default) |
| `owner is not None` | the pool must be supervised, never a bare task (SUBAGENT-11 goal 2) |
| `journal is not None` | the pool must be recoverable and its completion durable |
| `notices is not None` | a delivery path exists; this is what stops P5 recurring in a runner that cannot wake |
| `not in_workflow_phase` | a phase's transition contract needs the results (§12, Q2) |

The function replaces both the `background` parameter and the
`background_unavailable` refusals at `tool.py:575-594` and `:862-884`: a turn
that cannot background simply gets a foreground spawn, and the model is never
offered a mode that will be refused.

The tool **description** is composed per turn from the same function (the tool
already composes dynamic text, `tool.py:290`): a background turn is told the call
returns at once; a foreground turn is told it blocks. Call-time resolution wins
if the state changed mid-turn (for example, entering a phase), and the receipt
states the mode that was used.

### 6.3 The model contract

**Receipt** (background):

```text
Subagent pool {pool_id} is running in the background ({n} workers). Your turn
is not blocked: continue with anything that does not need their results, or end
your turn. You will be resumed automatically when the pool finishes. Do not
poll, sleep, or redo their work, and do not state or guess what they will find
until you have collected the results.
```

`status` stays `"running"` for a runtime-chosen background pool and
`"backgrounded"` for one the person detached with Ctrl+B (they mean different
things and both stay).

**System-prompt block** — stable, in the cached prefix, not per turn:

```text
## Subagents
spawn_subagents does not block you. After spawning, keep working on other tasks
or end your turn; the runtime resumes you with a notice when the pool finishes.
Then call collect_subagent_results(pool_id). Never poll, sleep, or repeat a
worker's task. Never describe or guess a pending worker's results; if asked,
say it is still running.
```

It is added only when the tool is visible and the resolved mode is background;
foreground turns keep today's wording. A test pins the text so wording changes
are deliberate.

### 6.4 Delivering the completion: three paths, one source of truth

**Source of truth.** `ConversationJournal.completed_background_subagent_pools()`
(undelivered terminal background pools, folded from the journal) plus pending
worker questions from the continuation registry. Delivery marks a notice
delivered with the existing `subagent_pool_notification_delivered` record and
the stable message id (`subagent-completion:<pool_id>`,
`subagent-question:<question_id>`), via `add_user_once`. The wake is never the
truth.

**Path 1 — idle wake.** `PoolContinuationRegistry` gains
`on_terminal(callback)`, invoked from the pool task's done-callback **after** the
terminal journal record (`pool.py:3262`, `:3514`) is written. The session's
callback does one thing: `call_soon(self.advance)`. It never starts a turn.

`advance()` (`tui_session.py:5447`) is already the single scheduler: it drains
workflow returns (`workflow_stack.take_return()`), then queued user messages. It
gains one branch, **after** both: if no turn was started, undelivered notices
exist, and wakes are not paused (§6.5), start a wake turn with the same
primitive the workflow return uses (`_schedule_workflow_turn`, `:2289`), with
`client_id="subagent-completion"` and an idempotency key derived from the sorted
notice message ids. The wake branch shares `advance()`'s guards for tool-history
recovery and a pending handoff. It must check the other two explicitly rather
than assume them: no turn is running, and no workflow frame is paused (today
`run_turn` refuses the latter with "Resume or abandon the paused workflow
first"). FR-4 lists all four guards, and A21 tests each.

Everything that finishes in the same loop iteration is folded into one wake: the
turn starts via `call_soon`, and `deliver_background_pool_notices` already folds
all undelivered pools. No debounce timer is needed.

A user message that is already queued takes priority: `advance()` starts that
turn, and the turn-start delivery puts the notices ahead of it in the same turn
(P5's existing path, unchanged).

**Path 2 — running turn.** `NoticeSource` is a protocol bound beside the queued
message source (`bind_queued_message_source`,
`runners/agent_turn_context.py`):

```python
class NoticeSource(Protocol):
    def claim(self) -> tuple[PendingNotice, ...]: ...   # undelivered, oldest first
    def has_pending(self) -> bool: ...

@dataclass(frozen=True, slots=True)
class PendingNotice:
    message_id: str                      # the stable id above
    kind: Literal["completion", "question"]
    pool_id: str
    text: str                            # runtime-authored; never worker text
```

`_QueuedInputRunner._execute_tools` (`agent_turn.py:1775`) already commits the
tool results and then appends a queued user message, which keeps the provider
sequence `assistant(tool_use) → tool_result → user` valid. It claims
notices through the same seam, **before** a queued user message, commits the
tool exchange once, and appends each notice with `add_user_once`. The existing
guard (`ctx.turn >= max_turns - 1`) applies unchanged; a notice that cannot be
injected stays undelivered and is picked up by Path 1 or 3.

**Path 3 — fallback.** Turn-start delivery
(`_deliver_completed_background_subagents`, `:5516`) is unchanged. It is what
delivers after a resume and what the non-TUI runners use.

**Wake-turn input.** The only user-role content of a wake turn is the
runtime-authored notice(s). No text is synthesised for the person. Exactly one
user-role message per notice reaches the provider (A4). The implementation
chooses between passing the notice text as the turn text with `add_user_once`
dedupe, or a "continue from memory" entry; either must satisfy A4, and the
current `run_turn(text)` contract must not double-append.

### 6.5 The wake must not be lost, duplicated or runaway

The Claude Code lost-wake bug is the design brief for this section.

1. **Idempotent.** The notice message id is the dedupe key at three layers: the
   journal delivered-record, `add_user_once`, and the wake turn's
   `idempotency_key`. A crash between "terminal recorded" and "delivered"
   re-delivers once on resume, never twice (A6).
2. **Reconciled.** A session-owned task (a `SupervisedTasks` task, closed with
   the session) runs `reconcile()` — "undelivered notices exist and the session
   is idle ⇒ `advance()`" — on three triggers: every transition to idle, session
   start, and every 30 s **while** any background pool is live or any notice is
   undelivered. The interval is an internal liveness constant, not an
   operator-facing limit. A reconcile that finds work the callback missed logs
   `subagent_wake_recovered` with the pool id and age, increments a counter
   shown in `/perf`, and then wakes (A7).
3. **No wake on resume.** Pools recovered as `interrupted` or `cancelled` at
   session start are delivered at the first user turn, as today, and the person
   sees a transient notice ("2 background pools ended while you were away —
   Ctrl+O"). Spending tokens on a turn the person did not start, on opening a
   session, would be a surprise.
4. **The person can stop it.** Cancelling a wake turn (Esc / Ctrl+C) sets
   `wake_paused`; the next user submission clears it. While paused, notices stay
   undelivered and are delivered at the next user turn. This bounds autonomous
   follow-on without any numeric cap: a model that keeps spawning and being
   woken is still visible, interruptible, and stops when the person says so.
5. **The draft survives.** Starting a wake turn switches the composer to
   streaming input (`_activate_streaming_input`); text the person was typing is
   kept and is not submitted (A8). The workflow-return wake already does this
   and shares the path.

### 6.6 Visibility

- **Indicator.** While any background pool is live — and while any notice is
  undelivered — the status line shows `◆ N running` / `◆ N finished, resuming…`,
  counting exactly what it names (the TUIR-05 lesson). Each live pool shows the
  age of its last worker event, so a stuck pool is distinguishable from a busy
  one; this is display only, nothing is stopped automatically.
- **Automated-event rendering.** `_render_user_message` (`appender.py:1067`)
  honours `source ∈ {"subagent_completion", "subagent_question"}` and draws a
  muted automated-event block, not a `❯` user band:
  `↩ Subagents finished — pool a1b2c3d4 · 3/3 succeeded · resuming`. The same
  applies in the verbose transcript.
- **Usage attribution.** A wake turn's provider calls are tagged so `/usage` can
  show what automatic wakes cost. The ledger's `UsageCategory`
  (`agent`, `subagent`, `compaction`; `runners/usage_ledger.py:91`) says *whose*
  call it was, and `UsageSource` only says how a record was populated, so the
  tag is a new `UsageCategory` member (`wake`) or an equivalent ledger field;
  the implementation picks the smaller change and A20 pins the result.
- **Ctrl+B** is offered only while a **foreground** pool is active: a new
  composer condition `subagents_foreground` replaces `subagents_running` for the
  binding (`tui/actions.py:355-363`, `tui/input/unified_session.py:352`). A
  background pool has nothing to detach.

### 6.7 Workers that need the person

Unchanged machinery, made verifiable under the new default:

- **Approvals.** A background worker's approval request goes to the session's
  approval broker, attributed to the worker (`_RequesterApprovals`,
  `pool.py:1543-1546`), and the overlay opens whether the main session is idle
  or mid-turn (A10). Denying one call does not stop the worker. An unattended
  request is not auto-approved; it raises the attention badge (AGX-10).
- **Questions.** `subagents.on_clarification` is unchanged (`queue` in the TUI).
  A queued question becomes a `PendingNotice(kind="question")`, so it wakes an
  idle session the same way a completion does instead of waiting for the next
  keystroke.
- **Tool set.** Workers never inherit `control`-capability tools or
  `spawn_subagents` (SUBAGENT-12); this is the agenthicc counterpart of Claude
  Code's smaller background tool set and is covered by an existing test that this
  PRD does not weaken.

### 6.8 Other runners and workflow phases

| Context | Resolves to | Reason |
|---|---|---|
| Direct TUI turn (main session) | background | has owner, journal and a notice source |
| Workflow phase turn | foreground | the phase transition needs the results (Q2) |
| Headless stdin runner, background session, session-service turn | foreground | no owner / notice source bound (P7) |

`bind_background_task_owner` (`agent_turn_context.py:40`) is unchanged;
`TUISession` additionally binds a `NoticeSource` for direct turns only and binds
`in_workflow_phase` for workflow turns. A runner gains background by binding all
three, which is the follow-up for headless (Q3).

### 6.9 Remaining waits yield to the person

- `collect_subagent_results(wait=True, timeout_s=…)` polls `NoticeSource` and the
  queued-input source; when either has pending input it returns the running
  snapshot early with `interrupted: "user_input"` (or `"notice"`), so a typed
  message is claimed at the very next boundary rather than after up to 60 s. The
  tool text says that blocking is discouraged and why.
- Foreground pools (phases, operator setting) keep today's behaviour: Ctrl+B
  detaches; Esc cancels. Interrupting a blocking wait by typing is **not**
  adopted for foreground pools, because an operator who chose foreground, or a
  phase that needs results, has asked to wait.

### 6.10 Configuration

`subagents.spawn_mode = "background" | "foreground"`, default `"background"`,
validated in `SubagentSettings.__post_init__`, shown by `agenthicc config show`,
documented in `docs/guides/configuration.md`. It **replaces**
`subagents.background_enabled`. The old key is rejected with an error naming the
new one (as WFC-07 did for the renamed workflow); there is no alias and no
dual read. `foreground` restores today's blocking spawn everywhere, and keeps
Ctrl+B working on it.

### 6.11 Symbols removed or replaced

| Removed | Replaced by |
|---|---|
| `spawn_subagents(background=…)` parameter, its validation (`"background must be true or false"`) and docstring | `resolve_spawn_mode` |
| `SubagentSettings.background_enabled`, its parsing, validation and `config show` field | `subagents.spawn_mode` |
| The `background_unavailable` refusals at spawn time (`tool.py:575-594`, `:862-884`) | resolution to foreground |
| `subagents_running` as the Ctrl+B condition | `subagents_foreground` |
| The guide's "not inserted into an already-running provider loop" | Path 2 |

Prove with `grep -rn "background_enabled\|background: bool\|background_unavailable" src/ tests/ docs/ skills/ llms-full.txt`
returning nothing outside this PRD, the SUBAGENT-11 history and `CHANGELOG.md`.

## 7. Requirements

- **FR-1** In a background-resolved turn, `spawn_subagents` returns after the
  pool's journaled start, without waiting for any worker, with the §6.3 receipt.
- **FR-2** `resolve_spawn_mode` is the only place the mode is decided; its five
  inputs are the ones in §6.2.
- **FR-3** A terminal background pool, and a pending worker question, produce
  exactly one user-role notice each, whichever delivery path wins.
- **FR-4** The terminal callback never starts a turn; `advance()` is the only
  scheduler. A wake never starts while a turn runs, a workflow frame is paused,
  tool history needs recovery, or a handoff is pending.
- **FR-5** At a tool boundary, notices are injected after the tool results are
  committed and before any queued user message; the provider sequence stays valid.
- **FR-6** `reconcile()` guarantees that an undelivered notice with an idle
  session is acted on within one reconcile interval, and logs and counts every
  recovery.
- **FR-7** Esc during a wake turn pauses automatic wakes until the next user
  submission; the paused state is visible.
- **FR-8** The notice and the wake render as automated events; no `user_message`
  with an automated `source` renders as a user band.
- **FR-9** `collect_subagent_results(wait=True)` returns early on queued input.
- **FR-10** No new numeric limit on pools, workers, wakes or waits is added to
  `subagents/`, `runners/` or `config.py`, and no existing one changes (the 30 s
  reconcile interval is an internal liveness constant, not a setting).
- **FR-11** The §6.3 system-prompt block is present exactly when the tool is
  visible and the resolved mode is background, and is byte-stable across turns.
- **FR-12** New or changed signatures use concrete types; no `Any` in new code;
  `llms-full.txt` gains entries for `NoticeSource`, `PendingNotice`,
  `SpawnMode`, `resolve_spawn_mode` and `SubagentSettings.spawn_mode`.

## 8. Acceptance criteria

Times use the scripted transport's injectable clock; "loop stall" uses the
TUIR-04 sampler.

| # | Criterion | Test level |
|---|---|---|
| A1 | **Baseline (written first, fails).** With three scripted workers of 5 s each and the old foreground default, the next provider request after `spawn_subagents` starts ≥ 5 s later; a sibling `read_file` result in the same batch is withheld for the same time. After the change the same script sees the next request within 250 ms. | integration |
| A2 | In one turn the model spawns, then completes two unrelated tool calls and writes a reply, all before any worker finishes; the receipt is the spawn call's result. | integration + e2e cassette |
| A3 | A message typed while a pool runs in the background is claimed at the next tool boundary, not after the pool. | integration |
| A4 | **Idle wake.** The turn ends; the pool then completes; a turn starts with no input from the person, within 100 ms of the terminal record; the provider sees exactly one user-role notice with the pool id and counts and **no worker text**; the model calls `collect_subagent_results` and its answer is rendered. Repeated 200 times: no duplicate, no miss. | integration + e2e |
| A5 | **Busy delivery.** A pool completes mid-turn: the notice is in the request that follows the next tool boundary (assert on the request log), not at turn end. With `ctx.turn >= max_turns - 1` it is deferred and still delivered once. | integration |
| A6 | **Crash safety.** Kill the process between the terminal record and delivery, then resume: exactly one notice is delivered, at the first user turn, with no wake. | integration |
| A7 | **Lost wake.** Drop the `on_terminal` callback in a test; the reconciler delivers within one reconcile interval (fake clock), logs `subagent_wake_recovered`, and `/perf` shows the count. | unit + integration |
| A8 | A half-typed draft is intact and unsubmitted after a wake turn starts and ends. | unit (TUI) |
| A9 | Esc during a wake turn pauses wakes: a second pool finishing afterwards does not start a turn; the next user message delivers its notice; the paused state is visible. | integration |
| A10 | A background worker's approval request opens the overlay, naming the worker, with the main session idle and with it mid-turn; denying it leaves the worker running; nothing is auto-approved. | integration |
| A11 | A queued worker question wakes an idle session via a `question` notice; `answer_subagent` resumes the worker. | integration |
| A12 | Mode resolution table: a workflow phase turn, the headless runner and a service turn each resolve to foreground and **never** return `background_unavailable`; `subagents.spawn_mode = "foreground"` blocks as before and Ctrl+B still detaches. | unit |
| A13 | Ctrl+B is offered only while a foreground pool is active. | unit (TUI) |
| A14 | The completion notice renders as an automated-event block in the live view and in the verbose transcript, never as a `❯` band. | unit (TUI) |
| A15 | `collect_subagent_results(wait=True, timeout_s=60)` returns within one event-loop iteration of a queued user message, with `interrupted="user_input"`. | unit |
| A16 | With eight background workers plus wakes, the worst event-loop stall is < 250 ms and p95 < 25 ms (the TUIR-04 budget). | performance |
| A17 | Configuration: `spawn_mode` validates; `background_enabled` is rejected with a message naming `spawn_mode`; `config show` prints it. | unit |
| A18 | The §6.3 prompt block and receipt text match their pinned strings; the block is absent in a foreground turn. | unit |
| A19 | `grep` of §6.11 returns nothing; `rg 'Any' ` over new modules finds nothing; no numeric cap was added to `subagents/`, `runners/` or `config.py` (FR-10), checked by grep in the PR and by the existing tests that refuse the removed limit keys. | CI |
| A20 | A wake turn's usage records carry the wake attribution and `/usage` reports their total separately from ordinary agent turns. | unit |
| A21 | **Wake guards.** With a turn running, a workflow frame paused, tool history awaiting recovery, or a handoff pending, a finished pool starts no turn; its notice stays undelivered and is delivered once, by the reconciler or the next turn, when the guard clears. | integration |

## 9. Test plan

- **Baseline first (A1).** The first change is the failing test, using the
  scripted transport and the TUIR-04 lag sampler from
  `tests/performance/test_subagent_loop_lag.py`. This also produces the measured
  numbers §2 leaves as reasoning from code.
- **Unit:** mode resolution truth table, `NoticeSource`/`PendingNotice`,
  reconcile arithmetic with a fake clock, receipt and prompt strings, config,
  renderer, composer conditions, interruptible collect
  (`tests/unit/test_spawn_mode.py`, `test_subagent_notices.py`,
  `test_subagent_wake_reconcile.py`).
- **Integration:** real `EventProcessor` and journal, scripted workers: A2–A6,
  A9–A11 (`tests/integration/test_subagent_background_default.py`,
  `test_subagent_wake.py`). Crash injection at each of the three delivery seams.
- **E2E cassette:** a full TUI session — spawn, keep working, end turn, wake,
  collect, answer (`tests/e2e/test_subagent_wake.py`), with request-log
  assertions that the model sees one notice and no worker text.
- **Performance:** A16 in `tests/performance/`.
- Existing SUBAGENT-11 tests that assert the old default or the
  `background_unavailable` refusal are **rewritten**, not copied
  (`tests/integration/test_subagent_tool_execution.py`,
  `tests/unit/test_subagent_resume.py`, `tests/unit/test_tui_session_coverage.py`).
- Host load flakes timing tests: re-run a failure in isolation and say so before
  calling it a regression.

### Implementation evidence

The original foreground-default run delayed the next provider request and its
sibling file-read result by 5.190 seconds. All three five-second workers had
finished by that request. The regression test retains the three-worker script
and measures when the provider sees the sibling result on the injectable
script clock. The local after-change sample reached that boundary in 108 ms
of wall time with all workers still active. The performance scenario uses the TUIR-04 sampler on a warm
session with eight concurrent scripted file reads, real journal writes, and an
automatic wake. It uses tmpfs where available to control storage latency. The
crash tests use filesystem-backed process restarts. The local performance run
measured 97.9 ms maximum stall and 15.6 ms p95.

| Criterion | Test evidence |
|---|---|
| A1 | [Background default integration](../../tests/integration/test_subagent_background_default.py) measures the provider boundary and verifies the sibling result. |
| A2 | [Background default integration](../../tests/integration/test_subagent_background_default.py) and the [recorded TUI scenario](../../tests/e2e/test_subagent_wake.py) assert two unrelated reads and a parent reply while workers are live. |
| A3 | [Wake integration](../../tests/integration/test_subagent_wake.py), `pending`, asserts queued input in the next provider request with the worker still gated. |
| A4 | [Wake integration](../../tests/integration/test_subagent_wake.py) checks 200 scheduled deliveries against a simulated clock and receipt set. The [recorded TUI scenario](../../tests/e2e/test_subagent_wake.py) asserts the user-role notice, private tool result, and rendered answer. |
| A5 | [Wake integration](../../tests/integration/test_subagent_wake.py), `at-boundary` and `deferred`, checks provider message order and final-boundary deferral. |
| A6 | [Wake integration](../../tests/integration/test_subagent_wake.py) kills a process after the terminal record, message append, and acknowledgement. Every reopen waits for user input and delivers one notice. |
| A7 | [Reconciler unit tests](../../tests/unit/test_subagent_wake_reconcile.py) check scheduler idempotency. [Wake integration](../../tests/integration/test_subagent_wake.py) drops the callback, fires the 30-second timer, and checks the log and `/perf`. |
| A8 | [Reconciler unit tests](../../tests/unit/test_subagent_wake_reconcile.py) and the [recorded TUI scenario](../../tests/e2e/test_subagent_wake.py) assert the draft before, during, and after a wake. |
| A9 | [Wake integration](../../tests/integration/test_subagent_wake.py) interrupts a wake with Esc, observes the paused indicator, and delivers the second notice at the next user turn. |
| A10 | [Wake integration](../../tests/integration/test_subagent_wake.py) runs the named approval overlay and scoped denial with the parent idle and busy. |
| A11 | [Wake integration](../../tests/integration/test_subagent_wake.py) collects the question through a tool and observes the answer in the worker's next continuation request. |
| A12 | [Spawn mode unit tests](../../tests/unit/test_spawn_mode.py) exercise all five dependencies, phase/headless/service calls, and the operator setting. [Subagent tool integration](../../tests/integration/test_subagent_tool_execution.py) verifies Ctrl+B detachment under the foreground setting. |
| A13 | [Notice and composer unit tests](../../tests/unit/test_subagent_notices.py) and [keymap coverage](../../tests/unit/test_keymap_screens.py) check the foreground-only binding. |
| A14 | [Notice and composer unit tests](../../tests/unit/test_subagent_notices.py) cover both notice kinds in live and verbose rendering, composer history, and session title. |
| A15 | [Notice and composer unit tests](../../tests/unit/test_subagent_notices.py) interrupt a 60-second collect within one loop iteration of a queued message or notice while the pool task remains live. |
| A16 | [TUIR-04 performance gate](../../tests/performance/test_subagent_loop_lag.py) enforces the maximum and p95 stall budgets. |
| A17 | [Spawn mode unit tests](../../tests/unit/test_spawn_mode.py) exercise TOML validation, the migration error, and `config show`. |
| A18 | [Spawn mode unit tests](../../tests/unit/test_spawn_mode.py) pin the exact prompt and receipt. The [recorded TUI scenario](../../tests/e2e/test_subagent_wake.py) checks when the runtime includes the prompt. |
| A19 | [No-limit contracts](../../tests/unit/test_subagent_no_limits.py), [removed config keys](../../tests/unit/test_config.py), the CI type-safety ratchet, and the source review check the tool schema, concrete annotations, and unchanged admission bounds. The retired key appears in migration validation and its regression test. |
| A20 | [Wake usage unit test](../../tests/unit/test_subagent_wake_reconcile.py) reopens the ledger and checks separate totals. The [recorded TUI scenario](../../tests/e2e/test_subagent_wake.py) verifies the actual turn's provider records. |
| A21 | [Wake integration](../../tests/integration/test_subagent_wake.py) parameterizes all four guards, checks stored undelivered notices, and verifies one wake after each guard clears. |

[Journal projection tests](../../tests/unit/test_journal_projection.py) also
check the sequence assigned to concurrent off-loop appends, the terminal
state when a detach receipt overlaps completion, and durable append
completion on task cancellation.

## 10. Rollout, migration and security

**Rollout.** No flag and no dual path; the project is not in production. The
operator's escape is `subagents.spawn_mode = "foreground"`. Slices (§11) each
leave the tree green and shippable.

**Migration.** `subagents.background_enabled` is rejected with a message naming
`spawn_mode`. In-flight journals need no migration: the terminal and delivered
records are unchanged. A pre-existing undelivered pool is delivered by Path 3 at
the first turn.

**Security and trust.**

- The notice stays runtime-authored with no worker text; worker output reaches
  the model only as the *tool* result of `collect_subagent_results`. A wake turn
  therefore adds no new injection channel: what the model receives automatically
  is a pool id, a status and two integers.
- A wake turn runs under the active mode and capability ceiling like any turn;
  tool approvals still prompt. An unattended wake that needs approval waits and
  raises the attention badge. It is never auto-approved. In Yolo mode,
  unattended autonomy is the person's existing choice.
- Autonomous follow-on is bounded by *visibility and the person*, not a cap:
  the automated-event block, the usage attribution, the status indicator, and
  `wake_paused`.
- No new filesystem, network or process capability is introduced.

**Cost.** Each wake is one extra model request that the person did not type.
It is attributed (§6.6) and visible. A pool that finishes while the person is
away costs one turn, not a polling loop.

## 11. Implementation slices

Each slice is independently shippable and leaves `main` green.

1. **S1 — Measure and render.** A1 baseline test and the loop-lag harness;
   automated-event rendering of notices (P6, FR-8) — useful alone.
2. **S2 — Deliver without a user.** `NoticeSource`, `PendingNotice`, Path 2
   (tool-boundary injection), `on_terminal`, the `advance()` wake branch, the
   reconciler, `wake_paused`, usage attribution. Works with today's opt-in
   `background=True`, so it can be exercised before the default changes.
3. **S3 — Make it the default.** `resolve_spawn_mode`, remove `background`
   parameter and `background_enabled`, add `spawn_mode`, the receipt and prompt
   block, `subagents_foreground`, interruptible `collect`, mode table for
   phases and other runners.
4. **S4 — Docs and close-out.** `docs/guides/subagents.md` and
   `parallel-workers.md`, `docs/guides/configuration.md`, `skills/subagents/`,
   `llms-full.txt`, `CHANGELOG.md` (**breaking**: default and setting renamed),
   the SUBAGENT-11 "superseded in part" banner, and the §6.11 searches.

Order matters: S2 before S3, so the default never flips onto a path that cannot
wake.

## 12. Risks and open questions

| Risk | Mitigation |
|---|---|
| A model that cannot block produces worse plans when it needs results to proceed. | It ends the turn and is resumed with the notice, which is Claude Code's own model; the operator setting restores blocking. Watch evals; Q1. |
| Runaway autonomous loop (spawn → wake → spawn). | Visible, attributed, interruptible; Esc pauses; no new cap (the owner removed the turn and budget ceilings in PR #30). |
| A wake turn surprises the person. | Automated-event block and status line say why; no wake on resume; draft preserved. |
| Mid-turn injection breaks the provider message sequence. | It reuses the proven queued-message seam, commits the exchange first, and A5 asserts the sequence. |
| `advance()` is already busy (workflow returns, queued input, commands); a new branch there risks starvation or reordering. | The wake branch runs last and only when nothing else started; tests cover each ordering (FR-4). |
| Reconciler cost. | An in-memory journal fold and one timer, only while a pool is live or a notice is undelivered. |

**Open questions** (recommendation first):

- **Q1.** Should the model ever be able to request a single blocking spawn?
  *Recommend no*, matching Claude Code, where the model cannot wait in
  interactive sessions. Revisit only if evals show a real regression; the
  operator setting is the escape.
- **Q2.** Phased workflows. *Recommend* foreground now. A follow-up may let a
  phase declare background spawns with a collect-before-transition guard, so a
  phase cannot end with results still pending.
- **Q3.** Headless and service runners. *Recommend* a JSON-lines
  `subagent_completed` event plus a synthetic turn once they can bind an owner
  and a `NoticeSource`; until then they run foreground.
- **Q4.** Wake while the person is mid-draft. *Recommend* wake immediately and
  preserve the draft (§6.5, item 5); a hold-while-typing rule can follow if people
  find it jarring.
- **Q5.** Should the wake coalesce a burst of unrelated completions into one
  notice message? *Recommend* no: one message per pool keeps the stable ids and
  idempotency simple, and they are folded into one *turn*.

## 13. Related and superseded PRDs

- **SUBAGENT-11** — superseded in part: goal 1's `background` parameter, goal 5's
  "next logical-turn boundary" delivery, and goal 10's default. Its collection
  (goals 3–4), supervision (goal 2), question policy (goal 6), control tools
  (goals 7–8) and the worker-text-free notice stand and are built on.
- **SUBAGENT-07 / 08** — background orchestration and lifecycle controls; reused
  unchanged.
- **SUBAGENT-12 / 15** — workers behave like the main agent; worktree isolation.
  A background pool with several mutating workers still gets one worktree each.
- **WFC-01 / WFC-03** — the stack return that wakes a parent through `advance()`
  is the precedent this PRD follows; the same idempotency-key pattern is used.
- **AGX-08 / AGX-10, TUIR-04 / TUIR-05** — the panel and attention routing this
  surfaces through; the loop-lag budget and the "badge counts what it names"
  rule this obeys.

## Appendix A — Evidence and sources

**Code (commit `bebe522`).** `src/agenthicc/subagents/tool.py`
(`:302`, `:321-324`, `:573-594`, `:832-838`, `:862-884`, `:920-925`);
`subagents/delivery.py`; `subagents/types.py:44`; `subagents/pool.py:1543-1546`,
`:3262`, `:3514`; `runners/agent_turn.py:1775-1814`, `:3067`;
`runners/agent_turn_context.py:40-100`; `runners/tui_session.py:2289`, `:4638`,
`:5447`, `:5516`, `:5921-5925`; `tui/workspace/appender.py:1067-1088`;
`tui/actions.py:355-363`; `tui/input/unified_session.py:352`;
`memory/journal.py:1435` (`completed_background_subagent_pools`);
`config.py` (`SubagentSettings`); lauren-ai 1.6.1
`_agents/_runner.py::_execute_tools`; `docs/guides/subagents.md` § Background
pools; `prds/SUBAGENT-11-model-facing-pool-control.md` §4;
`prds/SUBAGENT-research-harness-comparison.md` G-7.

**Claude Code documentation (tier B, fetched 2026-11-23).**
<https://code.claude.com/docs/en/sub-agents> — "Run subagents in foreground or
background", "Let Claude wait for a subagent", "Tools, permissions, and results
of background subagents", "Viewing, stopping, and resuming", "Concurrent
subagent limit", "Fork the current conversation".

**Claude Code tool contracts (tier C).** The Agent, Bash (`run_in_background`),
SendMessage, TaskStop and ScheduleWakeup tool descriptions of the Claude Code
build used to author this PRD, read 2026-11-23.

**Issue tracker (tier D).** <https://github.com/anthropics/claude-code/issues/108836>
(closed 2026-11-20 as a duplicate of #77578): an idle background subagent not
re-woken when its own background command finished; ~11 hours idle; recovered by
a `SendMessage` from the main session.

**What this PRD did not do.** It did not run Claude Code's source (closed) and
it did not measure agenthicc's blocking time; the §2 durations follow from the
code and are replaced by measured numbers in A1.
