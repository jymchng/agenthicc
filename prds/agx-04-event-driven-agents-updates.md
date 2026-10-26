---
title: "AGX-04: Event-driven live updates and targeted maintenance"
status: Implemented
version: 0.1.0
date: 2026-10-23
repository: jymchng/agenthicc
priority: P1
series: AGX
related_prds: [agx-00, agx-03, agx-07, agx-14, prd-150, prd-209, prd-225, prd-227]
tags: [agents, performance, maintenance, events, storage]
---

# AGX-04 — Event-driven live updates and targeted maintenance

## Implementation status (2026-10-23)

Implemented. The agents screen (`BackgroundManager.run`) has no refresh timer
and no maintenance timer.

- **Change notification (§5.1).** `agenthicc.background.watch.FileWatcher`
  watches named files, directory entries and process exits: inotify (via
  ctypes) and `pidfd_open` on Linux — falling back to the raw system call where
  the Python build lacks `os.pidfd_open` — kqueue `EVFILT_VNODE`/`EVFILT_PROC`
  on macOS and BSD, and a `stat` of each path every `refresh_interval_s`
  elsewhere. A 2 s stat sweep runs alongside native notification and arms
  watches whose directory appears later. The screen watches `events.jsonl`,
  `input-inbox/` (inotify reports which file changed) and the selected
  session's journal, and sleeps on keys, watcher changes, finished background
  tasks (each wakes the loop) and the next maintenance deadline. A registry
  change refreshes the visible page and counts. **Deviation:** Windows has no
  `ReadDirectoryChangesW` backend; it uses the stat fallback, which acceptance 3
  requires to meet the same latency.
