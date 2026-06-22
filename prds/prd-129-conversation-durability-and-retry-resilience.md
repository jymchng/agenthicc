# PRD-129 — Conversation Durability & Retry Resilience

**Status:** Analysis + proposal (investigation complete; implementation phased)
**Scope:** agenthicc (`runners/`, `tui/`, `memory/`, `kernel/`) + lauren-ai (`_agents/_runner.py`, `_memory/`, `_transport/`)

---

## 0. Executive summary

A user-reported symptom — *"after a network retry succeeds, prior conversation
context is lost; the agent acts as though it started fresh"* — was investigated
end-to-end across both codebases.

**Headline finding:** the existing PRD-126 snapshot/restore retry is **correct**
for preserving *prior* conversation history — it deep-copies memory before each
attempt and restores it on a transient error. The real defects are
*architectural*, not a broken restore:

1. **Retry granularity is the whole agent turn.** A turn is a multi-step loop
   (reason → tool calls → tool results → reason → …). On a late-turn timeout the
   retry rolls the **entire** turn back to its pre-turn snapshot and re-runs it
   from scratch. All intra-turn progress — assistant messages, tool calls, and
   tool results from already-completed steps — is **discarded and recomputed**.
   To the user this looks like the agent "restarting the task" / "forgetting
   what it was doing."
2. **Tool execution is re-run on retry (idempotency violation).** Because the
   rollback discards completed tool results, the retried turn **re-executes the
   same tools** — including side-effecting ones (`write_file`, `run_bash`,
   `git_commit`). Duplicate side effects are possible.
3. **No durability during a turn.** Conversation memory is an in-process object;
   it is persisted to SQLite **only at the turn boundary** (`run_turn`'s
   `finally`). A process crash mid-turn loses the entire in-flight turn.
4. **Streaming partials are discarded.** A mid-stream timeout raises before the
   assistant message is committed, so partial generations are thrown away.
5. **Retry layers compound.** Up to four independent retry mechanisms stack
   (SDK → transport → agent-turn → phase-loop), multiplying attempts and
   re-execution.

The fix is to move from *retry-and-rollback at the turn boundary* to a **durable,
append-only conversation journal with idempotent, resumable step execution.**

---

## 1. Findings

### 1.1 Current architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ TUI session (runners/tui_session.py)                                       │
│  • session_id = uuid4 (stable; reused on --resume)        [:117]           │
│  • session_memory = ShortTermMemory(32k)  — ONE per session [:264]         │
│  • ConversationStore (tui/) — in-memory reactive Signals (UI transcript)   │
│  • EventProcessor(persist=True) → events.jsonl (kernel domain events only) │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ run_turn(text)                       [tui_session.py:592]
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ AgentTurnRunner (runners/agent_turn.py)                                    │
│  • new intent_id/agent_id per turn (uuid4)                [:223]           │
│  • compaction (pre-snapshot, failure-safe)                [:494-504]       │
│  • _stream_with_retry(_stream_once)  ── PRD-126 retry ──   [:563,593]      │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ run_with_transport_retry(turn_fn, memory=session_memory)
                ▼                                      [runners/retry.py:67]
┌──────────────────────────────────────────────────────────────────────────┐
│ snapshot = memory.snapshot()  (deepcopy)  ← per attempt   [retry.py:102]   │
│ try: await turn_fn()                                                       │
│ except transient: memory.restore(snapshot); reset_fns; backoff; retry      │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ turn_fn = _stream_once → active_runner.run_stream(
                │   memory=session_memory, NO conversation_id, NO store)
                ▼                                   [agent_turn.py:521-533]
