# Subagents and `spawn_subagents`

`spawn_subagents` is the session-wide delegation tool. It lets the current
agent explicitly fan out independent pieces of work to typed workers, wait for
all of them, and receive one complete text digest. It is synchronous from the
parent agent's point of view: the parent turn is suspended while the pool runs.
The parent then gets another model turn with the aggregate as the tool result.
The TUI and kernel projections still use bounded previews so a long chapter or
source file does not make the screen unusable.

This guide describes the implementation in the current source tree. The
authoritative implementation is split across:

| Concern | Source |
|---|---|
| Provider-facing tool and input validation | `src/agenthicc/subagents/tool.py` |
| Worker lifecycle, filtering, retries, aggregation, and events | `src/agenthicc/subagents/pool.py` |
| Built-in role prompts and allow-lists | `src/agenthicc/subagents/types.py` |
| Per-turn injection into the parent agent | `src/agenthicc/runners/agent_turn.py` |
| Durable worker/pool result records | `src/agenthicc/memory/journal.py` |
| Reactive pool status | `src/agenthicc/tui/conversation_store.py` and `src/agenthicc/tui/workspace/` |
| Usage accounting | `src/agenthicc/runners/usage_ledger.py` |

## The complete data flow

```text
user message
    │
    ▼
AgentTurnRunner._build_agent()
    │  builds the parent-visible tool list from mode, workflow phase,
    │  plugin/MCP tools, and capability exclusions
    │
    ├─ creates a session-bound spawn_subagents callable
    │      closes over the parent transport, model, ConversationStore,
    │      session journal, retry/usage settings, approval service,
    │      and workspace policy
    │
    └─ registers it in the parent provider tool schema
           │
           ▼
parent LLM emits spawn_subagents({tasks, max_concurrent?, timeout_s?})
           │
           ▼
tool.py validates and normalises every task
  - accepts canonical type/task/context keys
  - accepts runtime compatibility aliases agent_type/task_description
  - resolves research → researcher
  - validates type, task text, and timeout
           │
           ├─ matching complete result in ConversationStore?
           │       yes → return cached digest; do not call the provider
           │       no  → construct SubagentPool
           │
           ▼
SubagentPool.run()
  ├─ creates one isolated SubagentWorker per task
  ├─ bounds active workers with an asyncio semaphore
  ├─ emits kernel and ConversationStore progress events
  └─ gathers workers in input order, regardless of finish order
           │
           ▼
each SubagentWorker
  ├─ intersects role allow-list with the parent's already-filtered tools
  ├─ builds a fresh ShortTermMemory(max_tokens=8_000)
  ├─ builds role system prompt + optional [ADDITIONAL CONTEXT]
  ├─ adds the final-response contract to the role prompt
  ├─ installs capability and, when supplied, approval/workspace gates
  ├─ calls AgentRunnerBase.run() on the parent's transport
  ├─ asks once more for the final artefact if the provider returned empty prose
  ├─ persists the complete worker result to the session journal
  └─ returns SubagentResult(ok, text, error, tool_calls, changed_paths)
           │
           ▼
_aggregate()
  ├─ applies an optional type-specific aggregator
  └─ creates a labelled plain-text digest without truncating worker bodies
           │
           ├─ complete pool → append UI cache event and fsync durable cache record
           └─ partial/failed pool → return failure; never cache it
           │
           ▼
parent LLM receives the complete digest as the tool result and decides what to do next
           │
           ▼
lauren-ai commits the parent tool exchange into the shared provider journal
```

The worker does not share the parent's short-term message history. The
parent's task description and explicit `context` are value-passed into the
worker; the worker's final text is value-passed back. `ConversationStore` is a
reactive UI projection, not shared mutable worker memory. The worker result and
complete aggregate are additionally written to the parent's
`ConversationJournal`, which is the restart-safe persistence boundary. This
isolation is what makes concurrent execution deterministic and prevents one
worker from appending messages to another worker's provider conversation.

There are therefore three different representations of a subagent result:

| Representation | Contains | Purpose | Size policy |
|---|---|---|---|
| `SubagentResult.text` / `AggregatedResult.text` | Complete final worker prose | Parent tool result and provider memory | Not truncated by the pool |
| `ConversationJournal` records | Complete worker and complete-pool prose | Crash recovery and durable resume | Fsync'd; subject to normal session-storage sensitivity |
| `subagent_worker_done` and kernel completion payloads | Status plus a short preview | TUI/event observability | Preview limited to 2,000 characters |

The last row is intentionally not the source of truth. Seeing a short preview
in the TUI does not mean the full worker output was discarded.

## How the tool is exposed

The tool is created for each parent agent turn by
`AgentTurnRunner._build_agent()`. It is not a global singleton. That matters
because the callable captures the current turn's:

- parent `AgentRunnerBase` transport and effective model;
- visible tool list after mode and workflow filtering;
- `AppState`, `ConversationStore`, event processor, retry settings, and usage
  ledger;
- session `conversation_id` and parent run ID;
- approval service and workspace access policy, when the session supplies them.

The tool is visible only when it survives the same `allowed_tool_names` and
capability filtering as other parent tools. A workflow phase that supplies an
explicit allow-list must include `spawn_subagents` if it expects the parent to
delegate. The tool is deliberately absent from every built-in worker role, so
workers cannot recursively create more pools.

The provider-facing signature is:

```python
spawn_subagents(
    tasks: list[Task],
    max_concurrent: int = 4,
    timeout_s: float = 3600.0,
) -> dict[str, object]
```

The generated task schema is:

```json
{
  "type": "object",
  "properties": {
    "type": {"type": "string", "enum": ["explorer", "planner", "implementer", "executor", "tester", "reviewer", "documenter", "verifier", "researcher"]},
    "task": {"type": "string"},
    "context": {"type": "string"}
  },
  "required": ["type", "task"]
}
```

`context` is optional. It is intentionally present in the decorated tool's
provider metadata even though the current lauren-ai standalone TypedDict
schema helper does not preserve that optionality when asked to regenerate a
schema independently. `tool.py` repairs the model-facing metadata at the
decoration boundary and tests assert the schema actually sent by the agent.

The canonical model call is therefore:

```json
{
  "tasks": [
    {
      "type": "explorer",
      "task": "Find the files that implement session resume.",
      "context": "The parent already suspects the TUI loader and journal replay are involved."
    },
    {
      "type": "tester",
      "task": "Identify the smallest deterministic regression tests for the resume path."
    }
  ],
  "max_concurrent": 2,
  "timeout_s": 900
}
```

For compatibility with programmatic callers, the runtime also accepts
`agent_type` in place of `type` and `task_description` in place of `task`.
Those aliases are not the preferred provider schema; use `type` and `task` in
prompts, integrations, and tests.

## Built-in worker roles

The role is a prompt plus a static tool allow-list. The parent session's actual
visible tools are a second ceiling, so naming a tool in a role does not make a
tool available if the current phase or mode removed it.

| Role | Intended work | Mutation / execution |
|---|---|---|
| `explorer` | Read files, search, inspect git history, report evidence | Read-only |
| `planner` | Produce a numbered implementation plan from repository facts | Read-only |
| `implementer` | Make one focused file change and verify it | File writes/patches, no shell |
| `executor` | Carry out an implementation/build/compile task end to end | File writes plus shell/commands/tests |
| `tester` | Write and run focused tests | File writes plus test/command tools |
| `reviewer` | Inspect a change for correctness, security, and regressions | Read-only, including git read |
| `documenter` | Update scoped documentation and report changed files | Documentation/file writes |
| `verifier` | Adversarially check an invariant or acceptance criterion | Read and test tools; no writes |
| `researcher` | Answer a technical question from local source and docs | Read/search-oriented |

The `research` spelling is accepted as an alias for `researcher`. Custom role
names can be registered in a `SubagentTypeRegistry`; the tool augments the
provider enum with those names at runtime.

### Role-specific success rules

Most roles succeed when they return non-empty final prose. `implementer` and
other write-capable roles should be instructed to make the change, but the
strict mutation-evidence rule currently applies to the built-in
`implementer`: it must successfully invoke at least one mutating tool. A prose
claim such as “I would update the file” is reported as a failed implementer.

