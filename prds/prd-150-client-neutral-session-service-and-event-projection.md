---
title: "PRD-150: Client-Neutral Session Service and Multi-Client Event Projection"
status: Implemented
version: 0.1.0
created: 2026-07-25
related_prds:
  - PRD-121  # HTTP Server (lauren-framework)
  - PRD-138  # Repository Improvement Roadmap
  - PRD-139  # OpenCode-Inspired Product Expansion and Privacy-First Advertisements
  - PRD-141  # Background Sessions and Session Manager TUI
  - PRD-143  # Safe Commands During Active Runs
  - PRD-148  # Unified Interrupt and Graceful Cancellation
  - PRD-149  # Background Terminals and Responsive Wait Control
supersedes: []
tags:
  - sessions
  - clients
  - tui
  - headless
  - web
  - ide
  - events
  - api-boundary
  - architecture
---

# PRD-150 — Client-Neutral Session Service and Multi-Client Event Projection

Study date: 2026-07-25. This PRD addresses the OpenCode-inspired capability
of one session model usable by the TUI, CLI, web, and IDE clients. It defines
the product contract and migration boundary; it does not mandate a web
framework, transport, desktop shell, or replacement for `lauren-ai`.

## 1. Executive summary

agenthicc currently has one product concept—an agent session—but several
client-specific orchestration paths. The Rich TUI runs through `TUISession`
and the reactive conversation store. Headless execution has its own stdin and
JSON-lines lifecycle. CLI commands inspect journals, exports, jobs, and
configuration through separate paths. A future web or IDE client would need to
reconstruct session state from whichever path it happens to call.

That split creates a product and correctness risk:

- a session can have different lifecycle, cancellation, approval, workflow,
  and error semantics depending on its client;
- a reconnecting client cannot reliably resume from a typed event position;
- TUI-only presentation state can be mistaken for durable session state;
- each new client can accidentally create a second model/tool execution path;
- security policy, capability visibility, and redaction can drift between
  clients.

PRD-150 introduces a client-neutral session service and event projection. The
service owns session intents, lifecycle coordination, authoritative snapshots,
durable event positions, subscriptions, and client capabilities. The TUI,
headless runner, CLI inspect/export commands, web transport, and IDE adapter
become adapters over that service. They do not become independent runners or
state stores.

The first delivery is an in-process service with TUI, headless, and CLI parity.
The next delivery exposes the same typed contract through an explicitly
approved local transport for web and IDE clients. Network access remains
disabled by default, and the API decision recorded in PRD-138 remains a gate
before any remotely attachable server is shipped.

## 2. Evidence-backed problem statement

| Concern | Current implementation | Gap this PRD addresses |
|---|---|---|
| Durable domain state | Frozen kernel `AppState`, kernel events, pure reducer, processor, journals | No single client-neutral service façade owns session intents and projections. |
| Interactive orchestration | `src/agenthicc/runners/tui_session.py`, `tui/conversation_store.py`, and Rich workspace | TUI orchestration directly coordinates concerns that another client would need to duplicate. |
| Headless execution | `src/agenthicc/runners/headless.py` and workflow CLI paths | JSON-lines output is useful but is not defined as the same session/event contract as the TUI. |
| CLI inspection/export | Jobs, session logs, replay, and export commands | Read models and lifecycle controls are spread across command-specific access paths. |
| Background sessions | PRD-141 supervisor/store and manager TUI | Durable background lifecycle exists, but its client events and direct-turn events need one projection contract. |
| Background terminals | PRD-149 `TerminalManager` and terminal events | Terminal ownership, waits, cancellation, and output need consistent client visibility. |
| Interrupt/cancellation | PRD-148 spans processor, TUI, headless, workflows, and background workers | A client-neutral command must define idempotency and observable cancellation outcomes. |
| API/server boundary | PRD-138 P0.2 and PRD-121 describe competing or historical server directions | A transport must consume the service rather than become another agent runtime. |

The intended comparison is therefore not “add a web UI.” It is “make every
client observe and control the same session.”

## 3. Goals

1. Define one typed session model for creation, attachment, turns, workflows,
   approvals, questions, tool calls, background jobs, terminals, cancellation,
   completion, failure, resume, and archival.
