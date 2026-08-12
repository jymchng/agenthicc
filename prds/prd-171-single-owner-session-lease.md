# PRD-171 — Single Live Owner for Resumed Sessions

**Status:** Implemented  
**Date:** 2026-08-10  
**Scope:** CLI startup, TUI session attachment, headless session attachment,
session index persistence, session diagnostics, and process-safe ownership of
durable conversations.  
**Related:** PRD-150, PRD-156, PRD-158, PRD-169, PRD-170

## Summary

Prevent two `agenthicc` processes from attaching to and writing the same
durable session at the same time. In particular, two terminals running
`agenthicc --continue` must not both load the most recent transcript and then
send turns, tools, or workflow transitions against the same conversation.

The solution is a session-scoped, crash-recoverable owner lease acquired before
transcript, journal, provider, workflow, or tool state is opened. The lease is
distinct from the existing per-workflow `.claim` lease: the session lease
protects the entire interactive conversation, while a workflow claim continues
to protect one workflow run and its side effects.

If another live process owns the selected session, the second invocation must
fail quickly with a typed `session_already_active` diagnostic. It must not load
the transcript, initialize the LLM, create a second `SessionConversation`,
start the TUI, fall back to an older session, or silently create a new session.
Claims left by a crashed process may be reclaimed only when the owner is
provably dead. Unknown, malformed, cross-host, or otherwise unverifiable owner
records fail closed.

## 1. Evidence-backed current state

### 1.1 Current `--continue` path

The current flow is:

```text
agenthicc --continue
  -> parse CLI flags
  -> find_latest_session_for_cwd()
  -> _run_tui_session(resume_id=<selected ID>)
  -> _build_session_context(resume_id)
  -> SessionService.ensure_session()
  -> restore kernel log and touch index
  -> SessionConversation.open()
  -> construct providers, tools, workflows, and TUI
  -> replay conversation.jsonl
  -> accept input
```

`find_latest_session_for_cwd()` reads `~/.agenthicc/sessions/index.json` and
chooses the record with the greatest `last_active` value. The index read and
the subsequent session construction are separate operations. There is no
process-wide ownership check between them.

Consequently, this race is currently possible:

```text
Terminal A                         Terminal B
----------                         ----------
read index -> selects S            read index -> selects S
open S and replay transcript       open S and replay transcript
start input loop                   start input loop
write conversation/journal S       write conversation/journal S
```

The resulting failure is not limited to duplicate display. Both processes can
send provider requests, append interleaved events, execute the same workflow
phase, claim different in-memory turns, or repeat external tool effects.

### 1.2 Existing protection is narrower

`WorkflowCheckpointStore` publishes `<session>/<workflow-run>/.claim` with
atomic installation and PID/host/process-start diagnostics. It prevents two
owners from resuming one workflow run, but it is acquired only after a session
has already been constructed. It therefore cannot prevent two terminals from
opening the same conversation, and it does not cover direct turns or transcript
replay.

`BackgroundStore` and `TerminalStore` contain short critical-section registry
locks. Those locks protect local registry updates, not long-lived ownership of
a conversation. Their current best-effort lock fallback must not be used as an
authority for this feature: silently proceeding when an inter-process lock
cannot be acquired would violate the single-owner invariant.

`metadata.json`, `index.json`, the kernel event log, `conversation.jsonl`, and
the conversation journal are durable records, but none is currently an active
owner marker. Their presence or `last_active` timestamp must never be treated
as proof that a process owns or has released a session.

### 1.3 Affected entry points

The same ownership contract must cover every local path that can attach to a
durable conversation:

- `agenthicc --continue`;
- `agenthicc --resume <session-id>`;
- selecting a session with Enter from the interactive sessions UI;
- headless workflow execution when it receives an existing session ID;
- any future CLI or client adapter that opens the existing session directory.

In-process subagents and workflow phases do not acquire a second session lease.
They execute under their parent process's lease. A background job that writes a
different session remains independent; a job that explicitly attaches to an
existing session must use this same open/claim API.

## 2. Problem statement

`--continue` is a session attachment operation, but the application currently
treats it as a read-only lookup followed by ordinary construction. The absence
of a durable, process-aware owner means that the same session ID has multiple
writers. This is especially damaging because resume now restores the full
conversation/journal and can recover interrupted workflow state: duplicate
owners can replay the same pending turn or tool transaction at the exact point
where correctness requires one owner.

The product needs an explicit invariant:

> At most one live local agenthicc owner may attach to a given durable session
> ID at any time. No attach path may read or mutate the session as an owner
> before it has acquired that session's lease.

The invariant must survive normal exit, exceptions, Ctrl-C, terminal closure,
process crashes, zombie processes, PID reuse, partial writes, and concurrent
startup races without relying on an arbitrary timeout.

## 3. Goals

### G-1 — Make session ownership exclusive

Two processes racing for the same session ID have a deterministic result:
exactly one acquires the owner lease and the other receives a structured
conflict. This applies even when both resolved the same latest session from
the index at the same time.

### G-2 — Claim before any session restore work

The winning process acquires the lease before `SessionService.ensure_session`,
kernel-log restoration, `touch_session`, `SessionConversation.open`, provider
construction, tool discovery, workflow construction, or visual transcript
replay. A losing process performs none of those operations.

### G-3 — Preserve existing resume semantics

The owner keeps the stable session ID and existing conversation ID. It can
continue direct turns, Plan mode, `code_plan`, `create_workflow`, and other
checkpoint-aware workflows without a new conversation or a split transcript.

### G-4 — Recover safely after abnormal termination

A claim left by SIGKILL, a crash, host process failure, or power loss does not
permanently strand the session. The next attach may reclaim it only after the
owner is proven dead using host, PID, and process-start identity where
available.

### G-5 — Keep the user-facing failure actionable

The second terminal exits promptly with a concise message naming the session,
the conflict code, and bounded owner diagnostics such as PID, host, and age.
It tells the user to close or use the original agenthicc process. It never
prints prompts, transcript content, tool arguments, API keys, or provider
secrets.

### G-6 — Make every attach path use one authority

`--continue`, explicit resume, the session picker, headless resume, and future
clients call one typed session-open coordinator. There must not be a second
ad-hoc lock implementation in the TUI or CLI.

### G-7 — Retain workflow-level defense in depth

The new session lease is an outer ownership boundary. Existing workflow claims
remain required for workflow-run ownership, including headless execution and
workflow recovery. The two leases must have explicit nesting and cleanup rules.

## 4. Non-goals

- Allowing two interactive terminals to merge or collaboratively edit one
  transcript in the initial release.
- Forcibly taking a lease from a process that is demonstrably alive.
- Reconstructing a missing lease from `last_active`, terminal title, or an
  elapsed-time threshold.
- Undoing filesystem, Git, network, browser, MCP, or command side effects that
  have already completed.
- Replacing the existing workflow checkpoint or tool-transaction recovery
  protocols from PRD-169 and PRD-170.
- Serializing provider clients, browser handles, asyncio tasks, live approvals,
  or credentials into the owner record.
- Guaranteeing ownership across hosts when the session directory is on a
  shared filesystem and remote process liveness cannot be verified.
- Introducing a general distributed lock service in this local-first feature.

## 5. Product and ownership contract

### 5.1 One owner per durable session

The session ID is the unit of exclusion. The owner lease covers all writes to
the session's durable artifacts, including:

- `conversation.jsonl` and `conversation-journal.jsonl`;
- the kernel event log and session index metadata;
- workflow checkpoints and workflow claim coordination;
- usage accounting, tool transaction repair, and resume markers;
- browser/session artifacts and owned terminal metadata where applicable.

The session lease does not make the individual files safe for concurrent
writers. It prevents concurrent writers from being created in the first place.

### 5.2 `--continue` selection policy

`--continue` keeps its meaning: continue the most recently active session for
the canonical current working directory.

The coordinator must:

1. canonicalize the current directory in the same way session registration
   does;
2. acquire the short-lived session-index lock;
3. load and validate the index, distinguishing “no index/no matching session”
   from “index unreadable or corrupt”;
4. choose the newest matching record using `last_active`, with a stable
   session-ID tie-breaker;
5. attempt the selected session's owner lease while the selection decision is
   still under coordination;
6. return the selected session ID and lease, or a typed conflict/error.

If the selected newest session is active, `--continue` must not silently choose
an older session or create a new session. The user can close the owning process,
resume that process, or invoke agenthicc without `--continue` to intentionally
start a fresh conversation.

If no matching session exists, the command may create a new session. If the
index cannot be read, parsed, or safely validated, it must fail with an index
diagnostic rather than interpreting corruption as “no previous session.”

### 5.3 Explicit resume and session-picker policy