- **Due-driven maintenance (§5.2).**
  `agenthicc.background.maintenance.MaintenanceScheduler` replaces the
  1,024-session batch cursor (`recover_stale_batch`, the store's
  `_lifecycle_page_after` and the index's `ids_after` are gone). Workers are
  probed when first seen, when their process exits, when the heartbeat deadline
  passes or a stale heartbeat resumes, and — only where the exit cannot be
  watched — every `maintenance_interval_s`. Exit watching uses pidfd for every
  active worker, not only the supervisor's own children, after a first-sight
  probe verifies the worker's identity; a PID reused in the moment between that
  probe and opening the pidfd would only delay detection to the heartbeat
  deadline.
  Deferred-input candidates come from the input-inbox directory and are re-read
  only when their file changes; a failed start backs off exponentially (capped
  at five minutes), and capacity-blocked input waits for a registry change.
  Dispatch runs when pending requests exist and the registry changed, or a
  claimed lease expires. Registry- and inbox-prompted passes are coalesced to
  one per second; exits and `r` run at once. `recover_stale()` is one full pass.
  **Not done:** the daily trash-retention timer. The guide describes
  `jobs purge` as a deliberate operator action, and purging recoverable trash
  from an open screen would delete user data that today is only removed on
  request; retention stays an explicit command.
- **Delta update events (§5.3).** `update()` and `restore_deleted()` write only
  changed fields plus `last_active`. One coupling needed care: `evolve` reads a
  `mode_name` that arrives without `mode_application_status` as a new pending
  request, so a changed mode name is always written with its status.
- **Live detail (§5.4), deviation.** The detail activity follows the session's
  journal through the same watcher in every case instead of subscribing to the
  session service's SSE stream, which reads the same journal; the bounded tail
  is re-read only when the journal changes.

Fixed on the way: flagging a stale heartbeat went through `update()`, which
stamps `last_active`, so the next pass saw a fresh heartbeat and cleared the
flag — a silent worker flapped between stale and healthy every ~35 s; the
flag now keeps the heartbeat age. The Windows worker probe called
`os.kill(pid, 0)`, which is `CTRL_C_EVENT` on Windows; it now returns
"unknown" before signalling. `background.stale_after_s` was never applied;
`BackgroundSupervisor.from_settings()` now builds every supervisor from the
settings, which also gives the worker, parallel-plan and worktree-tool
supervisors the configured cancel grace and wall timeout.

Measured with `scripts/benchmark_agents_manager.py` on the development host
(load average 29–60 on 8 vCPUs; the screen runs in process with a scripted
terminal; one change at a time, 50 samples):

| Measure | 1k sessions / 50k events | 10k sessions / 200k events | Budget |
|---|---:|---:|---:|
| Idle screen CPU, 30 s | 0.03% | 0.03% | ≤ 1% |
| Work during the idle window | 0 refreshes, 0 passes, 0 frames | 0 refreshes, 0 passes, 0 frames | — |
| Change by another writer → visible | p50 5 ms, p95 104 ms | p50 7 ms, p95 191 ms | p95 ≤ 250 ms |
| Maintenance pass with nothing due | 0 probes, 0 bytes, p50 0.3 ms | 0 probes, 0 bytes, p50 0.3 ms | 0 probes, 0 events |
| Single-field `updated` event | 166 B of 1,739 B (9.5%) | 168 B of 1,742 B (9.6%) | ≤ 25% |
| Worker exit → orphaned (pidfd) | < 1 s (e2e test) | — | ≤ 1 s |

The p95 tails track scheduling stalls on the loaded host — the writer's own
append has a similar p99. Changes fired back to back see a p50 near 80 ms
because each waits for the previous frame's Rich render on the event loop, the
cost AGX-11 targets. A first pass over a large backlog of expired sessions
still pays one durable append per reconciled session; batching those appends
is not part of this PRD.

Tests: `tests/unit/test_background_watch.py` (each backend, entry names,
replacements, late creation, process exits), `tests/unit/test_background_delta_events.py`
(delta size, a 60-case replay property including attested mode changes, mixed
full-record and delta logs), `tests/unit/test_background_maintenance.py`
(nothing due → no probes or appends, heartbeat expiry and resume, exits, new
attempts, polling only unwatched workers, inbox candidates, backoff, capacity,
dispatch and lease deadlines, the Windows probe, `from_settings`), and
`tests/e2e/test_agents_manager_event_driven_e2e.py` (idle screen does no work,
another process's change appears promptly with native and stat backends, a
killed worker is orphaned within a second, the detail view follows its
journal).


## 1. Summary

An open agents screen polls: it requests a page refresh four times a second
(`refresh_interval_s = 0.25`) and runs a maintenance pass every 5 s that walks
up to 1,024 sessions probing
workers and pending input. With 1,000 sessions an idle screen uses 3–6% of a
core indefinitely; a maintenance pass takes 1.4 s at 1,000 sessions and 2.3 s at
10,000. Every `store.update()` also writes the *entire* session record into
the authoritative log, whatever changed. Replace polling with change
notification, make maintenance proportional to the sessions that are actually
due, and write update events as deltas.

## 2. Problem and evidence

### 2.1 Polling loop

`BackgroundManager.run()` (`src/agenthicc/tui/workspace/background_manager.py:2811`)
waits up to `refresh_s` for a key — `refresh_interval_s`, 0.25 s by default
(`src/agenthicc/background/settings.py:220`) — calls
`_request_async_refresh("poll")` on every iteration and schedules maintenance
every `maintenance_interval_s` (5 s).
Maintenance calls `recover_stale_batch` (`src/agenthicc/background/supervisor.py:2322`),
which takes the next 1,024 sessions by ID and runs `_recover_stale_records`,
`_recover_pending_input_records` and `dispatch_pending` over them — regardless
of whether any of them changed.

### 2.2 Measured

`agenthicc agents` opened in a pseudo-terminal on a 1,000-session / 50,000-event
registry, then left idle for 60 s (AGX-00 §4.2):

| Interval after sessions appear | CPU | Log growth |
|---|---:|---:|
| 0–10 s | 2.81 s | 1,874,975 bytes |
| each later 10 s | 0.28–0.56 s | 0 |

- **Steady state:** 3–6% of one core with nothing happening.
- **First pass:** maintenance reconciled every fixture session whose heartbeat
  had expired, writing 1,000 `updated` events — 1.9 MB, about 1.9 KB per event.
- In-process benchmark: one maintenance pass takes 1.40 s (1k sessions) and
  2.29 s (10k).

### 2.3 Full-record update events

`BackgroundStore.update()` (`src/agenthicc/background/store.py:990`) builds the
event from the whole record:

```python
updated = current.evolve(**changes)
serialized_changes = updated.to_dict()      # every field, ~50 of them
```

So marking one session `heartbeat_stale`, changing its latest activity line or
pinning it appends a full copy of the session, including its phase and attempt
history. The fold applies `updated` events as field overrides
(`current.evolve(**changes)`, `store.py:386`), so nothing requires the full
copy.

## 3. How others approach it

- Claude Code's subagent panel updates as subagents run and removes a finished
  row after 30 s; background results arrive as completion notifications rather
  than through a polled list.
- Grok Build's tasks pane lists "everything currently running", and monitors
  turn each output line into a notification in the conversation.

The common shape is push: state changes produce notifications, and lists
re-render because something changed, not because a timer fired.

## 4. Goals and non-goals

**Goals**

1. An idle agents screen (and an idle TUI with background work) uses
   effectively no CPU.
2. A change made by any process is visible in an open screen within 250 ms.
3. Maintenance work is proportional to sessions that are due (expired heartbeat
   deadline, pending input, queued dispatch), and zero when none are.
4. Update events carry only the fields that changed.

**Non-goals**

- Autonomous restarts of finished sessions (PRD-219 keeps resume explicit).
- Replacing the inter-process lock or the append discipline.

## 5. Design

### 5.1 Change notification

The store exposes `changes() -> AsyncIterator[ChangeToken]`:

- **Linux:** an inotify watch on the registry directory (`IN_MODIFY` on
  `events.jsonl`, `IN_MOVED_TO` for atomic replacements).
- **macOS:** kqueue `EVFILT_VNODE` on the log file.
- **Windows:** `ReadDirectoryChangesW` on the directory.
- **Fallback:** a 250 ms `stat` of `(inode, size)` — one system call, no reads.

On a change the manager applies the tail to the index (AGX-03) and re-queries
only the visible page and counts. The 250 ms refresh timer is removed; the render
loop wakes on keys, change tokens or animation frames only.

### 5.2 Due-driven maintenance

With AGX-03's `heartbeat_deadline` column, maintenance becomes a set of indexed
queries plus targeted probes:

| Concern | Trigger | Work |
|---|---|---|
| Dead or mismatched worker | `heartbeat_deadline < now` | Probe only those PIDs |
| Live worker exit | `pidfd_open` + poll on Linux ≥ 5.3 for workers started by this supervisor; heartbeat deadline elsewhere | Mark exit immediately instead of waiting for the next pass |
| Pending deferred input | Inbox change event (PRD-227) | Recover that session only |
| Queue dispatch | Queue change or worker slot freed (PRD-225) | `dispatch_pending` once |
| Trash retention | Daily timer | Purge expired trash |

A scheduler keeps the next deadline and sleeps until then. When nothing is due,
maintenance does no work and no I/O.

### 5.3 Delta update events

`BackgroundStore.update()` writes only the fields whose values changed, plus
`last_active`:

```python
delta = {name: value for name, value in updated.to_dict().items()
         if value != previous[name]}
```

The fold already applies `changes` with `evolve`, so existing logs (whose
`updated` events list every field) and new logs replay identically; no reader
change or log migration is needed. `created` events keep the full record.

### 5.4 Live detail without polling

The detail view (AGX-07) follows a session's activity:

- If the local session service is running, subscribe to its existing stream
  endpoint (`GET /v1/sessions/{id}/stream`, server-sent events, PRD-150).
- Otherwise, watch the session's journal file with the same notification
  primitive and read only appended bytes.

Either way the periodic activity re-read in `_schedule_activity_read` (every
refresh interval, never more often than 250 ms) is removed.

## 6. Budgets and acceptance criteria

| Measure | Budget |
|---|---:|
| Idle agents screen CPU, 10k sessions, 60 s | ≤ 1% of one core |
| Event appended by another process → visible in open screen | p95 ≤ 250 ms |
| Maintenance work when nothing is due | 0 probes, 0 appended events |
| Single-field `updated` event size | ≤ 25% of the full record |
| Worker exit → status visible (Linux, supervisor-owned worker) | ≤ 1 s |

1. With no session due, a 10-minute idle run appends nothing to `events.jsonl`.
2. Replaying a log that mixes full-record and delta `updated` events yields the
   same sessions as before (property test against the current fold).
3. The fallback `stat` path passes the same change-latency test with
   notifications disabled.

## 7. Test plan

- Pseudo-terminal idle test (AGX-14) measuring CPU and log growth over 60 s.
- Cross-process latency test: a second process appends; measure time to
  re-render.
- Unit tests for the deadline scheduler and delta computation, including
  fields that change back to a previous value.

## 8. Risks

- **Missed notifications** (network filesystems, editors replacing files).
  Mitigation: the `stat` fallback runs alongside native watches at a slow rate
  (every 2 s) as a safety net.
- **PID reuse.** pidfd removes the reuse race on Linux; elsewhere the existing
  process-identity checks stay.

## Sources

- Claude Code subagents (panel, completion notifications): <https://code.claude.com/docs/en/sub-agents>
- Grok Build background tasks: <https://docs.x.ai/build/features/background-tasks>
- Linux `pidfd_open(2)`: <https://man7.org/linux/man-pages/man2/pidfd_open.2.html>
- Linux `inotify(7)`: <https://man7.org/linux/man-pages/man7/inotify.7.html>