2. Define one command/intent contract that all supported clients use to submit
   work and control a session.
3. Define one versioned event envelope with sequence positions, replay, live
   subscription, redaction, and client capability filtering.
4. Make the TUI, headless JSON-lines runner, and CLI inspect/export paths
   adapters over the service without changing their user-facing defaults.
5. Establish a transport-neutral contract that a local web client and IDE/ACP
   adapter can consume without importing TUI or Rich implementation details.
6. Preserve the kernel reducer, processor, journal, `lauren-ai` execution
   contract, workflow engine, capability checks, and existing persistence
   ownership as authoritative.
7. Make reconnect, concurrent clients, queued input, cancellation, approval,
   and partial failure deterministic and testable.
8. Keep local-first privacy and fail-closed authorization as defaults for every
   client and transport.

## 4. Non-goals

- Building a browser application, desktop application, or IDE plugin in the
  first implementation phase.
- Selecting a web framework, REST shape, WebSocket protocol, SSE library, or
  ACP version before the service contract and PRD-138 API decision are
  complete.
- Replacing `kernel.AppState`, `kernel.events`, `kernel.reducer`, or
  `kernel.processor` with a second domain store.
- Replacing `TUISession`, `headless.py`, or the workflow runners in one large
  rewrite. They migrate behind adapters incrementally.
- Introducing a second provider, agent, tool executor, workflow engine, or
  background-session implementation. `lauren-ai` and the existing registries
  remain canonical.
- Making local sessions remotely discoverable, shareable, or multi-tenant by
  default.
- Treating presentation-only Rich output, ANSI strings, spinner frames, or
  client-local input state as durable session events.

## 5. Product contract

### 5.1 Canonical session resource

The service exposes a client-neutral `SessionSnapshot` projection. The exact
Python module name is a design-phase decision, but it must live within the
existing session/kernel ownership boundary and must not use the historical
`src/agenthicc/api/` path.

The snapshot contains only stable, policy-filtered data:

```json
{
  "schema_version": 1,
  "session_id": "sess_01J...",
  "project_root": "/workspace/project",
  "created_at": "2026-07-25T12:00:00Z",
  "updated_at": "2026-07-25T12:04:10Z",
  "state": "waiting_approval",
  "active_turn_id": "turn_01J...",
  "workflow": {"name": "code_plan", "phase": "review"},
  "agent": {"name": "build", "model": "provider/model"},
  "queue": {"depth": 1, "accepting_input": true},
  "approvals_pending": 1,
  "questions_pending": 0,
  "background_jobs_running": 2,
  "terminals_running": 1,
  "last_event_sequence": 184,
  "capabilities": ["read", "write", "run", "approve"]
}
```

Requirements:

- `session_id` is stable across TUI, CLI, headless, web, and IDE attachment.
- `project_root`, tools, agent metadata, workflow metadata, and output are
  filtered through the requesting client's capability and workspace policy.
- The snapshot is a read model. Mutations happen only through service
  intents, kernel events, workflow commands, or the owning lifecycle service.
- Ephemeral client state—cursor position, selected overlay row, terminal
  width, prompt text not submitted, and Rich render fragments—is not part of
  the session snapshot.
- Background sessions and terminals are related resources with explicit owner
  IDs; they are not silently flattened into conversation text.

### 5.2 Client intents and commands

Every mutation enters through an explicit, typed command envelope:

```json
{
  "schema_version": 1,
  "command_id": "cmd_01J...",
  "idempotency_key": "client-123-turn-7",
  "client_id": "tui-local",
  "session_id": "sess_01J...",
  "kind": "submit_message",
  "expected_sequence": 184,
  "payload": {"text": "Run the tests", "workflow": null}
}
```

The initial command vocabulary is:

| Intent | Purpose | Idempotency/authorization requirement |
|---|---|---|
| `create_session` | Create a session with project, agent/profile, and policy inputs | Client receives a new owner-scoped session ID. |
| `attach_session` | Attach or reconnect a client to a session | Read/control capability is checked before events are returned. |
| `submit_message` | Queue or start a user turn | Idempotency key prevents duplicate turns after reconnect. |
| `invoke_command` | Submit a supported slash command | Immediate-control commands retain PRD-143 policy; mutating commands remain queued. |
| `approve`, `reject`, `answer` | Resolve an approval, question, or review | Must identify the pending item and expected sequence. |
| `cancel`, `interrupt` | Request graceful cancellation | Repeated requests are coalesced and emit one observable outcome. |
| `resume`, `retry`, `fork` | Continue durable work or create a child session | Parent/child lineage and authorization are persisted. |
| `subscribe`, `unsubscribe` | Manage an event projection stream | Subscription does not grant control capability. |
| `archive`, `delete` | Change retention or remove a session | Destructive operations require explicit capability and policy confirmation. |

Unknown commands, stale `expected_sequence` values, invalid state transitions,
and insufficient capabilities return structured errors. A client must never
mutate a snapshot by writing a field directly.

### 5.3 Versioned event envelope

All client-visible events use one envelope, regardless of whether the source is
the kernel processor, workflow engine, background supervisor, terminal manager,
or a projection adapter:

```json
{
  "schema_version": 1,
  "event_id": "evt_01J...",
  "sequence": 185,
  "session_id": "sess_01J...",
  "turn_id": "turn_01J...",
  "source": "kernel",
  "kind": "assistant_delta",
  "occurred_at": "2026-07-25T12:04:11Z",
  "durability": "durable",
  "visibility": "session",
  "payload": {"text": "The tests are running."}
}
```

Required event properties:

- `sequence` is monotonically increasing within one session and is the
  reconnect/replay cursor. Events from different sessions are not ordered by
  this field.
- `event_id` is unique and stable across replay. Replaying an event does not
  execute its side effect again.
- `kind` is a documented, additive enum. Renaming or changing payload meaning
  requires a schema version or compatibility adapter.
- `durability` distinguishes journal-backed domain events from ephemeral
  presentation updates. Durable events can be replayed; ephemeral events may
  be coalesced or dropped under backpressure.
- `visibility` and payload filtering are applied before delivery. Credentials,
  hidden tool arguments, private prompts, and unapproved project data never
  enter a client projection.
- `source` identifies the owning subsystem for diagnostics, not a second event
  authority.

The initial event families are:

| Family | Examples | Durability |
|---|---|---|
| Session lifecycle | `session_created`, `session_attached`, `session_archived`, `session_failed` | Durable |
| Turn/message | `turn_queued`, `user_message`, `assistant_delta`, `turn_completed` | Durable where journaled; deltas may be compacted with a final message |
| Tool/approval | `tool_started`, `tool_result`, `approval_requested`, `approval_resolved` | Durable audit event |
| Workflow/agent | `workflow_phase_changed`, `agent_started`, `agent_completed`, `todo_changed` | Durable |
| Background resources | `job_changed`, `terminal_changed`, `terminal_output` | Durable metadata; output is bounded and policy-filtered |
| Control/error | `cancel_requested`, `cancelled`, `retry_scheduled`, `projection_error` | Durable outcome; diagnostics are redacted |
| Presentation | `status_tick`, `typing`, `viewport_hint` | Ephemeral and client-specific |

### 5.4 Replay, subscription, and backpressure

The service provides two separate operations:

1. `snapshot(session_id)` returns the current policy-filtered read model.
2. `events(session_id, after_sequence, filters)` replays durable events and
   then follows new events.

An adapter reconnects by requesting a snapshot and replaying from its last
acknowledged sequence. It must tolerate duplicate delivery, missing
ephemeral events, and a server-side compaction boundary. If the requested
sequence has been compacted, the service returns a typed `replay_gap` response
with the earliest available sequence and a fresh snapshot requirement.

Each subscription has bounded memory and an explicit overflow policy:

- durable events are never silently discarded; the client receives a
  backpressure or disconnect error and can replay later;
- ephemeral presentation events may be coalesced or dropped;
- one slow web/IDE client cannot block the TUI, headless runner, kernel, or
  another subscriber;
- cancellation of a subscription does not cancel the session turn unless the
  client submits an explicit `cancel` intent.

### 5.5 Lifecycle and concurrency

The canonical session state machine is:

```text
created → idle → running → waiting_approval/question → running
                 │       ├→ background
                 │       ├→ completed
                 │       ├→ failed
                 │       └→ cancelled
                 └→ archived
```

The service must define and test:

- one active turn owner at a time unless the existing background-job contract
  explicitly permits concurrent child work;
- FIFO queue semantics for normal messages and immediate-control semantics for
  commands approved by PRD-143;
- explicit ownership for approvals, questions, background jobs, and terminal
  handles;
- a single cancellation owner consistent with PRD-148;
- deterministic conflict responses for two clients submitting commands against
  the same `expected_sequence`;
- safe resume after process restart using the existing journal and session
  persistence layers.

## 6. Client adapters

| Client | Adapter responsibility | Must not own |
|---|---|---|
| TUI | Map service snapshots/events into `conversation_store`, workspace components, overlays, and input policies | A second session lifecycle, journal, or tool executor |
| Headless JSON-lines | Map stdin commands and service events to stable JSON-lines; preserve exit codes and non-interactive approvals | Rich rendering, prompt UI, or a separate event taxonomy |
| CLI inspect/export | Read snapshots, replay durable events, export redacted session records, and issue explicit lifecycle commands | Ad-hoc direct writes to journals or hidden session mutation |
| Local web client | Consume the approved local transport; render snapshots/events and submit intents | Direct imports of TUI state or server-side agent orchestration |
| IDE/ACP adapter | Translate session events, diagnostics, approvals, and file context into the approved IDE protocol | Bypassing workspace/capability policy or creating IDE-specific sessions |

The first migration must keep the existing commands usable. A client adapter
may have a richer or poorer presentation, but it cannot change the meaning of
an approval, cancellation, tool result, workflow transition, or session
completion event.

## 7. User journeys

### 7.1 TUI turn and reconnect

1. The TUI creates or attaches to `session_id=S` through the service.
2. The user submits a message. The TUI sends `submit_message` with an
   idempotency key and immediately renders the returned event stream.
3. Tool calls, workflow phases, approvals, background jobs, and terminal waits
   arrive as typed events. The TUI maps them to its existing reactive state.
4. If the TUI restarts, it attaches to `S`, reads a snapshot, and replays from
   its last sequence without repeating the turn or tool side effects.

### 7.2 Headless automation

1. The caller provides an optional session ID and receives a stable session
   identity in the first JSON-line response.
2. Each stdin request becomes a service intent; each output line is a versioned
   event or structured command result.
3. A CI process can resume or retry a session using the same idempotency and
   event cursor rules as the TUI.
4. Interactive-only events are represented as explicit `approval_required`,
   `question_required`, or `unsupported_interaction` results rather than
   blocking on terminal UI input.

### 7.3 CLI inspection and control

1. `agenthicc session show <id>` reads the service snapshot.
2. `agenthicc session events <id> --after <sequence>` replays the same durable
   event projection used by clients.
3. `agenthicc session export <id>` emits a redacted, schema-versioned export.
4. Control commands such as resume, cancel, retry, or archive submit typed
   intents and show the resulting event sequence.

### 7.4 Web or IDE attachment

1. The client connects only through an explicitly enabled local or authorized
   transport and proves its client/session capability.
2. It obtains a snapshot and event cursor, then subscribes without owning the
   turn unless control was granted.
3. It can submit approved intents such as message, approval, cancel, or
   context request. The service applies the same workflow, tool, workspace,
   and security rules as the TUI.
4. On disconnect, the session and turn continue according to session policy;
   reconnect replays from the last durable sequence.

## 8. Architecture and ownership boundary

The target flow is:

```text
client adapter
    │  typed intent / snapshot / event subscription
    ▼
client-neutral session service
    │  commands and projections
    ▼
kernel processor + reducer + journal
    │
    ├── workflow engine and agent runners
    ├── background supervisor / terminal manager
    ├── capability, workspace, network, and approval policy
    └── memory and session durability
```

Rules:

1. The kernel remains authoritative for durable domain state. New service
   operations dispatch events or invoke the owning lifecycle boundary; they do
   not mutate frozen state or append competing journal formats.
2. The service translates domain events into a documented neutral projection.
   It does not make the TUI reactive store, Rich output, or JSON-lines writer
   authoritative.