┌──────────────────────────────────────────────────────────────────────────┐
│ lauren-ai AgentRunnerBase.run_stream (async generator)  [_runner.py:764]   │
│  memory.add_user(text)                                    [:888]           │
│  while turn < max_turns:                                                    │
│     async for chunk in transport.complete(stream=True): accumulate…        │
│     # stream fully complete →                                              │
│     memory.add_assistant(synthetic_completion)            [:1197] ◀ COMMIT │
│     tool results = execute_tools(...)                      [:1248]         │
│     memory.add_tool_results(results)                      [:1265]         │
│  (save to store ONLY if conversation_id+store — NOT set)  [:1302]         │
└───────────────┬──────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ lauren-ai transport (_transport/_anthropic.py)                             │
│  • _complete_sync: retry loop on TransientTransportError                   │
│  • _stream: NO retry — classify + re-raise                                 │
│  • ReadTimeout → httpx → APIConnectionError → TransientTransportError      │
└────────────────────────────────────────────────────────────────────────────┘
```

**Entity model**

| Entity | Where | Lifetime | Durable? |
|---|---|---|---|
| **Session** | `session_id` (uuid4) | whole TUI process; reused on resume | yes — registry + event log |
| **Conversation (LLM history)** | `ShortTermMemory._messages` | one object per session, mutated across turns | **in-process**; SQLite snapshot only at turn end |
| **UI transcript** | `tui/conversation_store.py` Signals | in-process | no (rebuildable from session event log) |
| **Run / turn** | `intent_id`/`agent_id` (uuid4 per turn) | one user message → one `run_turn` | kernel events only (`IntentCreated`/`IntentStatusChanged`) |
| **Agent sub-turn** | `_turn` inside `run_stream` loop | one LLM round-trip | not individually persisted |
| **Tool execution** | `_execute_tools` | within a sub-turn | result lives only in `_messages` |
| **Provider request** | `transport.complete` | one HTTP call | none |

### 1.2 Retry boundaries — there are FOUR, and they stack

| # | Layer | Location | Unit retried | Streaming? | State handling |
|---|---|---|---|---|---|
| 1 | **Provider SDK** | Anthropic/OpenAI SDK `max_retries` (`config.py llm_sdk_max_retries=2`) | one HTTP request | partial (SDK rarely retries mid-stream) | none — same bytes |
| 2 | **Transport** | `_anthropic.py _complete_sync` / `_call_with_retry`, backoff `(2^n)*0.5` | one non-stream `complete` | **no** (`_stream` re-raises) | same `messages` |
| 3 | **Agent turn** | `run_with_transport_retry` via `_stream_with_retry` (`max_retries=3`) | **the entire streaming turn** | yes | **snapshot/restore of `session_memory`** |
| 4 | **Phase loop** | workflow runner; transient swallowed at `agent_turn.py:582-583` → "phase loop re-runs the whole turn" | the whole turn again | yes | reuses restored `session_memory` |

For the **streaming TUI path** (the reported scenario), layer 2 does **not**
apply, so layer 3 is primary and layer 4 re-runs on top of it. Worst case the
attempt count is `transport_max_retries (3) × phase retries`, matching the
field observation of ~30–40 attempts.

### 1.3 Where conversation state lives & when it persists

- **Live history:** `ShortTermMemory._messages` — a `list[dict]`, mutated
  in-process. `snapshot()` deep-copies (`_memory/__init__.py:892`); `restore()`
  replaces the list (`:897-914`); `messages()` returns a trimmed copy with hard
  guards — *never trims past the last conversational user message and never
  yields an empty list* (`:789-816`). These guards mean a normal turn **cannot**
  silently lose all history through trimming.
- **Durable snapshot:** `conversation_store.py` (SQLite) — `memory_snapshots`
  table. Written **only** in `run_turn`'s `finally` (`tui_session.py:684-690`),
  i.e. **once per turn, at the boundary**. Loaded on resume (`:365-372`).
- **Kernel event log** (`events.jsonl`, `persist=True`): records **kernel
  domain events** (`IntentCreated`, `IntentStatusChanged`,
  `TransportRetryScheduled`) — **not** LLM messages. A conversation **cannot** be
  reconstructed from it.
- **UI transcript** (`tui/conversation_store.py`): in-memory reactive `Signal`s
  only; rebuilt from the per-session conversation event log on resume.
- **3-tier memory** (`memory/layers.py`): agent KV + artifacts. **Not** LLM
  history.

**Net:** the authoritative conversation lives in one in-process object that is
checkpointed to disk only at turn boundaries. There is no message-level journal
and no per-step durability.

### 1.4 Failure scenario — traced end-to-end

A long turn: user asks for a multi-file refactor. The agent loop runs several
LLM round-trips with tool calls. The **second** LLM round-trip times out.

```
1. User message              → memory: [..H.., user]            (add_user :888)
2. Sub-turn A: assistant + 3 tool_use   → memory.add_assistant(A)   (:1197)
3.            execute_tools(A): write_file, run_bash (SIDE EFFECTS APPLIED)
              → memory.add_tool_results(A)                          (:1265)
   memory now: [..H.., user, A_assistant, A_tool_results]
