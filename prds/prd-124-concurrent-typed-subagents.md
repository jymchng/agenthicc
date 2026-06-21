# PRD-124 — Concurrent Subagents

**Status:** Architecture specification. Implementation PRDs to follow.
**Author:** Systems Architecture

> **Amendment:** Typed result schemas removed. LLM output cannot reliably
> follow a Pydantic schema. All subagents return plain text. Aggregation
> concatenates labelled text blocks. No `result_type`, no `TypedResult`,
> no output parser. See §4, §5, §10, §11, §17 for updated details.

---

## 1. Goals

### Problems subagents solve

The current execution model is strictly serial: one phase → one agent → one
LLM call at a time. This produces three concrete problems:

**Latency.** A code-plan execute phase that touches 8 files runs each file
serially. A parallel implementation could complete in the time of the slowest
single-file operation.

**Specialisation loss.** A single general-purpose agent must hold the plan,
the current file, the test suite, the lint output, and the documentation
requirements simultaneously. Context fragmentation degrades quality as the
window fills.

**Opaque progress.** The user sees one undifferentiated stream of tool calls
with no indication of which logical work unit is executing.

### Workloads that benefit

| Workload | Concurrency pattern |
|---|---|
| Parallel linting | Fan-out: one linter per file/module |
| Parallel test generation | Fan-out: one tester per component |
| Repository exploration | Fan-out: simultaneous directory trees |
| Multi-file refactor | Fan-out: independent file transforms |
| Documentation generation | Fan-out: one documenter per public API |
| Plan verification | Fan-out: reviewer + verifier run simultaneously |
| Dependency analysis | Fan-out: multiple explorers, fan-in for synthesis |

### What must remain unchanged

- Single-agent turns (Auto, Plan, Ask modes) are unaffected.
- Existing `CodePlanRunner` phase sequence and gating tools.
- `ToolCapabilityGate` enforcement — subagents cannot escalate capabilities.
- `ConversationStore` / `ScrollBufferAppender` rendering contract.
- Session persistence and `--resume` correctness.
- Kernel event log append-only invariant.
- Plugin authoring surface.

---

## 2. Architectural Principles

### P1 — Parent agent is the sole orchestrator

Subagents are dispatched only when the parent agent explicitly calls the
`spawn_subagents` tool. The `WorkflowRunner` never autonomously spawns
subagents. The parent decides what types to spawn, what tasks to assign, and
what to do with aggregated results. This preserves the invariant that LLM
intent drives all execution.

*Rationale:* Top-down orchestration makes the causal chain auditable and
resumable. Bottom-up or automatic spawning produces non-deterministic
re-execution on resume.

### P2 — Subagents are isolated workers, not collaborators

Each subagent receives a read-only context injection (intent + plan +
task description) and produces a typed result. Subagents cannot read or write
the parent's `ShortTermMemory`. They cannot communicate with each other.
They cannot spawn further subagents (no nesting in v1).

*Rationale:* Shared mutable memory across concurrent agents requires
distributed locking. Typed outputs via value-passing is both simpler and
safer. Flat spawning depth prevents unbounded resource growth.

### P3 — Deterministic aggregation

The parent receives subagent results as a structured list, ordered by the
original spawn order regardless of completion order. The aggregation step is
a pure function over typed results.

*Rationale:* Non-deterministic ordering would produce different parent-agent
context on retry, making resume semantics undefined.

### P4 — Resumable execution

Every subagent result is persisted to the kernel event log before the pool
returns. On resume, completed results are replayed from the log; only
failed/incomplete subagents are re-executed.

*Rationale:* The most expensive workloads (large repository exploration,
parallel test generation) must not restart from scratch on interruption.

### P5 — Capability safety

A subagent's capability set is the intersection of the parent's mode
capabilities and the subagent type's static allowed-tool list. It can never
exceed either. The `ToolCapabilityGate` is enforced independently for each
subagent worker.

*Rationale:* A compromised subagent prompt (e.g. injected via file content)
must not be able to escalate permissions.

### P6 — Bounded concurrency

A global asyncio semaphore (`SubagentPool.semaphore`) limits simultaneous
LLM calls across all active pools. Default: 4 concurrent subagents. Configurable
per workflow or per spawn call.

*Rationale:* Unbounded parallelism produces rate-limit bursts and unpredictable
memory growth.

### P7 — Tool-based spawning

Spawning is expressed as a tool call (`spawn_subagents`), not a workflow
directive. This means the parent agent can decide dynamically whether to spawn,
what to spawn, and what to do with results — within a single agent turn — using
the same tool-use mechanism already in place.

*Rationale:* Avoids a new workflow-runner state machine. The runner remains
a phase sequencer; subagent coordination lives inside a phase turn.

---

## 3. Execution Model

### Lifecycle

```
Parent Agent Turn
  │
  ├─ LLM call → stop_reason = "tool_use"
  │            tool = "spawn_subagents"
  │            input = {tasks: [{type, task, context?}, ...]}
  │
  ├─ ToolExecutor.execute("spawn_subagents", input)
  │     │
  │     ├─ SubagentPool.create(tasks, mode_caps, max_concurrent)
  │     │
  │     ├─ for each task → SubagentWorker.allocate()
  │     │     ├─ resolve AgentType from AgentsRegistry
  │     │     ├─ build isolated ShortTermMemory
  │     │     ├─ inject context snapshot (read-only)
  │     │     └─ attach ToolCapabilityGate(intersection)
  │     │
  │     ├─ asyncio.gather(*[worker.run() for worker in pool])
  │     │     ├─ Worker 1: LLM calls + tools, produces TypedResult
  │     │     ├─ Worker 2: LLM calls + tools, produces TypedResult
  │     │     ├─ Worker 3: LLM calls + tools, produces TypedResult  ← concurrent
  │     │     └─ Worker N: LLM calls + tools, produces TypedResult
  │     │
  │     ├─ aggregator.collect(results)  → AggregatedResult
  │     │
  │     └─ persist SubagentPoolCompleted event to kernel log
  │
  └─ ToolResult(content=AggregatedResult.summary) → parent memory
       │
       └─ Parent LLM call resumes with aggregated results
```

### Sequence diagram — fan-out / fan-in