`--resume <session-id>` and sessions-list Enter must call the same coordinator
with an explicit ID. They must not bypass the lease simply because a user
selected the ID directly.

An explicit ID is validated as a safe session identifier and must resolve to an
existing session directory/index record according to the current compatibility
rules. A missing or corrupt session is reported before provider startup. The
selected session is claimed exactly once and released by the process that
claimed it.

### 5.4 Fresh sessions

A fresh session receives a new cryptographically random ID and acquires its
owner lease before registration. The probability of a fresh-ID collision is
negligible, but applying the same API keeps all session construction paths
uniform and prevents a future caller from accidentally skipping ownership.

### 5.5 Re-entrancy

The same process may call ownership-aware helpers more than once for the same
session during startup only if it presents the same opaque owner token. The
helper must return the existing process-local lease rather than publish a
second owner record. A different owner token in the same process is rejected;
this catches accidental double attachment in tests or future client code.

In-process subagents, workflow phases, and tool calls inherit the parent lease
and never attempt to acquire it again.

## 6. Proposed architecture

### 6.1 Canonical components

Add one ownership module under the runner/session boundary, for example:

```text
src/agenthicc/runners/session_lease.py
```

The final module name may follow the repository's naming conventions, but the
ownership API must be centralized. It should expose typed equivalents of:

- `SessionOwnerLease` — immutable identity plus release state;
- `SessionAlreadyActiveError` — conflict metadata for UI/CLI rendering;
- `SessionOpenError` — invalid, corrupt, unsupported, or unavailable session
  errors;
- `SessionOpenCoordinator` — index selection plus lease acquisition;
- `open_session_owner(session_id, ...)` — explicit-ID acquisition for callers
  that already selected a session.

The implementation should extract or share process-identity and atomic-claim
logic with `WorkflowCheckpointStore`; it must not copy a subtly different PID,
host, or stale-owner algorithm. The existing workflow claim tests are a source
of regression cases, not a reason to make session ownership depend on a
workflow checkpoint.

### 6.2 Owner record

The proposed durable layout is:

```text
~/.agenthicc/sessions/
  index.json
  index.lock                 # short index critical sections only
  <session-id>/
    .owner                   # durable live-owner metadata, 0600
    .owner.lock               # short per-session owner mutation lock
    metadata.json
    conversation.jsonl
    conversation-journal.jsonl
    ...
```

`.owner` is the authority for active ownership. It is not a transcript,
checkpoint, or user-editable configuration file. Its schema is versioned and
contains only bounded operational data:

```json
{
  "schema_version": 1,
  "session_id": "<id>",
  "owner_id": "tui:<pid>:<nonce>",
  "pid": 12345,
  "host": "machine-name",
  "process_start_token": "<host-boot>:<start-time>",
  "acquired_at": 1786380000.0,
  "entrypoint": "tui"
}
```

`owner_id` is an opaque, high-entropy token and is the authority for release;
PID alone is never sufficient. `cwd` may be retained only if needed for a
bounded diagnostic, and must be canonicalized. Full argv, prompts, model
messages, tool arguments, environment variables, and secrets are prohibited.

Session directories are created with restrictive permissions consistent with
the existing session store. The owner file is written completely to a private
temporary file, fsynced where supported, and published atomically. A killed
process must leave either no visible owner or one complete record; an empty or
partially written visible owner must not permanently block recovery.

### 6.3 Liveness and stale-owner rules

For a record on the local host:

1. verify the owner record schema and session identity;
2. check whether the PID exists;
3. treat a missing process as reclaimable;
4. treat a zombie process as reclaimable when the platform exposes that state;
5. compare the recorded process-start token with the current process at that
   PID when available; a mismatch proves PID reuse and is reclaimable;
6. treat permission failures, unsupported identity checks, and ambiguous
   results as live/unknown and fail closed.

For a record from another host, local code cannot prove that its owner is dead.
It must fail closed and identify the recorded host. A timestamp or heartbeat
may be displayed as diagnostic age, but age alone must never authorize
reclamation. On platforms without a reliable process identity mechanism, the
implementation must use the strongest supported OS primitive and otherwise
fail closed rather than claim stale ownership optimistically.

Stale replacement is itself a race: multiple new processes may observe a dead
owner. Reclamation and publication must therefore be atomic so exactly one
replacement wins. A process that loses that race reports the current owner,
not the stale record it first observed.

### 6.4 Advisory locking and portability