4. Sub-turn B: transport.complete(stream=True) …
5. ReadTimeout MID-STREAM
      → httpx → APIConnectionError → TransientTransportError
      → raised out of `async for chunk in stream` (BEFORE add_assistant B)
      → propagates out of run_stream  (B partial text discarded)
6. Outer retry (retry.py:111): _is_transient → restore(snapshot_0)
      snapshot_0 was taken at attempt start = [..H.., ] (PRE-turn, no user msg)
      → memory RESET to [..H..]  — A_assistant, A_tool_results, user msg GONE
7. Retry re-runs the WHOLE turn from [..H..]:
      re-add user → Sub-turn A AGAIN → write_file & run_bash RUN A SECOND TIME
      → Sub-turn B → success
```

**Where state disappears:** step 6. The restore target is the **pre-turn**
snapshot, so every in-turn step (A's assistant message *and its tool results*,
plus the user message) is erased. Prior history `..H..` survives, but the
**within-turn working context is lost and the side-effecting tools re-run**
(step 7). For a single-round turn this is invisible; for a long multi-step turn
the agent visibly "starts the task over."

### 1.5 Root cause analysis

Mapping to the candidate causes in the brief:

| Candidate | Verdict | Evidence |
|---|---|---|
| **Retries occur too high in the stack** | **PRIMARY** | Retry wraps the *entire* streaming turn (`agent_turn.py:563`), so rollback erases all intra-turn steps and re-executes tools. |
| **Ephemeral in-memory state** | **YES** | `ShortTermMemory` is in-process; ConversationStore is Signals only. |
| **Delayed persistence** | **YES** | SQLite snapshot only in `run_turn` `finally` (`:684-690`) — turn granularity. |
| **Recreated sessions / conversations** | **NO** | `session_id` stable; `session_memory` is one object reused across turns; restore is correct (no fresh session on retry). |
| **Provider abstraction limitations** | **CONTRIBUTING** | Streaming transport has no retry and no resumability; partials discarded. |
| **Streaming behaviour** | **CONTRIBUTING** | `add_assistant` only after full stream (`:1197`); mid-stream timeout discards partial. |
| **Missing checkpoints** | **YES** | No per-step / per-message checkpoint; only whole-turn snapshots. |
| **Missing idempotency** | **YES** | Tools re-execute on retry; no idempotency keys / dedup. |

**One-sentence root cause:** *Retry and durability are both anchored at the
whole-agent-turn boundary using an in-process snapshot, so a transient failure
in any sub-step discards all completed sub-steps (re-executing their tools) and
nothing is recoverable after a crash — there is no message-level journal,
idempotent tool ledger, or resumable step execution.*

### 1.6 Evidence index

| Claim | File:line |
|---|---|
| Outer retry snapshot/restore per attempt | `agenthicc/src/agenthicc/runners/retry.py:101-117` |
| `_stream_once` calls `run_stream(memory=session_memory)`, no `conversation_id`/store | `agenthicc/.../runners/agent_turn.py:521-533` |
| Transient swallowed → phase loop re-runs whole turn | `.../agent_turn.py:582-583` |
| Compaction runs pre-snapshot, failure-safe | `.../agent_turn.py:494-504`, `memory/compactor.py:83-114` |
| `session_memory` created once per session (32k) | `.../runners/tui_session.py:264` |
| Snapshot persisted only in `run_turn` finally | `.../runners/tui_session.py:684-690` |
| Resume restores last snapshot | `.../runners/tui_session.py:365-372` |
| `add_assistant` only after full stream; `ensure_valid` on interrupt | `lauren-ai/.../_agents/_runner.py:1196-1273` |
| Tool execution + `add_tool_results` inside loop | `.../_runner.py:1244-1266` |
| Store save only if `conversation_id`+store (unset in TUI) | `.../_runner.py:1302-1303` |
| `snapshot()` deepcopy / `restore()` / `messages()` guards | `lauren-ai/.../_memory/__init__.py:789-914` |
| Streaming transport has no retry loop | `lauren-ai/.../_transport/_anthropic.py` (`_stream`) |
| Kernel log persists domain events only | `agenthicc/.../kernel/processor.py` |

---

## 2. Design requirements

| Requirement | Concrete acceptance |
|---|---|
| **Durability** | Every message/tool transition survives network failure, provider timeout, **process crash**, retry, and tool failure — recoverable from disk. |
| **Idempotency** | A retry never duplicates a committed message, never re-executes a tool whose result is already durably recorded, and never duplicates external side effects. |
| **Recoverability** | After any failure the system reconstructs full history, assistant messages, tool calls, tool results, and any *pending* operation, then continues. |
| **Provider independence** | The durable representation is provider-neutral (works for Anthropic, OpenAI, Gemini, local); provider adapters map to/from it. |
| **Streaming support** | Partial generations are checkpointed; an interrupted stream resumes or cleanly finalizes the partial assistant turn without corrupting history. |

---

## 3. Evaluation of design options

| Option | What it is | Durability | Idempotency | Recovery from crash | Complexity | Verdict |
|---|---|---|---|---|---|---|
| **A. Retry only provider calls** | Retry the single HTTP/stream request at the transport, below the turn loop | Low | N/A (no rollback) | None | **Low** | Necessary but **insufficient** — already partly present; doesn't survive crashes or fix re-execution. **Keep as Phase 1.** |
| **B. Durable conversation journal** | Append every transition (user/assistant/tool_call/tool_result) to a durable log; memory is a projection | High | High (with keys) | High | Medium | **Core of the recommendation.** |
| **C. Event sourcing** | Conversation = append-only event stream; state = fold | High | High | High (replay) | Medium-High | **Adopt — agenthicc already has the kernel event-sourcing substrate; extend it to message events.** |
| **D. Checkpoint-based execution** | Persist execution checkpoints during long runs | High | Medium | High | Medium | **Adopt for step-level resume** atop B/C. |
| **E. Run resumption** | Resume an interrupted run from the last successful step | High | High | High | High | **Target end-state (Phase 3-4)**, enabled by B+C+D. |

**Recommendation:** **B+C as the durable substrate, D for step checkpoints, E as
the resumable execution model**, with **A** as the immediate stop-gap. These are
complementary layers, not alternatives.

The decisive factor: agenthicc **already is an event-sourced system** (immutable
`AppState`, append-only `events.jsonl`, pure `root_reducer`). The conversation is
the one domain *not* yet modeled as events. Option C is therefore the
lowest-friction path to B's durability — we extend the existing kernel rather
than bolt on a parallel store.

---

## 4. Proposed architecture

### 4.1 Principle: the journal is the source of truth; memory is a projection

Replace "in-process `ShortTermMemory` checkpointed at turn end" with:

```
   append-only Conversation Journal (durable)  ──fold──►  ShortTermMemory (projection, in-RAM)
            ▲                                                    │
            │ every transition appended + fsynced                │ used for the next LLM call
            │                                                    ▼
   ToolLedger (idempotency keys, durable)  ◄────────────  tool execution