```
Parent          Pool            Worker1       Worker2       Worker3
  │                │                │             │             │
  ├─spawn_subagents─►               │             │             │
  │                ├─allocate──────►│             │             │
  │                ├─allocate───────────────────►│             │
  │                ├─allocate──────────────────────────────────►│
  │                │               │             │             │
  │                │           [run]         [run]         [run]
  │                │           tools         tools         tools
  │                │           result        result        result
  │                │               │             │             │
  │                │◄──done────────┘             │             │
  │                │◄──done──────────────────────┘             │
  │                │◄──done────────────────────────────────────┘
  │                │
  │                ├─aggregate
  │                ├─persist event
  │◄───result──────┘
  │
  ├─[parent LLM call with results]
```

### Synchronous vs async spawn

**Synchronous spawn (default):** `spawn_subagents` is a blocking tool call. The
parent's LLM loop suspends until all subagents complete (or timeout). The parent
receives all results before its next LLM call.

**Async spawn (v2, not in scope):** A `spawn_subagents_async` tool returns a
pool handle immediately; the parent can do other work and later call
`await_subagents(pool_id)` to collect results. Deferred to a follow-on PRD
because it requires the parent to maintain pool handles across LLM calls, which
complicates resume semantics.

### Nested spawning

**Not allowed in v1.** A subagent that calls `spawn_subagents` receives a tool
error: `"Nested subagent spawning is not supported."` This is enforced by
not including `spawn_subagents` in any subagent type's allowed-tool list.

Nested spawning would require a recursive pool graph, making cancellation,
resource accounting, and resume semantics significantly more complex. It is
deferred until the flat model proves insufficient in practice.

---

## 4. Subagent Types

All types are registered in `AgentsRegistry`. Each entry carries:
`system_prompt_path`, `allowed_tools: frozenset[str]`, `max_turns: int`,
`memory_access: MemoryAccessLevel`.