The implementation may use an OS advisory lock as an additional guard, but
must not assume that an advisory lock alone supplies diagnostics or survives
all filesystem/process failure modes. If a file-lock backend cannot establish
the required exclusion, ownership acquisition fails closed.

The short-lived `index.lock` protects read-modify-write operations on
`index.json`. It must not be held for the lifetime of a TUI. The long-lived
per-session owner record is the authority for the attachment lifetime.

The POSIX backend should use the repository's existing `fcntl` conventions
where suitable. Windows needs an explicit tested backend or a conservative
unsupported result. The current background stores catch lock errors and
continue; that behavior is acceptable for their registry convenience but is
not acceptable for this safety invariant.

### 6.5 Integration boundary

The owner lease must be acquired at the earliest common point:

```text
CLI parse
  -> resolve explicit ID or latest-for-cwd
  -> SessionOpenCoordinator.acquire()
       -> conflict: render error and exit
       -> success: return SessionOwnerLease
  -> _build_session_context(..., owner_lease=lease)
       -> ensure service / restore logs / open memory / providers / tools
       -> create TUI or headless runner
  -> run
  -> finally: release_if_owner()
```

The lease must be present in `SessionContext` (or an equivalent typed
session-owned resource) so cleanup cannot depend on a module global. Partial
construction must release the lease even if configuration, provider setup,
plugin discovery, browser initialization, MCP discovery, or TUI creation
fails. A context that reaches the caller owns the lease until its normal
close path runs.

The TUI's current `--continue` resolution in `_run_tui` and session creation
in `_build_session_context` must be refactored so no work that can mutate or
replay the selected session happens before acquisition. Session replay must
remain after acquisition and must still use the bounded PRD-158 transcript
projection.

### 6.6 Cleanup contract

Every successful acquisition has one owner and one idempotent release path:

- normal TUI exit;
- Ctrl-C/KeyboardInterrupt;
- SIGTERM/SIGHUP handling where the process can run cleanup;
- provider/configuration error;
- plugin, MCP, browser, or tool initialization error;
- `SessionService` or processor startup failure;
- headless EOF and headless exception;
- cancellation during transcript replay or active agent work.

Release must verify the opaque `owner_id` before removing `.owner`. A late or
misbehaving cleanup callback must never delete a newer owner's record. If a
process is forcibly killed, the record may remain until the next safe
acquisition reclaims it.

The owner record is removed only after session writes and resource shutdown
have reached the existing cleanup boundary. The implementation must decide and
document whether release precedes or follows `touch_session`; it must not leave
an index update racing with owner replacement.

## 7. User experience and diagnostics

### 7.1 Conflict in the TUI

The conflict is detected before Rich Live, transcript replay, spinner startup,
LLM construction, or tool discovery. The process prints a concise error to
stderr and exits with a non-zero, stable session-conflict status. It must not
show a blank or indefinitely waiting TUI.

Illustrative output:

```text
error: session_already_active
Session 8217… is already open by another live agenthicc process
(pid=3845622, host=vmi1175516, age=12s, entrypoint=tui).
Close that process or continue the session in that terminal.
No transcript was loaded and no new session was created.
```

The full session ID is available where it is safe and useful; a display-short
ID must never make the selected session ambiguous. Owner fields are bounded and
escaped before rendering.

### 7.2 Headless conflict

Headless mode emits one machine-readable error record before any `ready`
record, then exits non-zero:

```json
{
  "status": "error",
  "code": "session_already_active",
  "session_id": "<id>",
  "owner": {"pid": 3845622, "host": "vmi1175516", "entrypoint": "tui"}
}
```

The schema must not include transcript, prompts, command lines, credentials,
or arbitrary exception reprs. Human and automation output must share the same
typed error source.

### 7.3 Session list integration

The interactive sessions list may read owner metadata for display, but reading
the list must not acquire a session lease. Pressing Enter invokes the same
explicit-ID open coordinator. A busy session is shown as `active` with bounded
owner diagnostics and remains selectable only to show the conflict; Enter must
never start a second owner.

The list must distinguish:

- `available` — no current owner;
- `active` — live or unknown owner;
- `recoverable` — owner is provably dead and can be reclaimed;
- `corrupt/unknown` — ownership cannot be safely classified.

Displaying `recoverable` does not itself delete or replace the owner record.

### 7.4 No force-unlock in v1

