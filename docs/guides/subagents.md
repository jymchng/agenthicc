# Subagents and `spawn_subagents`

`spawn_subagents` is the session-wide delegation tool. It lets the current
agent explicitly fan out independent pieces of work to typed workers, wait for
all of them, and receive one complete text digest. It is normally synchronous
from the parent agent's point of view. If a worker needs a decision, the pool
returns a structured clarification request; the parent answers it through a
continuation tool and then collects the still-running pool.
The TUI and kernel projections still use bounded previews so a long chapter or
source file does not make the screen unusable.

This guide describes the implementation in the current source tree. The
authoritative implementation is split across:

| Concern | Source |
|---|---|
| Provider-facing tool and input validation | `src/agenthicc/subagents/tool.py` |
| Worker lifecycle, filtering, retries, aggregation, and events | `src/agenthicc/subagents/pool.py` |
| Immutable effective-mode policy snapshots | `src/agenthicc/subagents/policy.py` |
| Parent/child/peer communication broker and tools | `src/agenthicc/subagents/communication.py` |
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
    │      workspace policy, and immutable effective-mode snapshot
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
  - resolves the registered type name exactly, including custom `research` types
  - validates type, task text, and timeout
           │
           ├─ matching pool lifecycle for this parent run + fingerprint?
           │       yes → return exact receipt, terminal result, or in_progress
           │       no  → check complete-result cache, then construct SubagentPool
           │
           ├─ creates a per-pool message broker and continuation handle
           │       child questions return awaiting_clarification
           │       parent answers through answer_subagent
           │       collect_subagent_results resumes the same pool
           │
           ▼
SubagentPool.run()
  ├─ persists pool identity before workers can perform side effects
  ├─ creates one isolated SubagentWorker per task
  ├─ bounds active workers with an asyncio semaphore
  ├─ emits kernel and ConversationStore progress events
  └─ gathers workers in input order, regardless of finish order
           │
           ▼
each SubagentWorker
  ├─ intersects role allow-list with the parent's already-filtered tools
  ├─ intersects that ceiling with the immutable effective-mode snapshot
  ├─ resolves model, effort, and context from call/type/operator/parent policy
  ├─ builds fresh ShortTermMemory with context clamped to the model window
  ├─ builds role prompt + inherited policy/communication instructions
  │      + optional [ADDITIONAL CONTEXT]
  ├─ adds the final-response contract to the role prompt
  ├─ installs child-local capability and approval/workspace gates
  ├─ installs bounded parent/peer communication tools
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

The communication broker is the controlled exception to message isolation. It
passes only bounded, typed envelopes between authenticated members of one pool;
it never merges private transcripts or grants capabilities. Large findings
must be written as authorized artifacts and passed by validated path/reference.

There are therefore three different representations of a subagent result:

| Representation | Contains | Purpose | Size policy |
|---|---|---|---|
| `SubagentResult.text` / `AggregatedResult.text` | Complete final worker prose or the rendered JSON result | Parent tool result and provider memory | Not truncated by the pool |
| `SubagentResult.value` / `spawn_subagents.values` | Validated JSON object envelopes (`task_id`, `agent_type`, `label`, `value`) | Structured parent handoff and resume | Each submitted value is limited to 64 KiB |
| `ConversationJournal` records | Complete worker prose, structured values and complete-pool results | Crash recovery and durable resume | Fsync'd; subject to normal session-storage sensitivity |
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
delegate. No built-in worker role includes the tool, and the default
`subagents.max_depth = 1` withholds it from every worker. A custom type may opt
in by declaring `spawn_subagents` in its own allow-list, but only when the
operator raises `max_depth`; the tool is still withheld at the configured limit.

The provider-facing signature is:

```python
spawn_subagents(
    tasks: list[Task],
    max_concurrent: int = 4,
    timeout_s: float = 3600.0,
    model: str | None = None,  # operator-gated
    effort: str | None = None,  # operator-gated
) -> dict[str, object]
```