3. `lauren-ai` remains the only model/tool execution contract. The service
   coordinates turns and observes results; it does not implement a second
   provider loop.
4. `SessionContext` remains the construction boundary for session-scoped
   services. TUI, headless, and future transports receive the same service
   dependencies through that boundary.
5. Workflows, background sessions, and terminals retain their current owning
   modules. The projection links them by IDs and lifecycle events rather than
   copying their state into an unbounded session object.
6. Client capability checks occur before snapshot fields, replay payloads, or
   control intents are exposed. Redaction is part of projection, not a UI-only
   formatting step.

## 9. Security, privacy, and reliability

### 9.1 Local-first access

- In-process TUI, headless, and CLI adapters are the default deployment.
- No listening socket, mDNS advertisement, browser origin, or remote attach is
  enabled by installing or upgrading agenthicc.
- Any non-local transport requires explicit configuration, authentication,
  exact origin/CORS policy, connection limits, and an audit event.
- Session IDs are opaque capabilities only when combined with an authorized
  client identity; guessing an ID must not grant access.

### 9.2 Data minimization

- Event projections redact credentials, OAuth tokens, API keys, hidden tool
  arguments, private system prompts, and data outside the client's workspace
  capability.
- Session export is an explicit operation with a redaction policy and a clear
  warning that durable user content is leaving the local process.
- Ads, telemetry, model-provider metadata, and account identity never enter a
  session event or model context unless a separate, explicit feature contract
  permits it.
- Event retention and replay compaction follow the existing storage and
  background-session policies; a live subscription is not a retention bypass.

### 9.3 Reliability

- Commands are idempotent where they can cause side effects; tool execution is
  never repeated merely because a client reconnects or an event is replayed.
- A projection failure is observable and isolated from the domain processor;
  one client cannot stop a session or another client from receiving events.
- Cancellation, timeout, failure, and successful completion remain distinct
  outcomes in the event stream, consistent with PRD-148.
- Every long-lived subscription and queued command has bounded resources,
  timeout behavior, and a diagnostic path.

## 10. Delivery phases

### Phase 0 — Contract and boundary decision (`P0`)

- Inventory current TUI, headless, CLI, workflow, background, terminal, and
  journal events.
- Decide the canonical session service ownership location without using the
  historical `src/agenthicc/api/` path.
- Define `SessionSnapshot`, command envelopes, event envelopes, lifecycle
  states, schema/version policy, error codes, capability filters, and replay
  semantics.
- Resolve overlap with PRD-121 and record the PRD-138 headless/API boundary
  decision before selecting a network transport.

### Phase 1 — In-process session service (`P0`)

- Implement the service façade over the existing kernel processor, reducer,
  persistence, and session construction boundaries.
- Add snapshot reads, typed intents, durable sequence cursors, subscriptions,
  replay-gap handling, idempotency, and structured errors.
- Link workflow, approval, question, background-session, and terminal resources
  by stable IDs.

### Phase 2 — Adapter parity (`P0`)

- Migrate `TUISession` and the reactive bridge to consume service snapshots and
  event projections.
- Migrate headless JSON-lines to the same command and event schema while
  preserving documented stdin compatibility and exit codes.
- Migrate CLI session inspect/export/control paths to service read models and
  intents.
- Add parity fixtures proving that the same scripted session produces
  equivalent durable events through TUI, headless, and CLI paths.

### Phase 3 — Local attachment transport (`P1`)

- Implement the approved local transport only after PRD-138's API decision.
- Support snapshot, replay, live subscription, typed commands, health,
  graceful shutdown, authentication, exact origins, and bounded connections.
- Keep the transport as an adapter over the in-process service. It must not
  instantiate a second agent runner or bypass the kernel journal.

### Phase 4 — Web and IDE clients (`P1`)

- Build a minimal web client or reference client only after the transport and
  security contract are stable.
- Add an IDE/ACP adapter for session attach, streaming events, approvals,
  diagnostics, file context, cancellation, and reconnect.
- Test a TUI-attached and IDE/web-attached client observing the same session,
  including concurrent read-only subscriptions and an authorized control.