If a provider returns no final prose after tool calls, the worker synthesizes a
bounded summary such as `Executed tool call(s): read_file.`. If it returns no
prose and calls no tools, the worker fails with
`agent returned no final summary and executed no tools`.

## Capability, approval, and workspace boundaries

There are two independent filters:

```text
worker tools = parent-visible tools ∩ role.allowed_tools
```

The parent-visible list has already been restricted by the active workflow
phase and runtime mode. Each worker then installs `ToolCapabilityGate` using
the same `AppState`. Consequently:

- Plan mode hard-blocks write, execute, git-write, network, control, and
  undeclared capabilities;
- Safe mode permits reads but requires approval for restricted capabilities;
- Yolo mode does not require per-call capability approval;
- a worker cannot add a tool by changing its task text or context;
- a worker cannot recursively call `spawn_subagents` because that name is not
  in any built-in role allow-list.

When the parent has an approval service, the child also installs
`ApprovalGate` with the parent's approval service and workspace policy. Safe
mode approvals therefore pause the same TUI approval request path instead of
being silently bypassed by the nested runner. Workspace path authorization is
performed against the same parent policy. In headless operation, approval
behaviour depends on the approval service supplied by the headless session;
the absence of an approval service does not create a new approval UI.

This is a ceiling, not a grant. A role can still fail because the parent phase
did not expose a required tool, because the mode blocks it, because workspace
authorization denies the path, or because the user denies approval. The
returned aggregate preserves those failures for the parent to reason about.

## Memory and provider calls

Each worker gets a new `ShortTermMemory(max_tokens=8_000)`. It does not inherit
the parent's complete conversation, workflow journal, or other workers'
messages. The worker's system prompt is:

```text
<role system prompt>

[ADDITIONAL CONTEXT]
<task.context, when supplied>
```

The task description is sent as the worker's user message. This means context
should contain concise, relevant facts—not a full transcript. If the parent
needs to provide a large artifact, prefer a path and ask an explorer or
researcher to read it.

Workers use the parent's transport and model ID, and receive the session's
provider options such as temperature, top-p, completion limit, and request
options. They call `AgentRunnerBase.run()` directly, so the pool explicitly
wraps that path with the same snapshot/rollback transport retry helper used by
the main turn runner. Nullable provider usage fields are normalized before
Lauren's token arithmetic. Each worker gets its own usage-ledger run ID under
the parent run when accounting is enabled.

The parent receives only one aggregate tool result. Individual worker message
histories are not appended to the parent prompt. This keeps parent context
growth proportional to the bounded digest rather than to every worker turn.

## Concurrency, timeout, and failure semantics

`max_concurrent` controls an asyncio semaphore and defaults to four. The pool
does not start more than that many workers at once. Results are collected in
the original task order even if workers finish in a different order.

`timeout_s` is a finite, positive per-worker wall-clock deadline for this
invocation and defaults to one hour. It overrides the role's
`SubagentTypeSpec.max_turn_time_s` for calls through `spawn_subagents`. Direct
`SubagentWorker` callers can omit it and use the role default. A timeout is a
failed worker, not a pool exception, and sibling workers continue.

Worker exceptions are converted into `SubagentResult(ok=False, error=...)`.
The pool normally returns an aggregate even when some workers failed. The
outer tool sets `ok` to `false` whenever any worker failed:

```json
{
  "ok": false,
  "pool_id": "…",
  "total": 3,
  "succeeded": 2,
  "failed": 1,
  "error": "1 subagent(s) failed",
  "results": "=== explorer #1 … ===\n…\n\n=== tester #1 (✗ timeout …) ===\n…"
}
```

This is intentionally a recoverable tool result. The parent can retry only
the failed task, use a direct tool, or explain the limitation. A failed pool
does not get stored as a successful resume result.

Cancellation follows normal asyncio cancellation from the parent turn. Each
worker converts cancellation into a failed result, the semaphore is released
by its context manager, and the pool aggregates what it can. The parent turn
still owns the final cancellation/transaction cleanup.

## Why a worker can appear to return only `read_file`