`MemoryAccessLevel` has two values: `CONTEXT_ONLY` (reads the injected snapshot,
no direct memory access) and `READ_SHARED` (can call `memory_read` tools against
the session's `MemoryRouter`). All types are `CONTEXT_ONLY` in v1.

**Output model:** All subagent types return `AgentResponse.content` — plain
text. No Pydantic schemas, no JSON parsing, no output validators. LLM output
cannot reliably follow a schema; plain text is the only robust contract.
The system prompt for each type instructs the agent on what to include in its
final response (e.g. "End your response with a one-paragraph summary of what
you found"), but this is a stylistic instruction, not a structural requirement.
Aggregation concatenates labelled text blocks; the parent agent reads them as
prose.

### Explorer

**Responsibilities:** Read-only codebase investigation. Builds a prose
description of files, symbols, dependencies, or patterns relevant to a task.

**Allowed tools:** `read_file`, `list_directory`, `search_files`, `grep_files`,
`git_log`, `git_show`, `git_blame`, `git_grep`, `file_exists`, `get_file_info`

**System prompt:** Injected from `system_prompts/explorer.md`. Instructs the
agent to summarise findings as Markdown with file paths and line references.
Never edits files.

**Output:** Plain text. Typically a Markdown summary with referenced paths.

**Memory access:** `CONTEXT_ONLY`

---

### Planner

**Responsibilities:** Produce a prose implementation plan for a specific
sub-task, given codebase context from the parent.

**Allowed tools:** `read_file`, `list_directory`, `search_files`

**System prompt:** Injected from `system_prompts/planner.md`. Instructs the
agent to write a numbered step list as prose.

**Output:** Plain text. Typically a numbered list of steps.

**Memory access:** `CONTEXT_ONLY`

---

### Implementer

**Responsibilities:** Execute a specific implementation task (a single file
or tightly scoped change). Does not make architectural decisions.

**Allowed tools:** `read_file`, `write_file`, `patch_file`, `append_file`,
`list_directory`, `search_files`, `grep_files`, `run_python_expr`

**System prompt:** Injected from `system_prompts/implementer.md`. Given a
precise task and file scope; must not stray beyond assigned files.

**Output:** Plain text. Summary of what was changed and why.

**Memory access:** `CONTEXT_ONLY`

---

### Tester

**Responsibilities:** Write or run tests for a specific component.

**Allowed tools:** `read_file`, `write_file`, `patch_file`, `run_tests`,
`run_bash`, `list_directory`, `search_files`

**System prompt:** Injected from `system_prompts/tester.md`. Instructs the
agent to describe tests written and their outcomes in prose.

**Output:** Plain text. Summary of tests written, passed, and failed.

**Memory access:** `CONTEXT_ONLY`

---

### Reviewer

**Responsibilities:** Review a specific change or file for correctness,
style, security issues, or logic errors.

**Allowed tools:** `read_file`, `list_directory`, `search_files`, `grep_files`,
`git_diff`, `run_python_expr`

**System prompt:** Injected from `system_prompts/reviewer.md`. Instructs the
agent to list issues found as prose with file/line references.

**Output:** Plain text. Prose review with issues and a final verdict sentence.

**Memory access:** `CONTEXT_ONLY`

---

### Documenter

**Responsibilities:** Write or update documentation for a specific module,
function, or API surface.

**Allowed tools:** `read_file`, `write_file`, `patch_file`, `search_files`,
`list_directory`

**System prompt:** Injected from `system_prompts/documenter.md`.

**Output:** Plain text. Summary of what was documented and which files changed.

**Memory access:** `CONTEXT_ONLY`

---

### Verifier

**Responsibilities:** Verify that a specific requirement or assertion holds in
the codebase. Adversarial companion to Reviewer.

**Allowed tools:** `read_file`, `search_files`, `grep_files`, `run_tests`,
`run_python_expr`, `git_diff`

**System prompt:** Injected from `system_prompts/verifier.md`. Instructs the
agent to actively look for counter-evidence and report its finding in prose.

**Output:** Plain text. Prose verdict with supporting evidence.

**Memory access:** `CONTEXT_ONLY`

---

### Researcher

**Responsibilities:** Web search and external documentation lookup for a
specific technical question.

**Allowed tools:** `search_web` (if Brave API key present), `fetch_page`,
`read_file` (for local docs)

**System prompt:** Injected from `system_prompts/researcher.md`.

**Output:** Plain text. Answer with source references inline.

**Memory access:** `CONTEXT_ONLY`

---

### AgentsRegistry integration

The existing `AgentsRegistry` in `agenthicc` is extended with a
`SubagentTypeRegistry` field:

```
AgentsRegistry
  └─ subagent_types: dict[str, SubagentTypeSpec]
       ├─ "explorer"     → SubagentTypeSpec(...)
       ├─ "implementer"  → SubagentTypeSpec(...)
       └─ ...
```

Plugin authors register custom types by dropping a `SubagentTypeSpec` into the
registry at plugin load time (see §17). The `spawn_subagents` tool validates
the requested type against this registry before allocating.

---

## 5. Subagent Pool Architecture

### Pool scope: per spawn call

Each `spawn_subagents` tool invocation creates one `SubagentPool` instance.
The pool is created, runs to completion (or cancellation), and is destroyed
before the tool returns. There is no persistent global pool across turns.

**Tradeoffs:**

| Scope | Pro | Con |
|---|---|---|
| Per spawn call | Simple lifecycle, no cross-turn state | Workers not reused |
| Per turn | Worker reuse within a turn | Complicates resume |
| Global | Maximum reuse | Cross-turn state is a bug source |

Per-spawn-call wins on simplicity and resume correctness. Worker startup cost
(a `ShortTermMemory` allocation + `@agent` class construction) is negligible.

### Pool structure

```
SubagentPool
  ├─ pool_id: str                      (UUID, used in kernel events)
  ├─ semaphore: asyncio.Semaphore      (max_concurrent, default 4)
  ├─ workers: list[SubagentWorker]     (one per task)
  ├─ results: list[SubagentResult]     (populated as workers complete)
  ├─ cancelled: asyncio.Event          (set on ESC / CancelledError)
  └─ context_snapshot: str            (read-only parent context injection)
```

### Task model

```
SubagentTask
  ├─ task_id: str           ("task-0", "task-1", …)
  ├─ agent_type: str        ("explorer", "tester", …)
  ├─ task_description: str  (injected into subagent system prompt)
  └─ context: str | None    (optional additional context, truncated to 4k chars)
```

### Worker model

```
SubagentWorker
  ├─ task: SubagentTask
  ├─ memory: ShortTermMemory      (isolated, max_tokens=8_000)
  ├─ runner: AgentRunnerBase
  ├─ capability_gate: ToolCapabilityGate
  └─ status: WorkerStatus         (PENDING → RUNNING → DONE / FAILED)
```

Each worker runs under a semaphore slot:

```
async def run(self) -> SubagentResult:
    async with self._pool.semaphore:
        self.status = RUNNING
        emit SubagentStarted event
        try:
            response = await self.runner.run(agent, task_message, memory=self.memory)
            # Plain text — no schema, no parsing. AgentResponse.content is the result.
            text = response.content or ""
            self.status = DONE
            emit SubagentCompleted event
            return SubagentResult(ok=True, text=text)
        except (CancelledError, BaseException):
            self.status = FAILED
            emit SubagentFailed event
            raise / return SubagentResult(ok=False, text="", error=str(exc))
```

### Queue model

No explicit queue. Workers are pre-allocated at pool creation time (one per
task). The semaphore provides natural queuing: `asyncio.gather` on all workers
starts all of them simultaneously, but only `max_concurrent` hold the semaphore
at any moment. The others block at `async with self._pool.semaphore` until a
slot frees.

This avoids a task-queue data structure while achieving identical semantics to
a bounded queue with N workers.

### Aggregation

After `asyncio.gather` returns, the pool produces an `AggregatedResult` by
concatenating each worker's plain-text output with a labelled header. No
second LLM call. No schema parsing.

```
AggregatedResult
  ├─ pool_id: str
  ├─ total: int
  ├─ succeeded: int
  ├─ failed: int
  └─ text: str          (the concatenated plain-text digest, in spawn order)
```

**Aggregation format:**

```
=== explorer #1 (✓ 1.2s) ===
Found 3 relevant files in auth/: session.py (L1-120), middleware.py (L45-88),
tests/test_auth.py (L200-340). Session tokens stored as plain strings — no
expiry or signing.

=== tester #1 (✓ 3.1s) ===
Wrote 8 unit tests in tests/test_jwt.py covering encode, decode, expiry, and
tamper detection. All 8 pass.

=== implementer #1 (✗ timeout) ===
[failed: execution timed out after 120s]
```

Each section is `=== {type} #{n} ({status} {duration}) ===` followed by the
worker's `AgentResponse.content` verbatim, truncated to 2,000 characters if
necessary. Failed workers show `[failed: {error}]` as their content.

The `text` field is the `ToolResult.content` the parent agent receives — it
reads the entire digest as prose in its next LLM call. Because the parent is
also an LLM, natural-language summaries are more reliable than structured data
as a communication channel between agents.

---

## 6. Memory Architecture

### Three memory domains

```
┌────────────────────────────────────────────────────────┐
│ WorkflowMemory                                         │
│  CodePlanContext (intent, plan, summaries, task list)  │
│  Read by: parent, injected as context to subagents     │
│  Written by: parent only                               │
└────────────────────────────────────────────────────────┘
         │ context_snapshot (read-only str injection)
         ▼
┌────────────────────────────────────────────────────────┐
│ ParentMemory (ShortTermMemory, max_tokens=32_000)      │
│  Full conversation history: user→assistant→tool turns  │
│  Read by: parent agent runner                          │
│  Written by: parent agent runner                       │
│  NOT accessible to subagents                           │
└────────────────────────────────────────────────────────┘
         │ snapshot at spawn time → injected as system context
         ▼
┌────────────────────────────────────────────────────────┐
│ SubagentMemory (ShortTermMemory, max_tokens=8_000)     │
│  Isolated per worker, created fresh at spawn           │
│  Read by: subagent runner only                         │
│  Written by: subagent runner only                      │
│  Destroyed when worker completes                       │
└────────────────────────────────────────────────────────┘
         │ typed result extracted at completion
         ▼
┌────────────────────────────────────────────────────────┐
│ AggregatedResult (plain-text digest + typed structs)   │
│  Appended to ParentMemory as a tool result             │
│  Persisted to kernel event log                         │
└────────────────────────────────────────────────────────┘
```

### Context snapshot injection

At pool creation time, a `context_snapshot` string is constructed from the
parent's workflow context (not from the parent's full conversation history).
It contains:

```
[PARENT CONTEXT]
Intent: {ctx.intent}
Plan: {ctx.plan}          (if available)
Task: {task.task_description}
```

This is injected as the **first user message** in the subagent's
`ShortTermMemory`. The subagent's system prompt comes from its type definition.
The context snapshot is capped at 8,000 characters to prevent subagent memory
overflow before the agent has begun work.

### Write permissions

| Domain | Parent reads | Parent writes | Subagent reads | Subagent writes |
|---|---|---|---|---|
| WorkflowMemory | ✓ | ✓ | Context snapshot only | ✗ |
| ParentMemory | ✓ | ✓ | ✗ | ✗ |
| SubagentMemory | ✗ | ✗ | ✓ | ✓ |
| ProjectMemory (MemoryRouter) | ✓ | ✓ | Read-only if CONTEXT_ONLY | ✗ |
| Filesystem | ✓ (via tools) | ✓ (via tools) | Type-dependent | Type-dependent |

**Key invariant:** Subagents cannot mutate `ParentMemory` or `WorkflowMemory`.
They produce outputs (typed results, filesystem changes) and return them through
the value-passing aggregation channel.

### Memory growth controls

- Subagent `ShortTermMemory` is capped at `max_tokens=8_000`. This is smaller
  than the parent's 32,000 cap to keep each worker lightweight.
- Auto-compact (PRD-119) is active for subagents too — threshold proportionally
  scaled to 200,000 tokens for subagents (they run shorter sessions).
- Parent memory grows by exactly one tool result per `spawn_subagents` call
  (the aggregated summary). Individual subagent conversations are never
  appended to parent memory.

---

## 7. Tool Capability Model

### Intersection rule

```
SubagentEffectiveCapabilities =
    ModeBlockedCapabilities.inverse        (parent's allowed set)
  ∩ SubagentTypeSpec.allowed_tools         (type's static allow-list)
```

Neither side can expand the other. A Tester subagent running in Plan mode
(which blocks EXECUTE capability) cannot execute code even though Tester
normally allows it.

### Per-type capability table

| Type | Reads files | Writes files | Executes code | Runs tests | Git read | Git write | Web |
|---|---|---|---|---|---|---|---|
| Explorer | ✓ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ |
| Planner | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Implementer | ✓ | ✓ | Limited¹ | ✗ | ✗ | ✗ | ✗ |
| Tester | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Reviewer | ✓ | ✗ | Limited¹ | ✗ | ✓ | ✗ | ✗ |
| Documenter | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Verifier | ✓ | ✗ | ✓ | ✓ | ✓ | ✗ | ✗ |
| Researcher | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |

¹ `run_python_expr` only (sandboxed expression evaluation, no shell).

### Escalation behaviour

If a subagent's tool call is blocked by `ToolCapabilityGate`, the tool returns
a standard error result: `"Tool '{name}' is not available for {agent_type}
agents."` The subagent may retry with a different tool or report the limitation
in its result. No escalation path to the parent agent exists mid-execution.
The parent may re-spawn with a different type if it determines the task
requires different capabilities.

### `spawn_subagents` capability gate

The `spawn_subagents` tool itself is **not** in any subagent type's allowed-tool
list. This is the enforcement mechanism for the no-nesting rule (P2).

The parent agent's `spawn_subagents` availability is controlled by the mode's
`blocked_capabilities` set. Administrators can disable subagent spawning in
Safe mode by adding `"spawn_subagents"` to the Safe mode's blocked list.

---

## 8. Workflow Integration

### Phase-level integration

The `WorkflowRunner` and `CodePlanRunner` do not change structurally.
The parent agent in each phase gains access to `spawn_subagents` as a base
tool. Whether it uses it is the parent's decision.

```
WorkflowRunner.run(intent)
  │
  ├─ Phase: PLAN
  │   └─ _run_agent_turn(text, tools=[base_tools + spawn_subagents + planner_tools])
  │        Parent may spawn Explorer subagents to investigate before finalizing plan.
  │
  ├─ Phase: EXECUTE
  │   └─ _run_agent_turn(text, tools=[base_tools + spawn_subagents + executor_tools])
  │        Parent may spawn Implementer / Tester subagents concurrently.
  │
  ├─ Phase: REVIEW
  │   └─ _run_agent_turn(text, tools=[base_tools + spawn_subagents + reviewer_tools])
  │        Parent may spawn Reviewer + Verifier subagents simultaneously.
  │
  └─ Phase: SUMMARIZE
      └─ _run_agent_turn(text, tools=[base_tools])
           Single-agent; no subagents needed.
```

### Phase completion semantics

Phase completion gates (`finalize_plan`, `mark_execute_complete`,
`approve_review`) are unaffected. A phase does not complete until the parent
agent calls its gate tool. If the parent spawns subagents and the spawn fails,
the parent's next LLM call receives the failure summary and decides how to
proceed (retry, fall back to sequential, or escalate with gate tool).

### Retry semantics

If `spawn_subagents` returns with some failed workers, the parent receives a
summary showing which tasks failed. The parent's LLM call can choose to:

1. Re-spawn only the failed tasks (by calling `spawn_subagents` again with
   a subset).
2. Handle the failures inline (call base tools directly for the failed tasks).
3. Proceed with partial results (call the phase completion gate with a note
   about the failures).

The phase retry loop in `CodePlanRunner` is unchanged — it retries the entire
`_run_agent_turn` only if the phase completion tool is never called. A
`spawn_subagents` call that returns (even with failures) is still an agent
turn that completes normally.

---

## 9. TUI Design

### Status bar — during active pool

Line 1 extends the existing state display:

```
✿ Running │ Execute │ 4/4 subagents │ ↑ 3,200 ↓ 1,100 │ 45s
```

When `SubagentPool` is active, line 1 shows:
- Flower + Running state (unchanged)
- Current phase name
- `N/M subagents` — completed / total (updates live as workers finish)
- Token counts + elapsed (existing)

Line 2 (model name) is unchanged.

### Footer extension

While a pool is active, the footer shows a concise worker grid:

```
──────────────────────────────────────────────────────────────
  ⎡ explorer #1 ✓  ⎤ ⎡ explorer #2 ⠸  ⎤ ⎡ tester #1 ⠼  ⎤ ⎡ tester #2 ○  ⎤
──────────────────────────────────────────────────────────────❯ ▌
```

Each cell: `type #N` + status icon (`○` pending, spinner running, `✓` done, `✗` failed).
Row hidden when no pool is active.

### Scroll buffer transcript

**On `spawn_subagents` call (pool starts):**

```
  ▶ Spawning 4 subagents
  ⎿ explorer #1     investigate auth module
  ⎿ explorer #2     investigate test suite
  ⎿ tester #1       write unit tests for JWT
  ⎿ implementer #1  implement JWT encode/decode
```

**On individual worker completion:**

```
  ✓ explorer #1     Found 3 relevant files in auth/   (1.2s)
  ✓ explorer #2     Test coverage: 42% on auth module (0.9s)
  ✓ tester #1       Wrote 8 tests, 8 passed           (3.1s)
  ✓ implementer #1  Modified auth/jwt.py              (4.7s)
```

**On pool completion:**

```
  ◈ 4/4 subagents complete   5.1s total
```

**On partial failure:**

```
  ✗ tester #1       LLM error: context exceeded       (timeout)
  ◈ 3/4 subagents complete   5.1s   1 failed
```

### Collapse / expansion

The worker spawn lines (`⎿ type #N  task`) are collapsible. When there are
more than 6 workers in a pool, the lines are collapsed by default:

```
  ▶ Spawning 8 subagents  [expand]
```

The user can press `e` (when cursor is on the collapsed line) to expand.
Expanded state is per-turn, not persisted.

### Live update mechanism

Worker completion events (`SubagentCompleted`, `SubagentFailed`) are appended
to the `ConversationStore` event queue normally. `ScrollBufferAppender` renders
them with `@register_renderer("subagent_started")` etc. (see §10).

The footer worker grid is driven by a new `Signal[SubagentPoolState | None]` on
`ConversationStore`. The signal is set when a pool starts and updated on each
worker state change. The workspace subscribes to it for redraws (one entry in
the existing subscription list).

---

## 10. Kernel Events

### New event types

**`SubagentPoolStarted`**
```
payload:
  pool_id:    str
  phase:      str          ("execute", "plan", …)
  tasks:      list[dict]   ([{task_id, type, description}, …])
  max_concurrent: int
persistence: required (needed for resume)
replay: yes — used to reconstruct pool state on resume
```

**`SubagentStarted`**
```
payload:
  pool_id:  str
  task_id:  str
  type:     str
  task:     str
persistence: required
replay: yes
```

**`SubagentCompleted`**
```
payload:
  pool_id:      str
  task_id:      str
  type:         str
  text:         str        (AgentResponse.content verbatim — plain text)
  duration_ms:  int
persistence: required
replay: yes — text is the authoritative record for resume; re-running is
              never needed for completed workers
```

**`SubagentFailed`**
```
payload:
  pool_id:    str
  task_id:    str
  type:       str
  error:      str
  retryable:  bool
persistence: required
replay: yes
```

**`SubagentCancelled`**
```
payload:
  pool_id:  str
  task_id:  str
  reason:   str   ("user_cancel", "timeout", "parent_cancel")
persistence: required
replay: yes
```

**`SubagentPoolCompleted`**
```
payload:
  pool_id:     str
  total:       int
  succeeded:   int
  failed:      int
  cancelled:   int
  text:        str    (the AggregatedResult.text — full labelled concatenation)
  duration_ms: int
persistence: required
replay: yes — text is injected verbatim into parent memory as a tool result
              on resume; no re-execution of any worker needed
```

### Event persistence requirements

All subagent events are appended to `events.jsonl` in insertion order before
the pool's `spawn_subagents` tool call returns. This guarantees that the kernel
log is consistent at any interruption point.

The `SubagentPoolCompleted.summary` field carries the full aggregated text so
that resume can reconstruct the parent's tool result without re-running any
subagent.

---

## 11. Session Persistence

### JSONL record structure

The kernel's `events.jsonl` already stores all `Event` objects. The new
subagent events slot into the existing format:

```jsonl
{"event_id":"…","event_type":"SubagentPoolStarted","payload":{…},"timestamp":…}
{"event_id":"…","event_type":"SubagentStarted","payload":{"pool_id":"…","task_id":"task-0","type":"explorer",…},"timestamp":…}
{"event_id":"…","event_type":"SubagentCompleted","payload":{"pool_id":"…","task_id":"task-0","result":{…}},"timestamp":…}
{"event_id":"…","event_type":"SubagentStarted","payload":{"pool_id":"…","task_id":"task-1","type":"tester",…},"timestamp":…}
{"event_id":"…","event_type":"SubagentFailed","payload":{"pool_id":"…","task_id":"task-1","error":"…"},"timestamp":…}
{"event_id":"…","event_type":"SubagentPoolCompleted","payload":{"pool_id":"…","summary":"…"},"timestamp":…}
```

### WorkflowContext reconstruction on `--resume`

On resume:

1. The `restore_from_log()` function replays all kernel events in order.
2. `SubagentPoolCompleted` events are detected and their `summary` payload
   is replayed as a synthetic tool result into the parent's `ShortTermMemory`
   — exactly as if the original `spawn_subagents` tool call had returned.
3. `SubagentCompleted` events are used to identify which tasks were done.
4. The `WorkflowContext.phase_outputs` dict is populated from the log, so the
   runner knows which phases completed.

### Partial completion handling

If the process was interrupted mid-pool (some workers completed, some did not):

1. On resume, the pool is re-created with only the incomplete tasks
   (those without a `SubagentCompleted` or `SubagentCancelled` record).
2. Completed results are read from the log and pre-populated into the
   `results` list.
3. The semaphore is re-created with the original `max_concurrent`.
4. `asyncio.gather` runs only the incomplete workers.
5. On pool completion, a new `SubagentPoolCompleted` event is appended with
   the merged results.

This means a pool with 8 workers that completed 5 before interruption only
re-runs 3 on resume.

---

## 12. Cancellation Model

### ESC (user cancel during subagent run)

1. The TUI input session sends `InterruptAgentCommand`.
2. The `agent_task` asyncio Task is cancelled (`task.cancel()`).
3. `CancelledError` propagates into `spawn_subagents` tool execution.
4. The `SubagentPool.cancel()` method sets `self.cancelled` event.
5. All running workers receive `CancelledError` at their next `await` point.
6. Each worker catches `CancelledError`, emits `SubagentCancelled`, and exits.
7. The semaphore is released for each cancellation.
8. `asyncio.gather(return_exceptions=True)` collects all outcomes.
9. Pool emits `SubagentPoolCompleted` with `cancelled > 0`.
10. `ToolResult(is_error=True, content="Cancelled by user")` returned to
    parent runner.
11. Parent runner's `except BaseException: memory.ensure_valid(); raise` fires.
12. `close_turn(error="Cancelled")` is called normally.

**Partial completion:** Workers that completed before the cancel retain their
`SubagentCompleted` events in the log. If the user resumes, those results are
available.

### Ctrl+C (SIGINT)

Same as ESC. `KeyboardInterrupt` propagates through `asyncio.run()` and
triggers the same cancellation path.

### Workflow cancellation (phase error / exception)

If the parent's `_run_agent_turn` raises (e.g. transport error, budget
exceeded), the pool is not yet active — `spawn_subagents` is a tool call
inside the turn. Exception propagation is the same as for any tool failure.