### Phase 5 — Migration and hardening (`P0/P1`)

- Add compatibility adapters for existing journals and headless records.
- Gate old client paths behind diagnostics, then remove duplicate orchestration
  only after parity and rollback checks pass.
- Document schema evolution, retention, export, troubleshooting, and client SDK
  compatibility.

## 11. Acceptance criteria

| ID | Criterion |
|---|---|
| 150.1 | A session created by the TUI can be identified, inspected, resumed, and controlled by the headless and CLI adapters using the same `session_id`. |
| 150.2 | TUI, headless, CLI, web-transport, and IDE-adapter paths use one command envelope and one versioned event envelope; no client invents a parallel lifecycle taxonomy. |
| 150.3 | Durable session state remains owned by the kernel processor/reducer/journal and is not duplicated in a client-specific store. |
| 150.4 | The service exposes a policy-filtered `SessionSnapshot` that excludes client-local cursor/render/input state and redacts unauthorized fields. |
| 150.5 | Events have stable IDs, per-session sequence numbers, schema versions, durability/visibility metadata, and documented payloads. |
| 150.6 | A client can reconnect from `after_sequence`, receive all available durable events exactly once in effect, and receive a typed replay-gap response after compaction. |
| 150.7 | Duplicate command delivery with the same idempotency key does not repeat a turn, tool side effect, approval resolution, cancellation, retry, or archive operation. |
| 150.8 | Two clients can observe one running session without blocking each other; concurrent conflicting commands receive deterministic authorization or stale-sequence errors. |
| 150.9 | TUI rendering, headless JSON-lines, and CLI inspect/export preserve their existing supported behavior while consuming the service projection. |
| 150.10 | Workflow phases, approvals, questions, background jobs, background terminals, cancellation, timeout, failure, retry, resume, and completion are observable through the same session event contract. |
| 150.11 | A slow or disconnected subscriber cannot block the kernel, agent turn, TUI, headless runner, or another subscriber; durable events are replayable after overflow. |
| 150.12 | No server socket or remote session access is enabled by default; non-local attachment requires explicit configuration, authentication, origin policy, and auditability. |
| 150.13 | Web and IDE/ACP adapters can attach, snapshot, stream, reconnect, submit authorized intents, and detach without importing TUI/Rich state or creating a second agent runtime. |
| 150.14 | Session export and event projection tests prove that secrets, hidden prompts, unauthorized paths, and unrelated sessions are not exposed. |
| 150.15 | Documentation identifies the canonical service, event schema, adapter responsibilities, compatibility guarantees, retention policy, and transport status. |

## 12. Verification plan