There is no unconditional `--force`, `sessions unlock`, or “delete the owner
file” escape hatch in the initial implementation. A live owner may be midway
through a tool side effect, and forcibly taking the lease would recreate the
duplicate-execution defect. If operations support a future administrative
override, it must be a separately specified, strongly confirmed, auditable
feature with a fail-closed default.

## 8. Data flow and race handling

### 8.1 Successful `--continue`

```text
argv
  -> CLIContext(continue_session=True)
  -> canonical cwd
  -> locked SessionIndexStore read
  -> latest session S
  -> SessionOwnerLease.acquire(S, owner=A)
  -> atomic .owner publication
  -> release index.lock
  -> _build_session_context(S, owner=A)
  -> restore kernel/journal/memory
  -> open provider/tools/workflows
  -> replay bounded visual transcript
  -> turns and workflow phases append to S
  -> cleanup durable writes and resources
  -> release .owner if owner=A
```

### 8.2 Two-terminal race

```text
Terminal A                         Terminal B
----------                         ----------
select S                           select S
acquire(S, owner=A)                acquire(S, owner=B)
publish .owner=A                   sees complete live .owner=A
build/replay/run                   raise session_already_active
                                   render error and exit
                                   no journal/provider/TUI work
```

If both calls see no owner, atomic publication determines the winner. The
loser must retry its read and report the winner rather than deleting or
overwriting a live record.

### 8.3 Crash and stale reclaim

```text
owner A publishes .owner
owner A is SIGKILLed
owner B selects S
  -> reads .owner=A
  -> proves PID missing/zombie or start-token mismatch
  -> atomically replaces stale claim with owner=B
  -> restores S exactly once
```

If owner A is still live, has unknown liveness, has a reused/unverifiable PID,
or is recorded on another host, owner B stops with a conflict. It must never
choose an older session or start fresh as an implicit workaround.

## 9. Functional requirements

### FR-1 — Typed ownership API

Implement a canonical session-open/lease API with explicit acquire, inspect,
and release operations. It must expose structured conflict and storage errors
without requiring callers to parse strings.

### FR-2 — Atomic exclusive acquisition

Acquisition must publish a complete owner record atomically and ensure exactly
one winner under concurrent processes. Partial visible records must not block
future recovery forever.

### FR-3 — Pre-restore acquisition

No selected-session restore, transcript read, journal open, `last_active`
touch, provider request, tool discovery, or workflow construction may happen
before lease acquisition succeeds.

### FR-4 — Latest-session coordination

`--continue` must resolve and claim the selected latest session through the
coordinator. Index read/write critical sections must be process-safe and
atomic. Corrupt index state must produce an error, not a fresh-session fallback.

### FR-5 — Explicit resume parity

`--resume`, sessions-list Enter, headless existing-session execution, and every
future durable-session adapter must use the same ownership contract.

### FR-6 — Live-owner conflict

A live or unclassifiable owner produces `session_already_active`, including
bounded session/owner diagnostics. The losing process exits before TUI or LLM
startup and does not mutate session state.

### FR-7 — Safe stale recovery

Reclaim only a local owner that is provably dead, a known zombie, or a PID whose
recorded process-start identity no longer matches. Never reclaim solely by age,
and fail closed for another host, malformed metadata, permission errors, or
unsupported liveness.

### FR-8 — Owner-safe release

Release is idempotent and compares the opaque owner token before deletion. It
is wired into all TUI/headless success, error, cancellation, and partial-start
paths.

### FR-9 — Lease inheritance

Subagents, workflow phases, tools, and workflow claims created by an attached
session inherit the session owner. They cannot accidentally create a second
session owner in the same process.

### FR-10 — Workflow claim compatibility

Existing per-run workflow claim acquisition/release remains intact. A session
conflict must be resolved before any workflow claim is created; session lease
release must not delete a workflow claim owned by another execution.

### FR-11 — Session inspection

The sessions UI and any existing inspection/list command can classify owner
state without claiming it. Owner metadata shown to users is bounded, escaped,
and secret-free.

### FR-12 — Compatibility and migration

Sessions created before this feature, with no `.owner` record, remain resumable
and can be claimed. Existing `metadata.json`, index fields, conversation IDs,
workflow checkpoints, and command syntax remain compatible. No migration may
delete transcript or journal data.

## 10. Non-functional requirements

### NFR-1 — Safety

The default behavior is single-owner and fail closed. There is no timeout-only
reclamation and no silent fallback that can create a second conversation.

### NFR-2 — Crash consistency