```

Every state transition is appended to the journal **before** it takes effect in
the projection; the projection (`ShortTermMemory`) is rebuilt by folding the
journal. Retry/crash recovery = re-fold from the journal.

### 4.2 Required components

1. **`ConversationJournal`** (new, agenthicc `runners/journal.py` or kernel
   event types) — append-only, fsync'd, one log per `session_id`. Entry kinds:
   `UserMessageAppended`, `AssistantMessageCommitted`,
   `ToolCallRequested`, `ToolResultRecorded`, `TurnStarted`, `TurnCompleted`,
   `CompactionApplied`. Each entry carries a monotonic `seq`, `turn_id`,
   `step_id`, and a content hash. **Implementation: new kernel event types +
   reducer handlers**, reusing `EventProcessor` persistence and replay
   (`restore_from_log`) — this is Option C realized on the existing substrate.

2. **`ToolLedger`** (new) — durable map `idempotency_key → ToolResult`, where
   `idempotency_key = hash(turn_id, step_id, tool_name, canonical(args))`.
   `execute_tools` consults the ledger first; a hit returns the recorded result
   without re-running the tool. This makes retries idempotent **even for
   side-effecting tools**, the single most important correctness gain.

3. **`MemoryProjector`** — folds the journal into a `ShortTermMemory`. Replaces
   ad-hoc `snapshot()/restore()` as the rollback mechanism: "rollback" becomes
   "re-fold journal up to the last committed `seq`," which is inherently correct
   and crash-safe.

4. **Step-scoped retry** — move the retry boundary **down** from the whole turn
   to the **single LLM round-trip** (sub-turn). On a transient error, retry only
   the failed round-trip; already-committed sub-turns and their tool results
   (now in the journal + ledger) are **kept**, not rolled back. This directly
   removes the "restart the task / re-execute tools" behavior.

5. **Streaming checkpointer** — accumulate stream deltas into a
   `PartialAssistantBuffer` that is periodically appended to the journal as an
   `AssistantDeltaAppended` entry. On mid-stream interruption, the partial is
   either (a) finalized as a complete assistant turn if it already contains a
   stop reason, or (b) recorded as `AssistantPartialAbandoned` and cleanly
   superseded on retry — never silently lost.

6. **`RunCoordinator`** (resumable execution, Phase 3-4) — owns the turn/sub-turn
   state machine; on startup, reads the journal, finds the last incomplete
   `TurnStarted` with no `TurnCompleted`, and resumes from the last committed
   `step_id` instead of restarting.

### 4.3 Sequence — durable, idempotent retry (target state)

```
User msg
  │ append UserMessageAppended(seq=n, turn=T)            [fsync]
  ▼