If the exception occurs after `spawn_subagents` has already started (e.g. a
second tool call in the same turn), the pool has already completed by then and
its results are in parent memory. The exception is handled by the phase retry
loop normally.

### Subagent failure — does it cancel siblings?

**No — by default.** A failed worker emits `SubagentFailed` and the pool
continues. This is the correct default for fan-out workloads where each task
is independent (one failing linter should not block the other seven).

**Configurable via `spawn_subagents(fail_fast=True)`:** When set, the first
worker failure cancels the pool. This is appropriate when tasks are logically
dependent (e.g. all reviewers must approve, not just some).

### Graceful shutdown window

After `pool.cancel()` is called, each worker has a 5-second window to reach a
cancellation point before it is forcibly killed with `task.cancel(force=True)`.
This allows workers that are mid-LLM-response to finish streaming and persist
their partial result.

---

## 13. Failure Handling

### Tool failures inside a subagent

Handled by the subagent's own `AgentRunnerBase` with the existing
`tool_error_policy`. Default is `"return_error"` — the tool failure is returned
as a tool result and the subagent's LLM call continues. The subagent may retry
the tool, use a different tool, or include the failure in its typed output.

### LLM failures (transport errors)

Transient errors (429, 529, 5xx): handled by the transport's retry logic
(`max_retries` in `LLMConfig`). The worker waits for backoff and retries.