Owner publication, stale replacement, index updates, and removal must be
atomic to the extent supported by the platform. Temporary files must be
private, bounded, and cleaned opportunistically without making cleanup a
correctness dependency.

### NFR-3 — Portability

POSIX and Windows behavior must be explicit and tested. A platform that cannot
prove the required ownership result must return a clear unsupported/storage
error rather than claim success.

### NFR-4 — Performance

The uncontended path adds one small local metadata operation and does not scan
the transcript before claiming. The index lock is held only for selection and
short metadata updates, never for an LLM turn or TUI lifetime.

### NFR-5 — Privacy

Owner and conflict records contain no API keys, authorization headers,
prompts, tool arguments/results, transcript text, or environment dumps. Error
logging follows existing redaction policy.

### NFR-6 — Observability

Safe diagnostics identify acquisition, conflict, stale reclaim, release, and
unknown-owner outcomes with session ID and bounded owner identity. Debug logs
must not log secrets or content. Metrics, if added, should count outcomes but
not label them with unbounded user data.

### NFR-7 — Determinism

Latest-session tie-breaking, error codes, state classification, and race-test
assertions must be deterministic. Tests must use temporary session roots and
controllable process/liveness adapters rather than the real home directory.

## 11. Proposed implementation plan

1. Extract the shared process identity, owner metadata validation, atomic
   publication, stale-owner classification, and owner-safe release logic from
   the workflow-claim implementation into a reusable runner-level primitive.
2. Add `SessionOwnerLease` and `SessionOpenCoordinator` around the existing
   session index and session directory layout.
3. Harden session-index read-modify-write operations with a short, explicit
   cross-process lock and atomic replacement. Preserve existing index fields.
4. Refactor `_run_tui`/`_build_session_context` so `--continue` selection and
   explicit resume acquire before session construction. Thread the lease
   through `SessionContext` and make partial construction release it.
5. Integrate the same path into sessions-list Enter and headless existing
   session execution.
6. Add typed CLI/TUI/headless diagnostics and active-owner classification to
   session inspection.
7. Audit all current callers with `rg` and remove or route any direct
   `_build_session_context(resume_id=...)` call that can bypass the coordinator.
8. Update storage/workflow/session guides and the public PRD index after the
   implementation is verified.

The implementation must not add a second conversation store, second workflow
runner, or second session persistence format. The owner lease is a small
coordination layer around the existing durable session boundary.

## 12. Testing strategy

### 12.1 Unit tests

Add isolated tests for:

- owner-record schema validation and safe identifier validation;
- unique owner-token generation and bounded metadata;
- atomic install when the destination is absent;
- second acquisition against a live owner;
- idempotent re-entry by the same owner token;
- release by owner and no-op release by a different token;
- malformed, empty, oversized, and unreadable owner records failing closed;
- local PID missing, permission denied, zombie, matching start token, and
  mismatched start token outcomes;
- cross-host owner records remaining protected;
- deterministic latest-session tie-breaking;
- corrupt/unreadable index distinguished from an empty index;
- stale replacement races where only one contender wins;
- cleanup after errors at each session-construction stage;
- redaction and bounded rendering of conflict diagnostics.

Use dependency-injected filesystem, clock, host, PID, and process-identity
adapters where possible. Do not monkeypatch the process running pytest in a way
that can remove a real owner file.

### 12.2 Integration tests

Use a temporary `~/.agenthicc/sessions` root and real subprocesses or a
multi-process test harness for:

- two simultaneous `--continue` attempts selecting the same latest session;
- exactly one `ready`/TUI startup and one `session_already_active` result;
- the losing process not opening `conversation.jsonl`, journal, provider, MCP,
  browser, or workflow resources;
- normal owner release followed by a successful second attach;
- provider/configuration/plugin failure after acquisition followed by a
  successful second attach;
- SIGTERM/SIGHUP/KeyboardInterrupt cleanup where the platform supports it;
- SIGKILL followed by safe stale reclaim;
- PID reuse simulation using distinct process-start tokens;
- a malformed or cross-host owner refusing reclaim;
- `--continue` refusing to fall back to an older session or fresh session when
  the newest selected session is busy;
- explicit `--resume` conflict parity;
- sessions-list Enter conflict and successful selection;
- headless existing-session conflict and machine-readable error output;
- index read-modify-write races preserving all records and valid JSON;
- concurrent `touch_session` updates not corrupting `index.json`;
- session owner and workflow `.claim` nested cleanup behavior.