`model` and `effort` are omitted from the provider schema unless the operator
sets `subagents.allow_call_overrides = true`. When enabled, these per-call
values take precedence over type and session defaults; model routing still uses
the active provider and `subagents.allowed_models` ceiling. Otherwise a supplied
value is rejected at the tool boundary (see [configuration](configuration.md#subagents)).

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

The provider schema's enum contains the registered type names, and the tool
description lists each name with its description. Names are exact: `research` can
be registered as a custom type and is not an alias for `researcher`.

User and project Markdown agent definitions with `mode: subagent` (or `mode: all`)
are projected into a session-owned registry alongside built-ins and trusted
plugin types. Project definitions win over user definitions. Replacing a built-in
worker role requires `override: true`; invalid or untrusted definitions do not
fall through to a built-in of the same name. See [Agent Library](agent-library.md)
for the format and trust rules.

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

### Structured result contracts

A subagent definition may declare an `output:` JSON Schema object (see the
[Agent Library format](agent-library.md#markdown-format)). Only contract types
receive the `submit_subagent_result` tool, and that tool's provider input schema
is the declared contract. A valid call must submit an object satisfying the
schema; the runtime bounds and projects it to JSON, validates it, and renders
stable JSON text for `results`. The response also contains `values`, an ordered
list of `{task_id, agent_type, label, value}` envelopes, so callers can inspect
the submitted objects without parsing rendered prose.

The schema is one compact JSON object on the `output:` frontmatter line. It is
limited to 16 KiB and a bounded, local Draft 2020-12 subset; `$ref` and other
unsupported keywords are rejected when the definition loads. Secret-shaped
property names are refused. Each submitted value is limited to 64 KiB, bounded
by nesting depth and node count, and must be JSON-serializable. Submitted values
pass through the same redaction rules as other tool and transcript payloads
before they reach the parent or durable journal. A type without `output:` keeps
the existing prose/fallback behavior.

A contract worker that omits submission or submits an invalid value fails; it
cannot succeed through final prose or the ordinary tool-call-summary fallback.
Validation failures identify the field path and are journaled with a typed
error code. Projector and renderer failures are also contained as failed worker
results. The effective schema is part of the pool fingerprint, and cached values
are revalidated against the active registered schema before reuse.

## Capability, approval, and workspace boundaries

There are independent tool, capability, and workspace filters:

```text
worker tools = parent-visible tools ∩ role.allowed_tools − type.permissions.deny_tools
worker capabilities = parent mode restrictions ∪ type.permissions.deny_capabilities
worker workspace = inherited workspace roots ∩ type.permissions.workspace_paths
```

A Markdown agent definition can declare `permissions` as documented in the
[Agent Library guide](agent-library.md#markdown-format). Every field only
narrows the inherited policy:

- Tool deny patterns are checked after the parent and role allow-lists, so a
  deny always wins, including for matching tools introduced later.
- Capability denials are hard blocks in the child `RuntimeMode`; an error names
  the type and `permissions.deny_capabilities` constraint and does not suggest
  changing to a wider mode.
- `require_approval` adds child-local approval requirements without changing
  sibling types. A capability already blocked by the inherited mode remains
  blocked rather than becoming approvable; if an explicitly required approval
  has no approval service, worker construction fails closed.
- Workspace paths are canonicalized and checked as subsets both when the
  definition registry is built and again when a worker is constructed. A
  missing inherited workspace scope or out-of-scope path fails closed; an empty
  declared list gives the worker no workspace roots.

The parent-visible list has already been restricted by the active workflow
phase and runtime mode. Each worker then installs `ToolCapabilityGate` from an
immutable copy of the effective mode. Consequently:

- Plan mode hard-blocks write, execute, git-write, network, control, and
  undeclared capabilities;
- Safe mode permits reads but requires approval for restricted capabilities;
- Yolo mode does not require per-call capability approval unless the type adds
  `require_approval` for a capability;
- a worker cannot add a tool by changing its task text or context;
- no built-in role can delegate, and a custom role must explicitly allow
  `spawn_subagents` while the operator has enabled a greater `max_depth`;
- even an opted-in worker cannot see the delegation tool at the depth limit, and
  direct child-pool construction independently refuses a depth over that limit.

When the parent has an approval service, the child also installs
`ApprovalGate` with the parent's approval service and an isolated policy bound
to the same workspace scope. Safe-mode approvals therefore pause the same TUI
approval request path instead of being silently bypassed by the nested runner.
Plan hard blocks remain hard blocks. In headless operation, approval behaviour
depends on the approval service supplied by the headless session; the absence
of an approval service does not create a new approval UI.

This is a ceiling, not a grant. A role can still fail because the parent phase
did not expose a required tool, because the mode blocks it, because workspace
authorization denies the path, or because the user denies approval. The
returned aggregate preserves those failures for the parent to reason about.

## Memory and provider calls

Each worker gets fresh `ShortTermMemory` sized by the effective context budget.
The routing precedence is:

| Setting | Precedence (highest first) | Default / special case |
|---|---|---|
| Model | Enabled per-call override → type `model:` → `subagents.default_model` → parent model | Omitted type model can use the operator default; `model: inherit` explicitly selects the parent and bypasses that default |
| Effort | Enabled per-call override → type `effort:` → `subagents.default_effort` → inherited provider effort | No explicit effort adds no effort option; unrelated inherited provider options are preserved |
| Context | Type `context:` → `subagents.default_context` → legacy default | Defaults to 8,000 tokens; the selected model's window clamps larger budgets |

`[memory.context_windows]` supplies model-specific windows and a fallback for
unknown model IDs. The worker's `AgentConfig` receives that context window as a
pre-send guard where the provider runner supports it. Model identifiers and
provider-qualified names are checked against `subagents.allowed_models` when
configured; a type cannot switch the worker to a different provider endpoint.
Effort values are `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`. If a
provider rejects an effort setting as unsupported, the worker warns and retries
once without the effort option, retaining the other request options.

The worker's system prompt is:

```text
<role system prompt>

## INHERITED RUNTIME POLICY
<effective Safe, Plan, or Yolo instructions>

## AGENT COMMUNICATION
<ask_parent, send_parent_message, peer-mail, and untrusted-message rules>

[ADDITIONAL CONTEXT]
<task.context, when supplied>
```

The task description is sent as the worker's user message. This means context
should contain concise, relevant facts—not a full transcript. If the parent
needs to provide a large artifact, prefer a path and ask an explorer or
researcher to read it.

Workers use the parent's transport and the resolved model, and receive the
session's provider options such as temperature, top-p, completion limit, and
request options. They call `AgentRunnerBase.run()` directly, so the pool
explicitly wraps that path with the same snapshot/rollback transport retry
helper used by the main turn runner. Nullable provider usage fields are
normalized before Lauren's token arithmetic. Each worker gets its own
usage-ledger run ID under the parent run when accounting is enabled; its record
names the provider and model actually routed to that worker.

The parent receives only one aggregate tool result. Individual worker message
histories are not appended to the parent prompt. This keeps parent context
growth proportional to the bounded digest rather than to every worker turn.

## Parent, child, and peer communication

Each pool owns an `AgentMessageBroker` with authenticated mailboxes. The
available control-plane operations are:

| Operation | Direction | Purpose |
|---|---|---|
| `send_parent_message` | child → main | Send a bounded finding/status/handoff |
| `ask_parent` | child → main | Ask for a missing decision or requirement |
| `answer_subagent` | main → child | Resolve a correlated child question |
| `send_subagent_message` | main → child | Send a bounded instruction at a safe turn boundary |
| `poll_agent_messages` | child | Read ordered parent/peer mail |
| `send_peer_message` | child → child | Coordinate within the same pool |
| `ask_peer` / `answer_peer` | child ↔ child | Correlated peer clarification |
| `collect_subagent_results` | main → pool | Collect a pool paused for clarification |

The clarification flow is deliberately non-reentrant:

```text
child ask_parent
  → durable question + pending pool result
  → spawn_subagents returns awaiting_clarification
  → main calls answer_subagent(pool_id, question_id, answer)
  → same child memory resumes
  → main calls collect_subagent_results(pool_id)
```

The parent provider is never invoked recursively while its tool call is on the
stack. Messages are untrusted context and cannot switch modes, approve tools,
change workspace scope, or route outside the current pool. Pending questions
have deadlines and explicit cancelled, expired, or orphaned outcomes. Parent and
peer messages are acknowledged only after the receiving worker completes a
provider turn successfully. A worker's outstanding message count is captured
when it finishes; a failed or cancelled turn does not acknowledge mail it did
not successfully consume.

## Concurrency, timeout, and failure semantics

`max_concurrent` controls an asyncio semaphore and defaults to the configured
`subagents.max_concurrent` (four by default). The tool rejects a requested
concurrency above that ceiling and never starts more than the effective limit
at once. The number of task entries is separately bounded by
`subagents.max_workers` (32 by default). Results are collected in the original
task order even if workers finish in a different order.

The pool also enforces aggregate budgets across all workers:

| Setting | Default | Counts |
|---|---:|---|
| `subagents.max_tool_calls` | 256 | Tool calls started by workers |
| `subagents.max_iterations` | 128 | Provider model calls started by workers |
| `subagents.max_tokens` | 200,000 | Input plus output tokens reported by worker model calls |
| `subagents.max_total_time_s` | 600 seconds | Elapsed wall-clock time before the pool budget action triggers |

Each count may reach its configured limit; the pool crosses the budget when the
next event would exceed it. The first exceeded budget becomes the pool's
`budget_exceeded_reason`. `on_budget_exceeded = "drain"` (the default) lets
running workers finish and prevents queued workers from starting; `"abort"`
cancels running workers, subject to `stop_grace_s`. A budget-limited aggregate
is reported with `status: "budget_exceeded"` and is never written as a
successful resume-cache result, even if all workers happened to finish
successfully during drain. The durable pool journal records it as cancelled,
and the pool completion event and TUI summary include the reason. The wall-clock
limit is a stop threshold, not a hard completion deadline:
`drain` can take longer while active workers wind down, and `abort` may use the
configured cleanup grace period.

These values can be set in `agenthicc.toml`:

```toml
[subagents]
max_workers = 32
max_concurrent = 4
max_tool_calls = 256
max_iterations = 128
max_tokens = 200000
max_total_time_s = 600.0
on_budget_exceeded = "drain" # abort | drain
stop_grace_s = 5.0
```

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

Worker stops use explicit, idempotent `abort` or `drain` requests. `abort`
cancels a running worker immediately and preserves text from its last completed
provider turn as a partial result. `subagents.stop_grace_s` bounds how long the
pool waits for cancellation cleanup; an overrun is recorded as a cleanup error.
`drain` lets running workers finish and prevents queued workers from starting.
Not-started workers are named in the aggregate but excluded from its `total`
and `failed` counts. Parent cancellation requests an abort for the pool, waits
for worker cleanup, then propagates cancellation.

After the pool finishes, `r` on a failed or cancelled worker starts a fresh
attempt for that worker only. Successful siblings are not rerun, and prior
attempt results remain in lifecycle history and the worker transcript. The
retry uses the same task and inherited runtime policy; it starts with a fresh
communication broker because the prior pool's peers are no longer running.

## Depth and fan-out governance

The top-level pool is depth 1. `subagents.max_depth` defaults to `1`, preserving
the current flat behavior: built-in roles do not declare `spawn_subagents`, and
the worker withholds the tool at the limit even if a role explicitly allows it.
To opt a custom type into bounded nesting, raise `max_depth` and add
`spawn_subagents` to that type's `allowed_tools`. The direct pool constructor
also refuses any child depth above the configured maximum, so tool visibility is
not the only guard. Refusals include current depth, limit, and the setting to
change.

The default fan-out settings are finite:

| Setting | Default | Enforcement |
|---|---:|---|
| `subagents.max_tasks` | 64 | Rejects an oversized task list before constructing workers |
| `subagents.max_concurrent` | 16 | Hard ceiling; the tool's omitted argument still requests 4 |
| `subagents.max_active_pools` | 16 | Process-wide limit on pools admitted at once |
| `subagents.max_depth` | 1 | Worker delegation is withheld at the cap |

`[subagents.depth_limits."N"]` can override `max_tasks` and
`max_concurrent` independently for a depth. A depth with no value inherits that
ceiling from its nearest configured ancestor, or from the top-level setting.
The model may request lower concurrency; a request above the ceiling is clamped,
and the parent-facing result reports the actual concurrency and its limit
source. Task-count refusals report the configured source as well. Pool-count
refusals name the pools already running. A refused pool is not recorded as
started in the durable journal.

A pool carries a bounded ancestry of `(type, task)` signatures. If a child
repeats one of its ancestor's tasks, admission is refused with the full bounded
chain rather than relying on a timeout. `WorkerState` and `SubagentPoolState`
carry depth and parent-pool identity; nested panel summaries show the depth,
while depth-1 summaries remain unchanged.

Depth tiers can further narrow a role without changing its definition:

```toml
[subagents.depth_tiers."2".reviewer]
allowed_tools = ["read_file", "search_files"]
allowed_models = ["model-id"]
```

A tier's tool set must stay within the type's declared tools and any earlier
tier. Model tiers require a finite ceiling from the type's fixed model or
`subagents.allowed_models`, and may only narrow that ceiling. Unknown types and
widening profiles are rejected when the spawn tool is constructed. These tables
are pool settings, not additional Markdown agent-definition keys.

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
order-insensitive hash of every task's type, task text, context, selected type
policy revision, and inherited parent policy revision:

```text
(agent_type, task_description, context, inherited_policy_revision, type_policy_revision)
```

It also includes each referenced type's effective routing policy: provider,
model, effort, context budget and model window, parent model, operator defaults,
allowlist, call-override setting, and permission constraints. Task order does not
matter; changing task text, role, context, inherited policy, routing, or a type's
permissions invalidates the cached result. The policy inclusion prevents a result
created under weaker mode or permission constraints from being replayed after a
policy change.

Only a pool with `failed == 0` is cached. Partial results are deliberately not
treated as successful cache entries. A cached call returns `pool_id: "cached"`,
does not call the provider, and appends a small “Resumed” system event for the
TUI. Every worker result—including failed and timed-out results—is retained in
the journal for diagnostics. The system does not replay five completed workers
and rerun only three from an interrupted eight-worker pool; a later, new parent
turn that requests the same tasks starts a new pool as a whole.

A separate lifecycle receipt covers replay of the *same logical parent run*.
The pool start and task fingerprint are journaled before workers can perform
side effects. Replaying that tool call returns its exact durable background
receipt, its terminal aggregate, or an `in_progress` response; it never starts
a duplicate pool. If the process crashes, resume marks unfinished workers
`interrupted` and preserves results already written. It does not automatically
rerun interrupted work. A graceful session shutdown records `cancelled` instead.

### Background pools

While a pool is running, Ctrl+B in the composer backgrounds it. The parent
receives a durable `backgrounded` tool receipt immediately; workers continue
under the session's `SupervisedTasks` owner, not as detached process tasks. The
session cancels them during shutdown. Ctrl+B is offered only while the parent
turn is streaming and at least one subagent worker is active.

A completed, cancelled, or crash-interrupted background pool is delivered as a
synthetic user message at the next logical-turn boundary. It is not inserted
into an already-running provider loop. Stable message IDs make delivery
idempotent across resume and compaction, so a completion is not delivered twice.

## TUI and kernel observability

### The subagent panel

While a pool runs, the live block shows a distinct subagent panel below the
composer's bordered input area and above the footer (AGX-08). The summary is
derived from the current immutable pool snapshot; zero categories are omitted,
approvals are distinct from queued work, and the concurrency limit appears only
when it explains queued workers:

```text
  Subagents · 1 needs input · 1 running · 1 queued · 1/4 done · limit 2  Ctrl+O details
  ● Explorer 1  Read src/agenthicc/background/store.py                 0:42  3.1k
  ◆ Executor 1  Needs approval: run tests/unit/test_store.py            1:05
  ✓ Reviewer 1  3 findings in the scheduler                            0:31
```

Each row has one normalized worker identity and a readable activity. At 64
columns and wider, elapsed time and compact token counts are shown when they fit;
otherwise metrics yield before identity and activity. At 40–63 columns, rows keep
status, identity and an activity prefix but omit metrics. Below 40 columns, the
summary and one selected/attention row remain; a terminal shorter than 24 rows
shows the summary alone. `Ctrl+O` or `/subagents` still exposes every worker,
full task, exact elapsed time and exact token count. Past six rows the live list
collapses the rest into `… +N more`.

Status is not conveyed by color alone. Screen-reader rows use plain sentences
such as `waiting for approval, Executor 1, Needs approval: run tests`; queued,
failed and stopped states are also spoken explicitly. Finished rows stay 30
seconds and then fold into the summary; failed rows stay until dismissed.
Everything shown is redacted with the session-export redactor.

The scrollback start announcement prints one header and at most three task
previews; larger pools end with `+N more · Ctrl+O details`. The overlay remains
the source for full tasks. Completion reports successful progress and any
non-success count. The current completion event combines failed and stopped
workers in its `failed` aggregate, so scrollback honestly labels that aggregate
`unsuccessful (failed or stopped)` rather than inventing separate counts. The
active panel still distinguishes failed from stopped using each worker snapshot.

`ConversationStore.subagent_pool_state` holds an immutable `SubagentPoolState`
snapshot that the pool replaces on every change, so subscribers hear about each
one. A finished pool stays published (with `finished_at`) while the panel still
lists rows; the store's tick clears it afterwards.

### Focus, transcripts, stopping and backgrounding

Ctrl+O (or `/subagents`) moves focus from the prompt to the panel: ↑/↓ select
a worker — the session's earlier pools are listed below the current one —,
Enter opens its transcript and Esc returns to the prompt. In a transcript, ←
and → move to its siblings, `f` follows new events, and Esc (or ↑ at the top)
returns to the conversation. The keys are the key map's `subagents` and
`transcript` contexts, so `~/.agenthicc/keybindings.toml` can rebind them. In
a session, the agents hub's Running tab also lists the turn's workers, and
Enter on one opens its transcript.

`x` aborts the selected worker, in the panel or its transcript. Only that
worker is cancelled: its completed provider-turn output is preserved as a
partial result, the other workers continue, and the parent receives the pool
aggregate. On a failed row, `x` dismisses it instead; after the pool finishes,
`r` retries a failed or cancelled row without rerunning its siblings.
`SubagentPool.request_stop()` and `SubagentPool.request_pool_stop()` expose the
idempotent lifecycle requests; `stop_worker(worker_id)` remains the panel's
abort shorthand, and `active_pool(pool_id)` finds a running pool.

Ctrl+B in the composer backgrounds the active pool and immediately returns a
durable receipt to the parent. The pool remains owned by the session and is
cancelled when that session shuts down. Its terminal result is delivered to the
provider conversation as a synthetic user message at the next logical-turn
boundary, never inside a provider loop already in progress.

Each worker's transcript is
`~/.agenthicc/sessions/<session-id>/subagents/<pool-id>/<worker-id>.jsonl`,
with the pool's workers in `pool.json` beside them: the task, the agent's
messages, each tool call's start and result, approval requests and decisions,
and the final result, one JSON line each and redacted. A background thread
writes them in order; a transcript is kept under 2 MiB by dropping its oldest
events behind a `truncated` marker, and it is deleted with the session. The
parent agent never reads them, so its provider requests are byte-identical
with and without them.

### Approvals name the worker

A worker's approval prompt says who asks — `executor #1 (executor) wants to
run bash` — and `o` opens that worker's transcript before you decide; closing
it returns to the prompt. Esc denies that single call without stopping the
worker. `ApprovalRequest.requester` carries an `ApprovalRequester` (label,
role, pool and worker IDs); a background session records it in the inbox item,
so the agents hub's request card shows it too.

### Events

The pool also appends scroll events:

- `subagent_pool_started`;
- `subagent_worker_done` for each worker, including a bounded display summary, duration,
  tool calls, and changed-path hints;
- `subagent_pool_done`;
- `system` for a cached “Resumed” result.

When an `EventProcessor` is supplied, corresponding kernel events are emitted:
`SubagentPoolStarted`, `SubagentStarted`, `SubagentCompleted` or
`SubagentFailed`, and `SubagentPoolCompleted`. The pool drains the processor
before returning the aggregate, so every emitted pool and worker event has been
processed and handed to the event log before the parent receives the tool result.
Event payloads contain bounded text and diagnostics; they are not a replacement
for the complete result or the durable journal. `subagent_pool_result` stores
the complete aggregate in the reactive turn and durable journal, but has no
verbose renderer because the parent tool result is the user-facing artefact.

## Custom roles and aggregators

Register a role with a fresh registry and pass that registry to the factory:

```python
from agenthicc.subagents import SubagentTypeRegistry, SubagentTypeSpec

registry = SubagentTypeRegistry()
registry.register(
    SubagentTypeSpec(
        name="api_reviewer",
        description="Reviews API changes for compatibility and security.",
        allowed_tools=frozenset({"read_file", "search_files", "grep_files"}),
        max_turns=6,
        system_prompt=(
            "Review API changes for compatibility and security. "
            "End with APPROVED or NEEDS CHANGES and evidence."
        ),
    )
)
```

For repository plugins, export `SUBAGENT_TYPES` from a Python file under
`.agenthicc/tools/` or `~/.agenthicc/tools/`. Session discovery registers those
specs into that session's registry, rather than mutating a process-wide registry.
Direct plugin code that registers into `DEFAULT_REGISTRY` remains a deprecated
compatibility path; sessions snapshot registrations made before construction,
so registering later does not change an active session. The runtime registry is
built from built-ins, plugin types, and discovered Markdown agent definitions.

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

Check role, task, and context. The effective type schema and routing policy are
also part of the fingerprint. The cache is intentionally order-insensitive and
only stores complete pools; structured values are revalidated before reuse. A
pre-fix cache record using the old role/task-only fingerprint will not match the
new key and will be safely re-executed.

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
| `tests/integration/test_subagent_panel_agx08.py` | live rows, transcripts, stopping one worker, named approvals, provider requests unchanged by the panel |
| `tests/unit/test_subagent_panel_agx08.py` | transcript bounding and redaction, panel rows, the focused panel, transcripts, the approval prompt |

Run the focused suite with:

```bash
./.venv/bin/pytest \
  tests/unit/test_subagent_pool.py \
  tests/unit/test_subagent_resume.py \
  tests/unit/test_lauren_integration_boundaries.py \
  tests/integration/test_subagent_tool_execution.py \
  tests/integration/test_subagent_executor_integration.py \
  tests/integration/test_subagent_panel_agx08.py \
  tests/unit/test_subagent_panel_agx08.py \
  tests/e2e/test_subagent_executor_e2e.py -q
```

For a release check, run the repository's full unit, integration, E2E, lint,
format, and type-check commands from `AGENTS.md`.