Permanent errors (400, 401, 403): the worker fails immediately with
`SubagentFailed(retryable=False)`. The pool continues with other workers.
The aggregated result includes the failure. The parent agent decides whether
to retry the specific task.

### Timeouts

Each worker has a configurable `timeout_s` (default: 120 seconds, configurable
per subagent type via `SubagentTypeSpec.max_turn_time_s`). When exceeded:

1. Worker is cancelled.
2. `SubagentFailed(error="timeout")` is emitted.
3. Pool continues.

The pool itself has a `max_total_time_s` (default: 600 seconds). If the pool
exceeds this, all remaining workers are cancelled and the pool completes with
whatever results were collected.

### Worker crashes (unhandled exceptions)

`asyncio.gather(return_exceptions=True)` catches all exceptions. An unhandled
exception in a worker is treated as `SubagentFailed(error=str(exc))`. The pool
never propagates exceptions from individual workers to the parent.

### Parent agent recovery flow

```
spawn_subagents returns AggregatedResult(succeeded=3, failed=1, ...)
  │
  └─ Parent LLM call receives summary:
       "3 of 4 tasks succeeded. Failed: tester #1 (timeout).
        Completed: explorer #1, explorer #2, implementer #1."
       │
       ├─ Option A: parent calls spawn_subagents([{type="tester", task=failed_task}])
       │            (re-spawns only the failed task)
       │
       ├─ Option B: parent calls base tools directly to complete the failed task
       │
       └─ Option C: parent calls mark_execute_complete with a note about the failure
```

