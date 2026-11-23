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
| Kernel and scroll payloads, transcript lines, panel activity | Status plus a redacted, bounded preview | TUI/event observability | Bounded with `truncated` markers ([Events](#events)) |

The size policies are normative and stated once, in the
[event contract](#redaction-and-bounds). The last row is intentionally not the source of truth. Seeing a short preview
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

The role describes the assignment and the expected result. Every built-in
role inherits the parent's available tools, including all 56 built-ins when
the parent has the full catalog. If Outlook is unavailable, the other 47
built-ins are available subject to the parent turn's policy. Plugin and MCP
tools follow the same rules. Modes, phases, skills, and explicit worker
permissions can limit the inherited set.

| Role | Intended work |
|---|---|
| `explorer` | Investigate the codebase and report evidence |
| `planner` | Produce a numbered implementation plan from repository facts |
| `implementer` | Make one focused file change and verify it |
| `executor` | Carry out an implementation or build task end to end |
| `tester` | Write and run focused tests |
| `reviewer` | Inspect a change for correctness, security, and regressions |
| `documenter` | Update scoped documentation and report changed files |
| `verifier` | Check an invariant or acceptance criterion against evidence |
| `researcher` | Investigate a question and return findings or write requested notes |

The parent owns session controls such as phase transitions and `ask_user`.
Workers reach it through `ask_parent` and `send_parent_message`. The runtime
builds each nested spawn tool for its owning worker when depth and policy
allow it.

The provider schema's enum contains the registered type names, and the tool
description lists each name with a capability tag and its description:

```text
- explorer [edits files, runs commands]: Explore the codebase and gather evidence.
- reviewer [edits files, runs commands]: Review code for correctness, clarity, security issues, and bugs.
- implementer [edits files, runs commands]: Implement a narrowly scoped code change and verify it.
- implementer [unavailable in this session: implementer needs a file-changing tool but the Plan policy blocks it]: …
```

The tag is computed from the tools the type would actually receive in **this**
session — its allow-list, the parent's visible tools, the type's `deny_tools`
and the Safe/Plan/Yolo policy — so the parent model is never offered an
`implementer` it cannot use. Only tools that change the **workspace** count as
"edits files": `send_email` or `create_event` do not. A task for an unavailable
type is refused before any worker, panel row or provider call, with
`status: "refused"`, the reason in `error`, and the type names in
`unavailable_types`; a mixed pool is refused as a whole so the parent can
re-plan. Names are exact: `research` can be registered as a custom type and is
not an alias for `researcher`.

User and project Markdown agent definitions with `mode: subagent` (or `mode: all`)
are projected into a session-owned registry alongside built-ins and trusted
plugin types. Project definitions win over user definitions. Replacing a built-in
worker role requires `override: true`; invalid or untrusted definitions do not
fall through to a built-in of the same name. See [Agent Library](agent-library.md)
for the format and trust rules.

### Role-specific success rules

A worker's success is decided from what the runtime **observed**, never from its
prose. Each type declares `completion_evidence`:

| Value | A worker succeeds only if it made… | Built-in types |
|---|---|---|
| `none` | no requirement; its text is the product | explorer, planner, reviewer, verifier, researcher, documenter |
| `mutation` | at least one successful workspace-mutating tool call | `implementer` |
| `action` | at least one successful mutating **or** command-running tool call | `executor`, `tester` |

A call counts only if its result is not `ok: false`, and only tools that change
the **workspace** count (`send_email` and `create_event` do not). A claim such as
"I would update the file" fails an `implementer` with
`stop_reason="missing_mutation"` (`missing_action` for the other two), after one
provider call and no hidden retry. A custom type defaults to `none`; a Markdown
definition that overrides a built-in name keeps that built-in's rule.

**A refused change is reported with its cause.** When a mutating or command call
is refused by policy — `approval_denied`, `blocked_by_mode`, `outside_workspace`
or `permission_denied` — and the worker made no successful change, it fails with
`error_code="mutation_refused"` and the message
`write_file was refused (approval_denied); no change was made`, for **every**
type. The refusal is also in `SubagentResult.blocked_calls`. The worker's own
account ("I wrote out.txt") is kept only as partial output.

**A cut-off reply is a failure, and it is saved.** A worker has no turn limit, so
the only cut-off is a reply stopped by the output-token limit, which ends with
`stop_reason="output_truncated"`. It starts no further provider run; the text so
far is partial output. That text is also written to
`.agenthicc/subagent-output/<pool id>/<task id>-attempt<N>.md` under the parent
session's workspace root, and the failure names the path (`error`, and
`SubagentResult.output_file`), so the parent can read it with `read_file` instead
of losing it. The file is never counted as success: the work is still incomplete.
The directory holds its own `.gitignore` (`*`), is never inside a worker's git
worktree (whose files are applied back as changes), and is not cleaned up
automatically. Nothing is saved when the reply was empty, when the session has no
workspace policy, when `.agenthicc` resolves outside the workspace, or when the
write fails; in each case the failure is reported as before.

**Long output goes in a file, before it is cut off.** A worker with a write tool
gets a `## LONG OUTPUT` section in its system prompt naming its reply limit
(`[execution] max_output_tokens`). It says that content too long for one reply — a
large file, a full rewrite, a long report — must be written to a file in the
workspace, checked, and referred to by path in the final response, even when the
task asks for full or verbatim content. Content that fits in one reply is still
returned in the final response. Workers without a permitted write tool do not get
the section; for them the saved partial reply is the safety net.

**An empty answer is not a result.** If a worker returns no final prose, it gets
one finalisation turn. If it is still empty and it changed or ran something, the
tool-call summary (`Executed tool call(s): write_file. Affected path(s): a.txt.`)
is its result, because the work was the product. If it changed nothing, it fails
with `stop_reason="empty_response"` and the tool inventory is kept as partial
output, so a `verifier` with no verdict is never shown as `✓`.

A tool call the model writes as *text* is never executed. Some gateways return
their own call markup in the assistant text when they fail to parse it — for
example DeepSeek's `<｜DSML｜ invoke name="write_file">` after a long `content`
argument — so the file is not written. When a worker's reply holds call markup
for one of its own tools (outside fenced code blocks), it gets one corrective
turn saying the call did not run. If the model writes the call as text again,
the worker fails with `stop_reason` / `error_code` `unexecuted_tool_call` and
the error `the model wrote a write_file call as text instead of calling the
tool; it was not executed`. The markup is never passed to the parent as a
result. Detection lives in `agenthicc.subagents.textual_tool_calls`.

### What the parent sees: the evidence line

A worker's own account ("I updated `src/x.py`") is a claim. Each worker's
section of the digest therefore ends with one deterministic line of what the
runtime **observed** it do, so the parent can check instead of trusting or
redoing the work:

```text
=== implementer #1 (✓ 41.2s) ===
Added the --fast flag and its test.
[evidence: changed src/cli.py, tests/test_cli.py · ran run_tests ×2, grep_files · 7 turns]

=== documenter #1 (✗ write_file was refused (approval_denied); no change was made) ===
[failed: write_file was refused (approval_denied); no change was made]
I wrote docs/guide.md.
[evidence: ran write_file · refused write_file: approval_denied · 2 turns]
```

`changed` lists only paths whose mutating call **succeeded** (at most 5, then
`(+N more)`); a refused or failed write is never a change. `ran` lists tools by
call count, ties by first use (at most 6). `refused` lists calls the runtime
refused by policy, with the error code (at most 3). The line never contains
worker prose, and each item is flattened and bounded to 120 characters. It is
omitted for a worker that ran no tools and answered in one turn, and for one
that never started. A type with a custom `SubagentAggregator` keeps full control
of its own section and gets no evidence line.

The same facts are in the `worker_finished` / `worker_failed` event payload
(`changed_paths`, `blocked_calls`, `turns`) and in the journal's worker record.
Records written before these fields existed simply lack them.

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

## Delegate a workflow

A `spawn_subagents` task can include `workflow` and a string-valued
`params` mapping. Its `type` still supplies the worker's tool policy and
completion evidence. The named workflow must permit worker execution.
An interactive workflow is refused for its own task, and eligible siblings
can run.

Each worker runs with a separate conversation and checkpoint namespace.
Mutating workflows and authoring workflows use their own Git worktree,
including a pool with one such task. The worker returns staged artifacts and
recorded phases. The parent session handles publication under its configured
policy.

## Capability, approval, and workspace boundaries

The pool pins the session's tool generation when it spawns a worker. Workers
spawned after a reload can use the new tools within their role and permission
filters. Each worker's transcript records `tool_surface_generation`.

There are independent tool, capability, and workspace filters:

```text
worker tools = parent delegable tools ∩ explicit type and depth limits − tool denials
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
  Explicit workspace paths are enforced in every mode. These workers use
  tools that can enforce the filesystem scope. Arbitrary commands and
  repository-wide Git operations require a scope that can support them.

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

## Parallel implementation in isolated git worktrees

Delegate independent work in parallel: workers cannot see this conversation, so
put file paths, interfaces and acceptance checks in `task` / `context`, and give
each worker **different files**. Two workers that edit one directory race, and
the last write silently wins — measured against the shared-directory behaviour,
both reported `✓` and one change was gone. So when two or more workers in a pool
can change files and the workspace is inside a git repository, each runs in its
**own git worktree**:

1. The pool snapshots the parent's *current working state* — uncommitted and
   untracked, non-ignored files included — as one commit built through a
   temporary index. The parent's index, `HEAD` and branch are never touched; a
   clean tree snapshots to `HEAD`.
2. Each working worker gets a worktree of that snapshot under
   `~/.agenthicc/worktrees/<repository>/`. Every tool derives its root from the
   task-local workspace policy, so the worker's file tools, commands and git all
   act inside its worktree. Workers with a read-only effective surface stay
   in the original workspace.
3. As each worker finishes, git records what it changed — whichever tool
   changed it, so a file made by `run_bash` counts as evidence and appears in
   `changed_paths` — and the worker's patch is applied to your workspace with
   `git apply`. `git apply` is all-or-nothing and needs no clean tree, so your own
   uncommitted work is preserved and no merge commit is made. Patches apply one
   at a time on top of each other: edits to *different parts* of one file compose,
   and only genuine overlaps conflict.
4. A patch that does not apply makes that worker `ok=False` with
   `error_code="integration_conflict"`; its branch and worktree are **kept** and
   the evidence line says where: `NOT applied: kept on branch agenthicc/… at
   /home/you/.agenthicc/worktrees/…`. When two workers overlap, the one that
   finishes first is applied. A failed worker that changed something is kept the
   same way. Everything else is removed when the pool ends, including on
   cancellation.

`subagents.isolation` (an operator setting, never a model argument) chooses the
behaviour:

| Value | Behaviour |
|---|---|
| `auto` (default) | isolate when ≥ 2 workers can change files and the workspace is in a git repository; otherwise run shared and say so in the digest (`=== Note ===`) |
| `worktree` | isolate even one worker; refuse the pool (`status: "refused"`) when the workspace is not a git repository |
| `shared` | every worker uses the one workspace, as before |

Limits to know about:

- A worktree contains tracked and untracked files but not git-ignored ones, so a
  `.venv` or `node_modules` is absent. A worker that runs tests there uses the
  project's on-demand tooling (for example `uv run`).
- Isolation binds the *workspace policy*, not a sandbox. A worker in Yolo mode
  that is given an absolute path inside your repository can still write there;
  tell workers to use relative paths. Safe mode asks first, and Plan mode denies.
- A type with `permissions.workspace_paths` retains its scoped roots in a
  worktree. Repository-relative roots point into the worker's checkout.
- A retry of a worker (see [Controlling pools](#controlling-pools-from-the-model))
  runs in your workspace, not in a fresh worktree.
- After a crash, `git worktree list` and `git worktree prune` find and clear
  orphaned worktrees; `git branch --list 'agenthicc/*'` lists their branches.

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
| `collect_subagent_results` | main → pool | Read a running pool's snapshot or a finished pool's results |
| `list_subagent_pools` | main | List this conversation's pools with status and worker counts |
| `stop_subagents` | main → pool | Stop one worker (abort) or a whole pool (drain) |
| `retry_subagent` | main → pool | Re-run one failed or cancelled worker of a finished pool |

Parent-directed status updates reach the foreground agent as untrusted worker
data. A foreground spawn includes them in its tool result. A background pool
can deliver an update at the next safe tool boundary or wake an idle direct
session. `collect_subagent_results` also includes the pool's updates.

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

**Subagents have no limits.** A worker may use as many turns, tool calls, model
calls, tokens, minutes and dollars as its task needs, and a pool has no
aggregate ceiling on any of them: no turn limit, no tool-call or iteration
budget, no total-time budget and no cost budget. Each worker may write as much
in one reply as the main agent (`[execution] max_output_tokens`, not
lauren-ai's 4,096-token default). The only bounds left are structural:
`max_workers`, `max_concurrent`, `max_tasks`, `max_active_pools` and
`max_depth`, plus the per-worker `timeout_s` below. Stopping a pool is
something a person or the parent does on purpose (`stop_subagents`, or the
panel), never something the runtime does for you because a counter ran out.

The settings that used to cap a pool — `subagents.max_tool_calls`,
`subagents.max_iterations`, `subagents.max_total_time_s`,
`subagents.max_tokens`, `subagents.max_budget` and
`subagents.on_budget_exceeded` — were removed. A configuration that still sets
one is refused at load and names the key, and `spawn_subagents` no longer takes
a `budget` argument or a type a `max_turns`.

```toml
[subagents]
max_workers = 32
max_concurrent = 4
stop_grace_s = 5.0
```

### Pool usage

Usage is counted and reported, never limited. A worker's provider calls are
recorded in the session usage ledger under a run id that names its pool
(`<parent run>:<pool id>:subagent:<task id>`), so a pool's usage is the sum of
its workers' completed ledger records — the same records the session total is
built from, never a second meter. The tool result and the
`SubagentPoolCompleted` kernel event carry:

- `usage`: `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, `total_tokens` (input plus output), `calls`,
  `cost_usd` and `cost_status`. Cost is summed only over calls with a known
  cost; if any call's cost is unknown, `cost_status` is `unavailable` and
  `cost_usd` is `null`, never `0`.

It holds counts only, never prompt or result text. The panel's summary line
shows workers and their state; it shows no cost or token total.

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
The model may request lower concurrency. A request above the ceiling, or below
1, is refused before any worker starts — no value is clamped in either
direction. The refusal names the ceiling and its configured source
(`max_concurrent_limit`, `max_concurrent_limit_source`) and echoes the refused
value as `max_concurrent_requested`; it carries no `max_concurrent_used`,
because nothing was used. An accepted request reports `max_concurrent` and
`max_concurrent_used`. Task-count refusals report the configured source as well. Pool-count
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

In a direct terminal turn, `spawn_subagents` returns a durable `running` receipt
after the pool starts. The agent can keep working or finish its reply while
workers run under the session's task supervisor. The receipt includes a
`pool_id` and each worker's ID and label. The agent collects the results after
the completion notice arrives.

A completion reaches the agent at its next tool boundary if a turn is active.
If the session is idle, the notice starts an automatic turn. A queued user
message takes priority and the next turn receives the notice with it. The
journal records each completion and its delivery, so a dropped wake can be
recovered. Reopening a session leaves saved notices for the first user turn.

The status line shows running pools and completions awaiting delivery. The
panel shows how long each live pool has gone without a worker event. Completion
and question notices appear as automated events in the live view and
transcript. Esc during an automatic turn pauses further wakes until the next
user submission. Starting or interrupting a wake preserves the draft.
`/usage` reports automatic-turn costs separately and `/perf` counts recovered
wakes.

The notice contains the pool ID, status and worker counts. Worker output
reaches the model in the result of `collect_subagent_results(pool_id)`. Approval
requests surface in the main session with the worker's name.

### Foreground pools

Set `subagents.spawn_mode = "foreground"` to have the spawn wait for its
aggregate. Workflow phases and runners that lack a supervised notice path
also wait. In the terminal, Ctrl+B detaches an active foreground pool and
returns a `backgrounded` receipt. Esc cancels a foreground pool.

### Worker questions

A background worker's `ask_parent` follows `subagents.on_clarification`.
`queue` keeps the question open and sends a notice through the same delivery
paths as completion. The parent reads the question via
`collect_subagent_results` and resumes the worker with `answer_subagent`.
`fail` ends the wait immediately and asks the worker to state its assumptions.
The default is `queue` in the terminal and `fail` in other runners.

### Controlling pools from the model

- A bounded `collect_subagent_results(wait=True)` ends early when a user
  message or notice is waiting. Check `interrupted` in the running snapshot.
- `list_subagent_pools()` lists the pools of this conversation: live handles
  and, for the root conversation, every pool folded from the journal (including
  pools from earlier turns), each with `status`, `background`, `total`,
  `succeeded` and `failed`.
- `stop_subagents(pool_id, worker_id=None, mode=None, reason="")` stops one
  worker (default mode `abort`) or the whole pool (default `drain`). The stop
  is recorded with source `parent_model`; repeating it returns the same
  request id. A pool drain also returns `not_started`, the workers that were
  still pending and will never start. A finished pool answers `the pool is not
  running`.
- `retry_subagent(pool_id, worker_id)` re-runs a failed or cancelled worker of
  a finished pool as a new attempt with the original type, model, policy,
  pool scope and depth. It refuses a pool still running, a retry that would exceed
  `subagents.max_active_pools` (a retry reactivates its pool), a worker that
  succeeded or never started, and an unknown worker, each with its reason.

Retryability does not depend on which pool the TUI panel shows: the
conversation registry keeps every live pool and the 32 most recent finished
ones. A nested worker's control tools see only the pools it spawned itself
(its ownership lineage), never its parent's or siblings' pools.

### Collecting a pool

`collect_subagent_results(pool_id, wait=False, timeout_s=None)` reads a pool
without blocking unless asked to wait:

- A running pool returns `status: "running"`, one row per worker (`worker_id`,
  `label`, `type`, `status`, `attempt`, `elapsed_s`) and any question a worker
  is waiting on (`pending_questions`).
- `wait=true` waits up to `timeout_s` seconds, capped by
  `subagents.collect_wait_max_s` (default 60, at most 3600). A pool still
  running then returns the snapshot with `timed_out: true`; it never raises.
- A finished pool returns its results — `status` (`completed`),
  counts, `results`, `values`, `usage` — identically on every call.
- A pool with no live handle — finished in an earlier turn, or left by a
  process that restarted — is read from this conversation's journal, including
  `interrupted` pools with their preserved worker output. A pool id from
  another conversation is refused.

The worker text therefore reaches the parent only as the output of a tool it
called, as it does for a foreground pool.

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
Enter opens its transcript and Esc returns to the prompt. Earlier pools come
from the conversation journal, with each pool's status (`completed`,
`cancelled`, `interrupted`) in its header, so a restored session
lists a pool a crash interrupted even when it left no transcripts; the
transcripts add each worker's activity. In a transcript, ←
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

Every subagent emission is one `SubagentEvent`
(`agenthicc.subagents.events`), and each sink records a projection of it: the
kernel event, the conversation-store scroll event, the worker transcript line
and the panel row. `LIFECYCLE_TABLE` is the normative map; no sink records a
state the table does not name:

| Transition (`event`) | Kernel event | Scroll kind | Transcript kind | Panel status |
|---|---|---|---|---|
| `pool_opened` | `SubagentPoolStarted` | `subagent_pool_started` | — (`pool.json`) | — |
| `worker_scheduled` (a retry) | — | — | `attempt_started` | pending |
| `worker_started` | `SubagentStarted` | — | `prompt` | running |
| `worker_text` | — | — | `text` | running |
| `tool_started` / `tool_finished` | — | — | `tool_started` / `tool_finished` | running |
| `approval_requested` | — | — | `approval_requested` | waiting |
| `approval_resolved` | — | — | `approval_resolved` | running |
| `stop_requested` | — | `subagent_worker_stop_requested` | `stop_requested` | — |
| `message_acknowledged` | — | — | `message_acknowledged` | — |
| `worker_finished` | `SubagentCompleted` | `subagent_worker_done` | `finished` | done |
| `worker_failed` | `SubagentFailed` | `subagent_worker_failed` | `finished` | failed, stopped |
| `pool_finished` | `SubagentPoolCompleted` | `subagent_pool_done` | — (`pool.json`) | — |
| `transcript_degraded` | — | `subagent_transcript_degraded` | — | — |

A transcript's `truncated` line marks the file itself, not a transition.

Every kernel and scroll payload carries the same envelope: `schema_version`
(1), `event_id`, `at`, `event`, `pool_id`, and — on every worker-scoped event —
`worker_id` and `attempt`, plus `session_id` and `run_id` (the worker's ledger
run id) when the pool has them. Kernel events also carry `schema_version` on
the event itself, declared once in `agenthicc.kernel.events.EVENT_SCHEMA_VERSIONS`.
Transcript lines carry `schema_version`, `pool_id` and `worker_id`, so a copied
`.jsonl` file is still attributable. A success and a failure are distinct kinds
(`subagent_worker_done`, `subagent_worker_failed`) whose payloads differ only in
`event` and `ok`.

The pool drains the processor before returning the aggregate, so every emitted
pool and worker event has been processed and handed to the event log before the
parent receives the tool result. `subagent_pool_result` stores the complete
aggregate in the reactive turn and durable journal, but has no verbose renderer
because the parent tool result is the user-facing artefact.

#### Redaction and bounds

Every text field passes through the session-export redactor before it is cut
to its display bound: the worker's answer (`text`), its `error`, and the
parent's task text (`task` in `SubagentStarted` and in each
`subagent_pool_started` worker row). Redaction runs on the complete text, so a
secret that straddles the bound is masked whole, and it is idempotent, so text
the pool redacted is unchanged when the transcript writer redacts it again. A
cut value says so: the main text carries `truncated: true` and its untruncated
`text_length`, and any other field carries `<field>_truncated` and
`<field>_length`. A value that fit carries neither.