Provider calls should be cassette/fake based. Assertions must inspect event
counts, open calls, owner state, and process exit codes—not real network output.

### 12.3 End-to-end tests

Exercise the critical user journeys through the CLI/TUI boundary:

1. create a session and exit;
2. open it with `--continue` in terminal A;
3. run `--continue` in terminal B and verify immediate conflict output;
4. submit a message in A and verify only A's session journal changes;
5. exit A and verify B can continue the same session with its transcript;
6. kill A during startup or an active turn, then verify a new invocation can
   reclaim and restore the session without a second conversation ID;
7. select the same session through the sessions UI and verify identical
   ownership behavior;
8. resume a session containing an interrupted workflow and verify the outer
   session lease and inner workflow claim both remain enforced.

PTY tests should assert that the conflict path does not start a spinner, wait
for transcript loading, or leave terminal settings altered. Non-TTY tests
should assert stable stderr/stdout and exit behavior.

### 12.4 Regression tests

Keep regression coverage for these failure classes:

- duplicate `--continue` owners;
- a busy latest session silently becoming a new session;
- a corrupt index being treated as empty;
- a stale process record being reclaimed while its PID has been reused;
- cleanup deleting a newer owner's record;
- transcript/journal load occurring before ownership acquisition;
- workflow-run claims being released when only the session owner should be
  released, or vice versa.

## 13. Acceptance criteria

### AC-1 — Concurrent continue has one winner

**Given** a valid latest session `S` for the current directory and two
processes starting `agenthicc --continue` concurrently,  
**When** both reach ownership acquisition,  
**Then** exactly one owns `S`, and the other exits with
`session_already_active` without loading or mutating `S`.

### AC-2 — Busy latest never falls back

**Given** the newest matching session is actively owned,  
**When** a second terminal runs `agenthicc --continue`,  
**Then** it reports that session conflict and does not select an older session,
create a fresh session, or send an LLM request.

### AC-3 — Conflict is fast and actionable

**Given** a live owner,  
**When** another attach is attempted,  
**Then** the result is emitted before TUI/transcript/provider startup, includes
the stable error code and bounded owner diagnostics, and explains how to
proceed.

### AC-4 — Explicit paths cannot bypass the lease

**Given** session `S` is live-owned,  
**When** another process uses `--resume S` or sessions-list Enter,  
**Then** it receives the same conflict and performs no duplicate execution.

### AC-5 — Clean release permits reattach

**Given** an owner exits through normal, cancellation, or handled-error
cleanup,  
**When** another process opens `S`,  
**Then** it acquires the lease and restores the existing transcript/journal.

### AC-6 — Crash recovery is safe

**Given** an owner is forcibly terminated,  
**When** a new process opens `S`,  
**Then** it reclaims the owner only after proving the previous owner is dead,
and restores the same session/conversation identity.

### AC-7 — Ambiguity fails closed

**Given** the owner record is malformed, cross-host, permission-blocked, or
cannot be tied to a process identity,  
**When** a new process tries to open `S`,  
**Then** it refuses to reclaim, emits a safe diagnostic, and leaves the record
untouched.

### AC-8 — PID reuse is protected

**Given** an old record contains PID `P` and start token `A`, while a new
process now has PID `P` with token `B`,  
**When** the new process opens `S`,  
**Then** it treats the old owner as gone and may reclaim without considering
the replacement process to be the old owner.

### AC-9 — Workflow protection remains layered

**Given** a session contains a resumable workflow,  
**When** the session is opened,  
**Then** the session lease is acquired before workflow restoration and the
workflow's existing run claim is still acquired before execution. Releasing
the session lease cannot remove another run's claim.

### AC-10 — No secret/content leakage

**Given** any ownership conflict or stale-owner diagnostic,  
**When** it is rendered to a terminal, JSON stream, log, or persisted owner
record,  
**Then** it contains no credentials, prompts, transcript text, tool
arguments/results, or unbounded exception data.

### AC-11 — Existing sessions remain compatible

**Given** a pre-PRD-171 session with no owner file,  
**When** it is opened with `--continue`, `--resume`, or the picker,  
**Then** it can be claimed without changing its session ID, conversation ID,
transcript, journal, or workflow checkpoints.

### AC-12 — Tests prove the race