---

## 14. Scheduling Strategy

### Comparison

| Strategy | Description | Complexity | Throughput | Observability |
|---|---|---|---|---|
| Fixed worker pool (semaphore) | Pre-allocate all workers; semaphore bounds concurrency | Low | Good | Excellent — all workers visible upfront |
| Dynamic worker pool | Workers created on demand; pool grows/shrinks | Medium | Marginally better | Harder — worker count varies |
| Work stealing | Workers pull from a shared queue; idle workers steal from busy workers | High | Best for heterogeneous task durations | Complex — task ownership unclear |

### Recommendation: Fixed worker pool with asyncio semaphore

**Chosen:** Pre-allocate all `SubagentWorker` instances at pool creation time.
Concurrency is bounded by an `asyncio.Semaphore(max_concurrent)`. Workers
that exceed the semaphore bound block at `async with semaphore` until a slot
frees.

**Rationale:**

1. **Complexity:** The semaphore pattern is already used throughout agenthicc
   (existing `Scheduler` uses it). No new scheduler logic needed.

2. **Throughput:** For typical subagent counts (4–16), fixed allocation with
   a semaphore achieves near-optimal throughput. Work stealing adds meaningful
   benefit only when task durations vary by 10×+, which is rare for LLM calls.

3. **Observability:** All workers are created up-front, so the TUI can show the
   complete worker grid immediately when the pool starts, before any work begins.
   Dynamic allocation would show workers appearing and disappearing, which is
   confusing.

4. **Resume:** Fixed allocation makes replay straightforward — the exact same
   worker list is reconstructed from the log.

---

## 15. Resume Semantics

### Resume state machine

```
               ┌──────────────────────────────────────────────┐
               │           load events.jsonl                  │
               └──────────────────────────────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
     No pool events          Pool fully complete          Pool partial
     in log for this         (SubagentPoolCompleted       (some SubagentCompleted,
     phase                    present)                     no PoolCompleted)
              │                       │                       │
              ▼                       ▼                       ▼
     Re-run phase           Inject summary as          Reconstruct pool with
     from scratch           synthetic tool result      incomplete tasks only;
                            into parent memory;        re-run only those workers;
                            resume from next           collect + merge results;
                            parent LLM call            emit PoolCompleted
```