| Field | Bound (characters) |
|---|---:|
| worker answer (`text`) | 2,000 |
| `error` | 1,000 |
| task text (`task`) | 2,000 |
| pool aggregate (`SubagentPoolCompleted.text`) | 16,000 |
| transcript text (`prompt`, `text`, `finished`) | 4,000 |

| Representation | Where | Size policy |
|---|---|---|
| Complete result | `spawn_subagents` / `collect_subagent_results` tool result | unbounded, redacted structured values |
| Complete journal record | `subagent_worker_result` and pool lifecycle records | unbounded, durable |
| Bounded preview | kernel and scroll payloads, transcript lines, panel activity | redacted, then cut, with markers |

#### Diagnosing a pool

`SubagentPoolState.diagnostics(now)` derives, from fields that already exist and
with no new meter: each worker's `elapsed_s` and `timeout_ratio`, the pool's
`wall_clock_s` (how long it has run — a measurement, not a limit),
`success_rate_by_type`, `stuck_workers` — workers `running` past 80 %
(`STUCK_TIMEOUT_FRACTION`) of their time limit; a `waiting` worker is not stuck —
and `transcript_write_failures`. `stuck_workers` and `transcript_write_failures`
also appear on `SubagentPoolCompleted` and `subagent_pool_done`.