**Given** the complete test suite,  
**When** the ownership unit, multi-process integration, and PTY/E2E suites
run in CI,  
**Then** the concurrent-owner race, crash recovery, error paths, and all
critical acceptance criteria pass deterministically without using real user
session data or network providers.

## 14. Rollout, migration, and operational safety

### 14.1 Rollout

Ship the lease behind no user-facing opt-in: single-owner behavior is the safe
default. During rollout, add bounded debug diagnostics for acquisition and
conflict outcomes, but do not log content. Monitor conflict counts, stale
reclaims, unknown-owner failures, and index-corruption failures.

### 14.2 Migration

No data migration is required. An absent `.owner` means “unowned,” not “legacy
busy.” Existing session folders are claimed on first open. Existing workflow
`.claim` files are interpreted by their current protocol and are not renamed or
deleted by session-owner cleanup.

### 14.3 Failure recovery guidance

If the user sees a live-owner conflict, they should close or continue using the
reported process. If that process is visibly gone but liveness cannot be
proven, the safe action is to inspect the reported PID/host and repair the
environment (for example, unmount an unavailable shared filesystem) rather
than deleting the owner file manually. An administrative force-unlock requires
a future PRD.

### 14.4 Documentation updates after implementation

When this PRD is implemented, update:

- `docs/reference/storage.md` with `.owner` schema, liveness, and retention;
- the session/resume guide with `--continue` conflict and recovery behavior;
- the workflow guide with outer session lease versus inner workflow claim;
- CLI help and sessions-list documentation;
- the PRD index with implementation and verification evidence.

## 15. Open decisions and assumptions

1. **Lease primitive:** Prefer extracting a shared durable process-claim
   primitive from `WorkflowCheckpointStore`; add an advisory lock only where it
   strengthens the invariant and has a tested backend. Do not reuse the
   background stores' “ignore lock errors and continue” fallback.
2. **Index locking:** Treat index hardening as part of this PRD because latest
   selection and `touch_session` currently use non-atomic read/modify/write.
3. **Conflict exit code:** Choose one stable non-zero code in implementation
   and expose the symbolic `session_already_active` code in human and JSON
   diagnostics. Do not overload a provider or workflow error code.
4. **Remote clients:** This PRD covers local process ownership. If a separate
   session-service process becomes the authoritative multi-client runtime, it
   must enforce the same session-owner state at the service boundary before
   claiming this PRD complete for remote clients.
5. **Fresh-session lease:** Acquire one for fresh sessions as well as resumed
   sessions to make the construction API uniform, even though fresh IDs do not
   normally collide.
6. **Heartbeat:** A heartbeat may improve inspection, but it is diagnostic only
   in v1. Process liveness and process-start identity, not heartbeat age, decide
   reclamation.

## 16. Verification commands

At minimum, implementation verification should include:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

The multi-process and PTY suites must be explicitly included in the reported
verification. A green single-process unit suite is insufficient evidence for
the core requirement.

## 17. Implementation evidence

The implementation is now in the runner/session boundary:

- `runners/process_lease.py` owns cross-platform short locks, atomic publish/
  replace, process identity, conservative liveness, and owner-safe removal.
- `runners/session_lease.py` owns the typed `.owner` record, its short-lived
  `.owner.lock` mutation guard, reference-counted
  same-process re-entry, stale recovery, latest-session selection, strict index
  validation, and the `session_already_active` exit contract.
- `tui_session.py`, `headless.py`, `background/worker.py`, and the session
  picker route attachment through the coordinator. Cleanup releases the outer
  lease after session resources and nested workflow claims are closed.
- `session_log.py` uses `index.lock` plus atomic index replacement, while
  session inspection and the picker expose redacted owner state without
  claiming a session.

Verification added in the repository includes unit coverage for schema,
identifier, liveness, PID reuse, zombie, cross-host, malformed-record,
reference-count, release-safety, replacement-race, index, and diagnostic behavior; real
multi-process coverage for the latest-session race and SIGKILL recovery; and
CLI E2E coverage for `--continue`, explicit `--resume`, and headless workflow
conflicts. The focused ownership suite passes with 18 tests. The broader suite
also passes the unaffected tests; the remaining full-gate failures in the
current environment are pre-existing checkout-path/permission and dependency
state failures (the test process resolves `/root/python_projects/...` while
the execution user can access the checkout only through its existing process
cwd, and the repository's committed type-audit baseline predates current
uncommitted source inventory). These are reported rather than hidden by
weakening the safety checks or changing unrelated baselines.