### --resume: restart vs replay vs reconstruct

| Scenario | Behaviour |
|---|---|
| Phase not started | Restart from the beginning of that phase |
| Phase in progress, no pool active | Restart phase turn from beginning |
| Pool started, no worker completed | Restart pool with all workers |
| Pool started, some workers completed | Reconstruct pool with incomplete workers only; replay completed results |
| Pool fully completed | Replay `PoolCompleted.summary` as tool result; skip re-execution |
| Phase gate called (finalize_plan etc.) | Phase is complete; skip to next phase |

### Invariant

A resume must never re-execute a subagent whose `SubagentCompleted` event is in
the log. The log is the source of truth for completion status.

---

## 16. Observability

### Metrics (emitted as kernel events, aggregatable externally)

| Metric | Source |
|---|---|
| Subagents spawned per session | Count of `SubagentStarted` events |
| Average worker duration | `SubagentCompleted.duration_ms` mean |
| Worker failure rate | `SubagentFailed` / `SubagentStarted` ratio |
| Concurrency utilization | `max_concurrent` vs simultaneous `RUNNING` workers |
| Pool completion time | `SubagentPoolCompleted.duration_ms` |
| Per-type success rate | Group `SubagentCompleted` / `SubagentFailed` by `type` |

### Log output (scroll buffer + `events.jsonl`)

The scroll buffer transcript (§9) provides the human-readable view. The
`events.jsonl` provides the machine-readable view. No additional log files.

### Status indicators

- Status bar `N/M subagents` counter (§9) — live, updates on each completion.
- Footer worker grid (§9) — live, updates on each state change.
- `ConversationStore.subagent_pool_state: Signal[SubagentPoolState | None]` —
  drives both status bar and footer.

### No external metrics system in v1

Prometheus / OTLP export is a follow-on. All metrics are derivable from
`events.jsonl` offline.

---

## 17. Plugin Integration

### Custom subagent types

A plugin registers a `SubagentTypeSpec` with the `SubagentTypeRegistry`:

```python
class MyCustomReviewerSpec(SubagentTypeSpec):
    name = "security_reviewer"
    allowed_tools = frozenset(["read_file", "grep_files", "search_files"])
    max_turns = 10
    max_turn_time_s = 60
    system_prompt = (
        "You are a security-focused code reviewer. "
        "End your response with a clear verdict: APPROVED or NEEDS CHANGES, "
        "followed by a bullet list of issues found."
    )
    # No result_type — output is plain text. The system prompt instructs
    # the agent on the expected prose format; no schema enforcement.
```

The plugin registers it in its `AgenthiccPlugin.on_load()`:

```python
def on_load(self, registry: PluginRegistry) -> None:
    registry.subagent_types.register(MyCustomReviewerSpec())
```

The `spawn_subagents` tool validates the `type` field against the registry, so
`{"type": "security_reviewer", ...}` becomes available immediately.

### Custom aggregators

A plugin can register a custom aggregator for a named agent type. The
aggregator receives a list of `SubagentResult` objects (each with a plain-text
`text` field) and returns a formatted string that will be the `ToolResult`
content sent to the parent agent.

```python
class SecurityAggregator(SubagentAggregator):
    agent_type = "security_reviewer"

    def aggregate(self, results: list[SubagentResult]) -> str:
        lines = ["Security review summary:"]
        for i, r in enumerate(results, 1):
            status = "✓" if r.ok else "✗"
            # Plain text — scan prose for keywords, not schema fields
            verdict = "NEEDS CHANGES" if "NEEDS CHANGES" in r.text else "APPROVED"
            lines.append(f"  {status} reviewer #{i}: {verdict}")
        lines.append("")
        for i, r in enumerate(results, 1):
            lines.append(f"=== reviewer #{i} ===\n{r.text[:1000]}")
        return "\n".join(lines)
```

Registered via:

```python
registry.subagent_aggregators.register(SecurityAggregator())
```

If no custom aggregator is registered for a type, the default aggregator
produces the labelled-concatenation format described in §5.

### Custom scheduling policies

A plugin can replace the `SubagentPool` constructor's default
`max_concurrent=4` with a custom `SchedulingPolicy`:

```python
class RateLimitedPolicy(SchedulingPolicy):
    def max_concurrent(self, tasks: list[SubagentTask]) -> int:
        explorer_count = sum(1 for t in tasks if t.agent_type == "explorer")
        return min(8, explorer_count)   # allow more explorers
```

Registered via `registry.scheduling_policies.register(RateLimitedPolicy())`.
The `SubagentPool` queries the registry for a policy matching the task list
before setting the semaphore bound.

### No modifications to core dispatchers

The `spawn_subagents` tool, `SubagentPool`, and `ToolCapabilityGate` are
extended only via the registry interfaces above. Plugin authors cannot
monkey-patch or subclass core pool components.

---

## 18. Migration Plan

### Phase 1 — Minimal infrastructure (no workflow integration)

- `SubagentTypeSpec`, `SubagentTypeRegistry`, `SubagentWorker`, `SubagentPool`
  as library code in `src/agenthicc/subagents/`.
- `spawn_subagents` tool with hard-coded Explorer and Implementer types.
- No TUI integration — scroll buffer gets a plain text event: `"N subagents ran."`.
- No kernel event persistence.
- Unit tests for pool lifecycle, cancellation, aggregation.

**Risk:** Low. Entirely new code path; existing flows unaffected.

### Phase 2 — Workflow integration

- `spawn_subagents` added to base tools in `_run_agent_turn`.
- All 8 typed agents registered.
- Full kernel event emission (`SubagentPoolStarted` → `SubagentPoolCompleted`).
- Event persistence in `events.jsonl`.

**Risk:** Medium. Pool execution is new concurrent code running inside existing
turn infrastructure. Regression risk in memory management and exception
propagation.

### Phase 3 — TUI integration