The transcript in the incident report is explained by this sequence:

1. The parent asks a `documenter` worker to rewrite a chapter and return the
   full content.
2. The worker receives a fresh conversation, not the parent's transcript. It
   calls `read_file` (and sometimes `list_directory`) to inspect its source.
3. The provider ends the worker turn with empty `content`. Tool results are
   present only in the worker's private memory; they are not automatically
   copied into the parent response.
4. Before this fix, `SubagentWorker.run()` converted that empty response into
   `Executed tool call(s): read_file.` and `_aggregate()` truncated every
   successful section to 2,000 characters. The parent consequently had neither
   the requested chapter nor a durable worker transcript.

The current contract addresses each step without sharing mutable histories:

- The role and generic final-response prompts tell the worker that the parent
  sees only final prose and that explicitly requested full content must be
  returned verbatim.
- An empty provider completion receives one bounded finalisation turn using
  the worker's existing private tool history. A provider failure on that
  recovery turn falls back to the explicit tool summary instead of hiding a
  successful tool invocation.
- The parent-facing aggregate preserves complete worker text. The
  2,000-character limit remains only on TUI/kernel diagnostic projections.
- Each worker result is fsync'd to `conversation-journal.jsonl` before its
  completion event, and a complete aggregate is fsync'd before
  `spawn_subagents` returns. The parent runner then commits the same aggregate
  as the normal tool result in its shared provider memory.

If the worker still returns only a tool summary after this contract, the
provider did not produce the requested artefact even after the recovery turn;
that is a model/task failure, not a persistence failure. The durable journal
still contains the exact worker result and diagnostics for inspection.

Filesystem writes have a separate rule: when `write_file` is present in both
the parent-visible tool list and the role allow-list, the child invokes the
same session filesystem tool and its write is immediately visible in the
workspace (subject to mode approval and workspace authorization). A
`documenter` task that only calls `read_file` has not written a chapter; it
must either call `write_file`/`patch_file` or return the complete content for
the parent to apply. Worker provider messages are isolated, but filesystem
side effects are not rolled back merely because the worker's prose is short.

## Resume and cache behaviour

Before creating a pool, the tool scans the durable `ConversationJournal` when
available, then the restored `ConversationStore`, for a recent complete
`subagent_pool_result` with the same fingerprint. The journal is authoritative
for restart recovery; the reactive store remains a compatibility path for
lightweight callers and already-restored TUI sessions. The fingerprint is an
order-insensitive hash of every task's:

```text
(agent_type, task_description, context)
```

Task order does not matter; changing task text, role, or context does. The
context inclusion is important because context changes the worker system
prompt. Older implementations hashed only role and task, which could return a
stale result after the parent supplied new findings.

Only a pool with `failed == 0` is cached. Partial results are deliberately not
treated as successful on resume. A cached call returns `pool_id: "cached"`,
does not call the provider, and appends a small “Resumed” system event for the
TUI. Every worker result—including failed and timed-out results—is retained in
the journal for diagnostics, but only a complete aggregate is eligible for
automatic replay. The system does not yet replay five completed workers and
rerun only three from an interrupted eight-worker pool; an incomplete pool is
rerun as a whole.

## TUI and kernel observability

While a pool is active, `ConversationStore.subagent_pool_state` contains the
pool ID, total count, and per-worker `pending`, `running`, `done`, or `failed`
status. The workspace uses that signal for the live worker display and clears
it when aggregation finishes.

The pool also appends scroll events:

- `subagent_pool_started`;
- `subagent_worker_done` for each worker, including a bounded display summary, duration,
  tool calls, and changed-path hints;
- `subagent_pool_done`;
- `system` for a cached “Resumed” result.

When an `EventProcessor` is supplied, corresponding kernel events are emitted:
`SubagentPoolStarted`, `SubagentStarted`, `SubagentCompleted` or
`SubagentFailed`, and `SubagentPoolCompleted`. Event payloads contain bounded
text and diagnostics; they are not a replacement for the complete result or
the durable journal. `subagent_pool_result` stores the complete aggregate in
the reactive turn and durable journal, but has no verbose renderer because the
parent tool result is the user-facing artefact.