A transcript write that fails (a full disk, for example) does not fail the
worker, but it is counted, and the pool emits one `subagent_transcript_degraded`
scroll event so a partial transcript is labelled partial.

`agenthicc.subagents.replay.replay_pool(pool_id, journal=..., transcripts_root=...)`
merges a pool's transcripts with its journal records into one ordered
timeline. Its fidelity, stated as `REPLAY_FIDELITY`: kernel subagent events are
projection-only and never stored, so they cannot contribute, and the timeline
replays what happened without re-deriving pool state. `partial` is true when a
transcript was compacted.

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

Read `stop_reason` and `error_code`. `missing_mutation` means the model never
made a successful mutating call (it described the change instead).
`mutation_refused` means it tried and the runtime refused: the message names the
tool and the code (`approval_denied` — a person or broker said no;
`blocked_by_mode` — Plan mode; `outside_workspace`). `output_truncated` means
the reply was cut off at the output-token limit. The `blocked_calls` field lists each
refusal.

### A task is refused with `unavailable_types`

A type that must change files (`implementer`) or act (`executor`, `tester`) is
refused when the session has no usable tool that could do so — for example
`implementer needs a file-changing tool but the Plan policy blocks it`. The
refusal happens at admission, before any worker or provider call, and the type
is tagged `unavailable in this session` in the tool description. Choose a
mode or policy that permits investigation, or run the implementation phase with the
intended mode and tools exposed.