- `ConversationStore.subagent_pool_state: Signal[SubagentPoolState | None]`.
- Status bar `N/M subagents` counter.
- Footer worker grid.
- Scroll buffer renderers for all subagent events.
- Collapse/expand behaviour.

**Risk:** Low. TUI changes are additive (new signals, new renderers).

### Phase 4 — Resume support

- `restore_from_log()` extended to replay subagent events.
- Partial-pool reconstruction on resume.
- Integration tests: interrupt mid-pool, resume, verify partial results.

**Risk:** High. Resume correctness is hard to test exhaustively. Requires
careful ordering of event log writes.

### Phase 5 — Plugin ecosystem

- `SubagentTypeRegistry` exposed in `PluginRegistry`.
- `SubagentAggregator` and `SchedulingPolicy` extension points.
- Documentation for plugin authors.
- Example custom type in the `python-password-generator` demo project.

**Risk:** Low. Additive to existing plugin architecture.

---

## 19. Recommended Final Architecture

### Component diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ agenthicc                                                       │
│                                                                 │
│  TUISession / HTTPServer                                        │
│       │                                                         │
│  WorkflowRunner                                                 │
│       │                                                         │
│  Phase (_run_agent_turn)                                        │
│       │                                                         │
│  AgentRunnerBase (parent)                                       │
│       │ tool call                                               │
│  ┌────▼──────────────────────────────────────┐                 │
│  │  spawn_subagents (ToolExecutor)           │                 │
│  │       │                                   │                 │
│  │  SubagentPool                             │                 │
│  │  ├─ asyncio.Semaphore(max_concurrent)     │                 │
│  │  ├─ SubagentWorker[0..N]                  │                 │
│  │  │    ├─ AgentRunnerBase (isolated)        │                 │
│  │  │    ├─ ShortTermMemory (isolated)        │                 │
│  │  │    ├─ ToolCapabilityGate (intersection) │                 │
│  │  │    └─ SubagentTypeSpec (from registry)  │                 │
│  │  └─ Aggregator                            │                 │
│  │       └─ AggregatedResult → ToolResult   │                 │
│  └───────────────────────────────────────────┘                 │
│       │                                                         │
│  Parent memory receives AggregatedResult                       │
│       │                                                         │
│  Kernel EventProcessor                                         │
│  └─ events.jsonl (SubagentPool* events appended)               │
│                                                                 │
│  ConversationStore                                              │
│  └─ subagent_pool_state: Signal → Status Bar + Footer          │
│                                                                 │
│  SubagentTypeRegistry (in AgentsRegistry)                       │
│  └─ {explorer, planner, implementer, tester,                   │
│       reviewer, documenter, verifier, researcher}               │
└─────────────────────────────────────────────────────────────────┘
```

### Execution diagram (one pool lifecycle)

```
turn start
    │
    ├─ parent LLM call #1
    │   → stop_reason=tool_use, tool=spawn_subagents
    │
    ├─ spawn_subagents executes
    │   ├─ SubagentPool created
    │   ├─ SubagentPoolStarted → events.jsonl
    │   │
    │   ├─ gather([w0, w1, w2, w3]) with semaphore(2)
    │   │   │
    │   │   ├─ w0 (explorer) + w1 (tester) acquire semaphore → RUN
    │   │   │   SubagentStarted(w0), SubagentStarted(w1) → events.jsonl
    │   │   │
    │   │   ├─ w0 completes → SubagentCompleted(w0) → events.jsonl
    │   │   │   w2 (implementer) acquires semaphore → RUN
    │   │   │   SubagentStarted(w2) → events.jsonl
    │   │   │
    │   │   ├─ w1 completes → SubagentCompleted(w1) → events.jsonl
    │   │   │   w3 (reviewer) acquires semaphore → RUN
    │   │   │   SubagentStarted(w3) → events.jsonl
    │   │   │
    │   │   ├─ w2 completes → SubagentCompleted(w2) → events.jsonl
    │   │   └─ w3 completes → SubagentCompleted(w3) → events.jsonl
    │   │
    │   ├─ aggregate(results) → AggregatedResult
    │   ├─ SubagentPoolCompleted → events.jsonl
    │   └─ return ToolResult(content=summary)
    │
    ├─ parent memory receives AggregatedResult as tool result
    │
    ├─ parent LLM call #2 (with full context including subagent results)
    │   → decides next action (more tools, gate tool, etc.)
    │
    └─ turn ends normally
```

### Justification of major decisions

**Tool-based spawn (not workflow-directive spawn):**
The parent agent decides whether and what to spawn based on the task at hand.
This preserves the invariant that all execution is LLM-driven and makes the
spawn decision auditable in the conversation history as a tool call.

**Flat spawning depth (no nesting):**
Recursive pools would require a tree of semaphores, make cancellation
non-trivial, and complicate resume. The flat model handles all realistic
workloads. The rule is enforced by excluding `spawn_subagents` from all
subagent tool lists — no special runtime checks needed.

**Plain-text aggregation (not shared memory, not typed schemas):**
Concurrent writes to shared `ShortTermMemory` would require locking and make
turn replay non-deterministic. LLM output also cannot reliably follow a Pydantic
schema — silent schema violations produce corrupt structured data that is worse
than plain text. `AgentResponse.content` (plain text) passed through
value-passing is both simpler and more reliable. The parent agent is itself an
LLM and reads the aggregated prose naturally.

**Per-spawn-call pool scope:**
A persistent pool would outlive its turn, creating state that interferes with
resume. A pool scoped to one tool call has a trivial lifecycle: created →
ran → destroyed. All state is in the kernel event log.

**asyncio.Semaphore for concurrency (not threads):**
All LLM transports are async-native. Threading would introduce GIL contention
and context-switch overhead for no benefit. The semaphore integrates with
the existing event loop without any new concurrency primitives.

**Fixed worker allocation (not dynamic):**
All workers are created before any start, enabling the TUI to show the
complete pool grid immediately. Resume is deterministic: the same list is
reconstructed from the log. Dynamic allocation would make TUI rendering race-y
and resume logic more complex.

**Result persistence before tool return:**
`SubagentPoolCompleted` is written to `events.jsonl` before `spawn_subagents`
returns its `ToolResult`. This ensures that even if the process crashes between
the tool return and the parent's next LLM call, the results survive and resume
can reconstruct them.