## Custom roles and aggregators

Register a role with a fresh registry and pass that registry to the factory:

```python
from agenthicc.subagents import SubagentTypeRegistry, SubagentTypeSpec

registry = SubagentTypeRegistry()
registry.register(
    SubagentTypeSpec(
        name="api_reviewer",
        allowed_tools=frozenset({"read_file", "search_files", "grep_files"}),
        max_turns=6,
        system_prompt=(
            "Review API changes for compatibility and security. "
            "End with APPROVED or NEEDS CHANGES and evidence."
        ),
    )
)
```

The role's names must match the names of tools actually passed in
`all_tools`. A role allow-list is not a way to discover or bypass tools. A
custom `SubagentAggregator` may combine all successful results for one role
into one plain-text section; aggregators should be deterministic and must not
perform side effects.

## Troubleshooting checklist

### The parent never calls `spawn_subagents`

This is a model decision, not automatic orchestration. Confirm that the tool
appears in the parent provider request and that the task prompt explains when
delegation is useful. In a phase with an explicit tool allow-list, include
`spawn_subagents` in that list.

### The tool is not present in the provider request

Inspect the active mode, workflow phase tool policy, capability exclusions, and
plugin registry. `AgentTurnRunner._build_agent()` injects the tool after the
initial session tools are assembled, then repopulates the agent metadata. A
phase-specific `allowed_tool_names` set can still exclude it.

### An implementer says it completed but `ok` is false

The implementer must make a successful mutating tool call. Check that the
parent phase exposed `write_file`, `patch_file`, or another write-capable
tool, that the active mode permits it, and that the provider actually emitted
the tool call instead of prose-only output.

### A worker fails immediately with no available tools

The role allow-list and parent-visible list have an empty intersection. This
is expected for an implementer in a read-only phase. Choose a read-only role
for investigation, or run the implementation phase with the intended mode
and tools exposed.

### Safe mode does not show an approval

The child must be created from the normal session turn path so the parent
approval service is passed into `make_spawn_subagents_tool`. Test-only callers
that provide `AppState` but no approval service have no service to display a
prompt. Headless callers should supply their explicit approval adapter.

### A repeated call returns old results

Check role, task, and context. All three are now part of the fingerprint, but
the cache is intentionally order-insensitive and only stores complete pools.
A pre-fix cache record using the old role/task-only fingerprint will not match
the new key and will be safely re-executed.

### The provider returns a 400 or tool-schema error

Inspect the actual decorated metadata rather than a separately regenerated
schema:

```python
metadata = spawn_subagents.__lauren_ai_tool__
print(metadata.parameters["input_schema"])
```

The task item must require only `type` and `task`; `context` is optional. Use
canonical keys and ensure the provider supports arrays of objects and enum
values. The repository has a regression test for this boundary in
`tests/unit/test_lauren_integration_boundaries.py`.

## Test coverage

The subagent tests intentionally use deterministic Lauren mock transports:

| Test | Coverage |
|---|---|
| `tests/unit/test_subagent_pool.py` | registry, role policy, aggregation, validation, pool orchestration |
| `tests/unit/test_subagent_resume.py` | fingerprinting, complete/partial cache behavior, context-sensitive cache keys |
| `tests/unit/test_lauren_integration_boundaries.py` | generated/decorated tool schema boundary |
| `tests/integration/test_subagent_tool_execution.py` | real worker runner, tool dispatch, mutation evidence, Safe approval |
| `tests/integration/test_subagent_executor_integration.py` | workflow-compatible executor role |
| `tests/e2e/test_subagent_executor_e2e.py` | user-facing aggregate contract |

Run the focused suite with:

```bash
./.venv/bin/pytest \
  tests/unit/test_subagent_pool.py \
  tests/unit/test_subagent_resume.py \
  tests/unit/test_lauren_integration_boundaries.py \
  tests/integration/test_subagent_tool_execution.py \
  tests/integration/test_subagent_executor_integration.py \
  tests/e2e/test_subagent_executor_e2e.py -q
```

For a release check, run the repository's full unit, integration, E2E, lint,
format, and type-check commands from `AGENTS.md`.