Required implementation checks include:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
```

The PRD implementation must add coverage for:

- service commands, invalid transitions, stale sequence conflicts, and
  idempotency replay;
- snapshot filtering, event schema validation, event ordering, durable versus
  ephemeral projection, and replay-gap recovery;
- TUI/headless/CLI parity for a scripted turn containing tools, workflow
  phases, approval, question, cancellation, background work, and completion;
- concurrent subscribers, bounded queues, slow-client isolation, disconnect,
  reconnect, duplicate delivery, and graceful shutdown;
- session restart and journal recovery without repeating tool side effects;
- workspace, capability, plugin trust, network, secret redaction, export, and
  cross-session isolation boundaries;
- local transport authentication/origin policy and an IDE/web adapter contract
  test once those phases are enabled.

## 13. Rollout and compatibility

1. Ship the service contract and in-process adapter behind an internal
   compatibility layer while the existing TUI and headless paths remain the
   default.
2. Run parity fixtures in CI for existing session, workflow, background, and
   headless scenarios before changing the default construction path.
3. Persist schema versions and event positions; provide a read-only migration
   adapter for older journals before enabling new control intents.
4. Enable local attachment only through an explicit configuration switch and
   print its bind/auth/origin policy at startup.
5. Keep rollback possible by retaining the old adapter for one release after
   parity is established. Rollback must not duplicate tool calls or discard
   already persisted events.
6. Do not advertise web or IDE support until their adapter and security
   acceptance criteria are complete.

## 14. Risks and decisions

| Risk | Mitigation |
|---|---|
| A third state model competes with kernel `AppState` | Make the service a façade/projection over kernel events and add architecture tests that reject direct client mutation. |
| TUI and headless parity hides client-specific semantics | Use scripted parity fixtures and a shared event schema before migrating defaults. |
| Replayed events repeat side effects | Separate command execution from event replay; require idempotency ledgers for side-effecting commands. |
| One client leaks another session or project | Bind client identity, session ownership, workspace policy, capability filtering, and redaction to every snapshot/event request. |
| Slow remote clients exhaust memory | Bounded per-client queues, durable replay cursors, ephemeral-event coalescing, and disconnect-on-overflow. |
| Web/IDE transport becomes a second agent runtime | Transport code may call only the session service; agent construction remains in the existing session boundary. |
| Protocol changes break CLI scripts or IDE adapters | Version envelopes additively, publish compatibility windows, and test unknown-field/unknown-event handling. |
| Background jobs and terminals become inconsistent | Project them by owner/job/terminal IDs and reuse PRD-141, PRD-148, and PRD-149 lifecycle contracts. |
| Local API is accidentally exposed | Bind locally by default, require explicit non-local configuration, authenticate every request, and test startup policy. |

Decisions required before Phase 3:

- Which local transport is supported after the PRD-138 API boundary decision?
- Is IDE integration implemented through ACP, a native protocol, or a thin
  adapter over the approved local transport?
- Which durable event families retain full payloads, and which compact into a
  final message or bounded artifact?
- What compatibility window is required for headless JSON-lines consumers?

## 15. Related documentation and implementation map

The implementation must update the following artifacts in the same change:

- `README.md` and a client/session guide for supported client behavior;
- `docs/guides/architecture.md` for the service and state boundary;
- `docs/reference/storage.md` for event retention, replay cursors, and export;
- `docs/reference/cli.md` for session inspect/replay/control commands;
- `llms-full.txt` and `llms.txt` for public service and event symbols;
- PRD-138 and PRD-139 status/roadmap links when the API decision or product
  phases change.

Candidate implementation surfaces, subject to the Phase 0 boundary decision,
are:

| Surface | Responsibility |
|---|---|
| `src/agenthicc/kernel/events.py` / `reducer.py` / `processor.py` | Authoritative domain events, reduction, persistence, and event positions |
| `src/agenthicc/runners/session_context.py` | Construct and inject the session service boundary |
| `src/agenthicc/runners/tui_session.py` | TUI adapter and compatibility bridge |
| `src/agenthicc/runners/headless.py` | JSON-lines adapter and non-interactive policy |
| `src/agenthicc/commands/` | CLI/session command adapters through the canonical registry |
| `src/agenthicc/background/` | Background job/terminal lifecycle projections, not a duplicate session store |
| `src/agenthicc/tui/conversation_store.py` and `tui/workspace/` | Reactive presentation projection only |
| New session-service package selected in Phase 0 | Client-neutral snapshots, intents, subscriptions, and projections |

## 16. Implementation evidence

Implemented in the existing ownership boundaries:

- `src/agenthicc/session_service/` contains the typed snapshot, command,
  event, fsync-backed JSONL store, replay/compaction, idempotency, capability
  projection, bounded subscriptions, in-process clients, and the explicit
  loopback-first HTTP/SSE adapter.
- `EventProcessor.subscribe_events()` exposes applied kernel events to the
  projection without making the service a second reducer or journal owner.
- `SessionContext`, `TUISession`, the headless runner, and the canonical
  `agenthicc session ...` CLI group use the service identity/projection while
  retaining their existing execution and presentation owners.
- `HttpSessionClient`, `WebSessionAdapter`, and `IdeSessionAdapter` provide a
  transport-neutral reference client contract without shipping a browser or
  IDE runtime.
- Coverage is in `tests/unit/test_session_service.py`,
  `tests/integration/test_session_service_integration.py`, and
  `tests/e2e/test_session_transport_e2e.py`, alongside existing TUI/headless
  regression tests.

The selected Phase 3 transport is aiohttp over loopback HTTP/SSE. Remote
binding requires an explicit bearer token; browser origins remain closed by
default. The service is an adapter over the kernel, workflow, background, and
terminal owners and does not create a second agent runtime.
