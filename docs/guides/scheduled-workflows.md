# Repository-scoped scheduled workflows

Repository schedules are durable automation definitions that create a fresh
background session for each accepted occurrence. Unlike `/loop`, a schedule
does not belong to a foreground conversation and can continue while that
conversation is closed. Schedules are user-local; each record is scoped to a
canonical repository path.

## Start the schedule service

Schedules are only dispatched while the local Agenthicc session service is
running. Start it in a long-lived terminal or under your process supervisor:

```bash
agenthicc session serve
```

The service reports `schedule_service: "running"` when it owns the scheduler
lease. Only one process can own a user's schedule service at a time. If the
service is stopped, definitions remain durable and enabled schedules show as
`awaiting service`; no model work is launched until a service starts again.
Inspect the state with:

```bash
agenthicc schedules service-status
```

The service also hosts Agenthicc's local session API. It binds to loopback by
default; follow the [session service guide](session-service.md) before exposing
it on another interface.

## Create and enable a schedule

The workflow and provider profile must be registered/configured for the
repository. The prompt is saved byte-for-byte as supplied, so quote it when it
contains spaces or shell-sensitive characters.

```bash
agenthicc schedules create code_plan local Plan 'interval:6h' Europe/Berlin \
  'Review dependencies and report security or compatibility concerns.' \
  --repository-root . --coalesce-policy skip --max-in-flight 1
```

Creation always leaves the schedule paused and prints the first-run preview.
`--coalesce-policy` accepts `skip` (default), `queue_one`, or `allow_bounded`;
`--max-in-flight` is bounded to 1–32 and applies to `allow_bounded`. Review and
confirm the exact next-run timestamp to enable:

```bash
agenthicc schedules resume SCHEDULE_ID
agenthicc schedules resume SCHEDULE_ID --confirm-next-run '2026-10-14T09:00:00+02:00'
```

The confirmation is deliberately exact, includes seconds and offset, and is
invalidated when a schedule is edited. Every edit requires `--confirm`, creates
a new audited revision, recomputes its preview, and pauses the schedule. Resume
it again only after reviewing the new preview.

## Recurrence and time-zone rules

Three recurrence forms are supported:

| Kind | Example | Semantics |
|---|---|---|
| One-shot | `once:2026-10-20T09:00:00` | An ISO-8601 instant; a local time uses the selected IANA timezone and must be in the future. |
| Fixed interval | `interval:6h` | Positive integer duration in seconds, minutes, hours, or days; anchored to the prior scheduled tick. |
| Cron | `cron:15 9 * * 1-5` | Five numeric fields: minute, hour, day-of-month, month, weekday. Sunday is 0 or 7. |

Always provide an IANA timezone (for example `UTC` or `Europe/Berlin`). Fixed
UTC offsets and unknown zone names are rejected. When both cron day-of-month
and weekday fields are restricted, standard cron OR semantics apply.

DST and clock changes are deterministic:

- A nonexistent local one-shot time is rejected rather than silently shifted.
- An ambiguous local one-shot time selects the earlier instant (`fold=0`).
- A cron occurrence at a nonexistent spring-forward wall time is skipped.
- An ambiguous fall-back cron wall time runs once, at the earlier instant; it
  is not repeated during the second fold.
- After downtime, the scheduler records at most the due occurrence it claims
  and advances to the first future tick. It does not replay every missed tick.
- Interval schedules stay anchored to their prior scheduled occurrence. A
  backward system-clock adjustment cannot move the persisted cursor backward
  or claim an occurrence key twice.

## Inspect and operate schedules

CLI operations:

```bash
agenthicc schedules list [--repository-root PATH] [--all] [--json]
agenthicc schedules show SCHEDULE_ID [--json]
agenthicc schedules pause SCHEDULE_ID
agenthicc schedules run-now SCHEDULE_ID [--json]
agenthicc schedules history SCHEDULE_ID [--limit 50]
agenthicc schedules retry SCHEDULE_ID OCCURRENCE_ID [--json]
agenthicc schedules update SCHEDULE_ID FIELD VALUE --confirm
agenthicc schedules delete SCHEDULE_ID --confirm
```

`run-now` creates an auditable manual occurrence without changing the
recurrence cursor. If the service is stopped, it remains queued as
`awaiting_service`. A retry creates a new occurrence, a new session ID, and a
new attempt linked to the failed/blocked occurrence. Deletion tombstones the
definition and prevents future scheduled ticks; existing occurrence records,
sessions, and worktree artifacts remain inspectable.

In the TUI, `/schedules` opens the paginated schedule table. `Enter` queues a
run now, `p` pauses or resumes (the displayed preview is the confirmation),
`i` expands prompt details, `n` inserts `/schedule create ` into the composer,
and `d` opens a delete confirmation. `/schedule create WORKFLOW PROFILE MODE
RECURRENCE TIMEZONE -- PROMPT` creates a disabled schedule from the current
repository. The CLI remains available for editing, history, and JSON output.

## Concurrency, runs, and safety

The default policy is one active occurrence per schedule. `skip` records a due
tick as skipped when a run is already in flight. `queue_one` retains at most
one pending tick and coalesces additional missed ticks into it. `allow_bounded`
admits work up to `max_in_flight`; excess due ticks are recorded as skipped.
These policies bound work and do not create a backlog after service downtime.

Each occurrence has a unique durable key and deterministic session ID. The
cursor, occurrence, schedule revision, and initial receipt are persisted
before dispatch. If the service exits during dispatch, the next service owner
reconciles the same occurrence/session identity instead of creating a second
logical run. A cross-process dispatch lock prevents overlapping service ticks
from concurrently dispatching that occurrence. Manual `run-now` admission
checks in-flight counts and inserts the occurrence in one SQLite transaction,
so simultaneous CLI/TUI requests cannot overrun the selected coalescing policy.

The selected permission ceiling is saved on each schedule revision and is
never overridden by a run prompt. `Plan` runs use the repository checkout in
read-only mode. `Safe` and `Yolo` runs use a dedicated Git worktree at a
recorded base commit; Agenthicc never automatically merges their changes.
Worktree environment setup follows the exact digest/trust contract described
in [Worktree environments and actions](worktree-environments.md).

## Persistence and troubleshooting

Schedule definitions, revisions, audit entries, and occurrence receipts live
in the private SQLite database `~/.agenthicc/schedules/schedules.sqlite3`.
The scheduler uses `scheduler.lock` for service ownership and `dispatch.lock`
to serialize cross-process dispatch. These are advisory OS locks; a process
crash releases them while leaving the committed database state available for
recovery. See the [storage reference](../reference/storage.md#repository-schedules)
for the complete storage contract.

If an enabled schedule says `awaiting service`, start `agenthicc session serve`
and inspect `agenthicc schedules service-status`. A `blocked` occurrence is
not retried automatically when doing so could duplicate work; inspect
`schedules show`/`schedules history`, then explicitly retry if appropriate.
Provider credentials are resolved from the configured profile/environment at
run time and are not copied into schedule records.