Sub-turn A:
  stream → append AssistantMessageCommitted(step=A)      [fsync]
  for each tool call:
     key = hash(T, A, name, args)
     ledger.get(key)?  ── miss ──► execute ──► ledger.put(key,result) [fsync]
                       └─ hit ───► reuse result (NO re-execution)
     append ToolResultRecorded(step=A, key)              [fsync]
  ▼
Sub-turn B:
  stream … ReadTimeout
     → retry THIS round-trip only (step-scoped)
     → journal/ledger for A untouched ► A NOT replayed, tools NOT re-run
     → B succeeds → append AssistantMessageCommitted(step=B)
  ▼
TurnCompleted(turn=T)                                    [fsync]
```

Compare to today: the timeout in B no longer erases A or re-runs A's tools.

### 4.4 Sequence — crash recovery

```
Process killed mid-turn (after A committed, during B)
  ▼ restart, --resume <session>
RunCoordinator.replay(journal):
  fold entries → ShortTermMemory = [..H.., user, A_assistant, A_tool_results]
  detect TurnStarted(T) with no TurnCompleted(T) → RESUME at sub-turn B
  (or, if mid-tool, ToolLedger shows which tools already ran → skip them)
```

Today this is impossible — the in-flight turn is gone; only the previous turn's
snapshot exists.

---

## 5. Failure scenarios — behavior after the design

| Scenario | Today | Proposed |
|---|---|---|
| **Provider/read timeout (single round-trip)** | retry whole turn; partial discarded | retry that round-trip; prior steps kept |
| **Timeout late in a multi-step turn** | **roll back whole turn; re-run all tools** | keep committed steps + ledger; retry only failed step |
| **Process crash mid-turn** | in-flight turn lost; resume from previous turn | replay journal; resume from last committed step |
| **Tool crashes / throws** | result is an error block in memory; on turn retry the tool re-runs | ledger records terminal failure; retry consults ledger, no blind re-run |
| **Duplicate retry / compounding layers** | up to ~30–40 attempts; each re-runs tools | idempotency keys dedup; single bounded step-retry; layers collapse |
| **Mid-stream interruption** | partial assistant text silently dropped | partial journaled; finalized or cleanly superseded |
| **Compaction during instability** | summarize-then-replace (already safe) | unchanged + journaled as `CompactionApplied` (replayable) |

---

## 6. Migration plan (incremental, no big-bang)

The system stays shippable at every step; each phase is independently valuable.

1. **Introduce the journal as a shadow writer first.** Add the journal/event
   types and write to them alongside the existing `session_memory`, but keep
   `session_memory` authoritative. Verify the fold reproduces `session_memory`
   byte-for-byte in tests (parity gate) before flipping authority.
2. **Flip authority to the projection.** Once parity holds, `ShortTermMemory`
   becomes a fold of the journal; `snapshot()/restore()` are reimplemented as
   `seq`-bounded re-folds. Delete the turn-boundary SQLite snapshot once the
   journal subsumes it (no dual write path — per repo convention).
3. **Add the `ToolLedger`** and route `_execute_tools` through it. This is the
   highest-ROI correctness change and can land independently of full resumption.
4. **Lower the retry boundary** from `_stream_with_retry` (whole turn) to
   per-round-trip, with the journal/ledger providing the "keep committed work"
   guarantee. Collapse the four retry layers into a documented two: transport
   (network) + step (semantic), removing the phase-loop whole-turn re-run.
5. **Add `RunCoordinator` resume.** Wire `--resume` and crash recovery to replay
   the journal and continue the incomplete turn.

Backward compatibility: per project policy there are **no dual code paths** —
each phase replaces the mechanism it supersedes (e.g. the SQLite
`memory_snapshots` table is removed when the journal replaces it), with tests
updated in the same change.

---

## 7. Testing strategy

| Category | Tests |
|---|---|
| **Network fault injection** | `MockTransport` variants that raise `ReadTimeout`/`APIConnectionError` at: before first byte, mid-stream (after N deltas), between sub-turns, during tool execution. Assert prior + committed-step context is intact and tools ran exactly once. |
| **Timeout simulation** | Wrap `complete`/stream in a controllable clock; assert step-scoped retry, backoff bounds, deadline awareness, and no compounding beyond the configured caps. |
| **Process-termination (crash) tests** | Spawn a headless run, `SIGKILL` mid-turn at scripted points (after user append, after sub-turn A commit, mid-tool), restart with `--resume`, assert the journal fold == expected and the turn resumes from the right step. |
| **Replay tests** | Property test: fold(journal) is deterministic and order-independent of fsync timing; replaying twice yields identical `ShortTermMemory`. |
| **Persistence tests** | Every journal append is fsync'd and survives a hard kill immediately after the call returns; truncated/corrupt trailing entries are skipped (mirror `restore_from_log`). |
| **Idempotency tests** | Same `(turn, step, tool, args)` key executed under retry runs the underlying tool exactly once; side-effecting fakes (counter-incrementing `write_file`) assert single application. |
| **Parity gate (migration)** | For a corpus of recorded sessions, `fold(journal) == legacy session_memory.snapshot()` before authority flip. |
| **Provider matrix** | Run the fault-injection suite against Anthropic + OpenAI + a local/stub adapter to prove provider independence of the journal representation. |

---

## 8. Recommended phased implementation plan

Prioritizing robustness and correctness over minimal change, in dependency order:

### Phase 1 — Minimal fix (days)
- **Idempotent tool ledger (in-turn scope).** Even before durability, cache
  `(turn,step,tool,args) → result` in-process so a whole-turn retry does **not**
  re-execute already-completed tools. Eliminates the worst symptom (duplicate
  side effects + "restart the task") with a contained change to `_execute_tools`
  and the retry path.
- **Collapse retry layers / cap compounding.** Make the phase-loop stop
  re-running turns that the step-retry already exhausted; document the two
  remaining layers. Stops the ~30–40-attempt blow-up.
- **Stream partial handling.** On mid-stream interrupt, record the partial
  rather than discard it; expose it for the retry to supersede cleanly.

### Phase 2 — Durable state (1–2 weeks)
- **ConversationJournal as kernel events** (Option C) with reducer handlers and
  `EventProcessor` persistence; shadow-write + parity gate; then flip authority
  and delete the turn-boundary SQLite snapshot path.
- **MemoryProjector**: `snapshot/restore` become journal re-folds.

### Phase 3 — Resumable execution (2–3 weeks)
- **RunCoordinator**: turn/sub-turn state machine over the journal; `--resume`
  and crash recovery resume the incomplete turn from the last committed step;
  ToolLedger becomes durable (survives crashes).

### Phase 4 — Full fault tolerance (hardening)
- **Step-scoped retry** as the primary boundary; transport retry stays for raw
  network; remove whole-turn rollback entirely.
- **Streaming checkpointer** writing periodic delta entries.
- **Provider-matrix fault-injection** in CI; chaos tests (random `SIGKILL`)
  asserting exactly-once tool semantics and full recovery.

**Recommended immediate action:** ship **Phase 1's tool ledger** — it is small,
self-contained, and removes the user-visible "started over + re-ran my tools"
behavior — then proceed to Phase 2 for true crash durability.

---

## Appendix A — what PRD-126 already gets right (do not regress)

- `snapshot()` is a true deep copy; `restore()` yields an independent buffer
  (`_memory/__init__.py:892-914`).
- `messages()` never trims past the last conversational user message and never
  yields an empty list (`:789-816`) — prior history is not silently lost to
  token trimming.
- Compaction is summarize-then-replace and failure-safe (`compactor.py:83-114`).
- `CancelledError`/`KeyboardInterrupt` are never retried (`retry.py:108`).

The proposal **keeps** these properties; it changes *where* state is anchored
(durable journal vs in-process snapshot) and *what granularity* is retried
(round-trip vs whole turn), not the correctness of the primitives.