### Safe mode does not show an approval

The child must be created from the normal session turn path so the parent
approval service is passed into `make_spawn_subagents_tool`. Test-only callers
that provide `AppState` but no approval service have no service to display a
prompt. Headless callers should supply their explicit approval adapter.

### A worker "succeeded" but the file it wrote is missing

Check the worker's `tool_calls`: a write that ran is listed there and in
`changed_paths`. A reply that shows `write_file` markup as text means the
provider leaked its call format; the worker now gets one correction and fails
with `unexecuted_tool_call` if it repeats it (see
[Role-specific success rules](#role-specific-success-rules)). Also check that
the parent mode exposes the write tool: in Plan mode no worker receives one.

### Two workers edited the same file

With `subagents.isolation = "shared"`, or in a directory that is not a git
repository, workers share one workspace and the last write wins — the digest
says `=== Note ===` when isolation was wanted but unavailable. In a repository
each worker has its own worktree: the first to finish is applied and the other
is `integration_conflict`, with its work kept on the branch the evidence line
names. Re-run that task against the new state, or inspect the kept worktree.

### Orphaned worktrees

A pool removes its worktrees when it ends. Only a crash can leave one behind.
`git worktree list` shows them, `git worktree prune` clears the ones whose
directories are gone, and `git branch --list 'agenthicc/*'` lists their
branches. A worktree named in a worker's `NOT applied` evidence line is kept on
purpose; remove it with `git worktree remove <path>` and `git branch -D <branch>`
when you no longer need it.

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
