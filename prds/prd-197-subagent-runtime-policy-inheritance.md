---
title: "PRD-197: Inherit the effective runtime policy in subagent workers"
status: Implemented
version: 1.1.0
date: 2026-09-26
scope: "subagent mode, capability, approval, prompt-policy, and execution-context inheritance"
related_prds:
  - PRD-76   # runtime tool capability gates
  - PRD-78   # tool approval
  - PRD-124  # concurrent subagent pools
  - PRD-126  # subagent retries and transaction safety
  - PRD-155  # Safe, Plan, and Yolo runtime modes
  - PRD-163  # cache-stable workflow prompts
tags:
  - subagents
  - runtime-modes
  - capability-policy
  - approvals
  - security
  - workflows
---

# PRD-197 — Inherit the effective runtime policy in subagent workers

## 1. Executive summary

Subagents do not currently inherit the runtime mode of the agent that spawned
them. `SubagentWorker` deliberately creates a fresh `AppState`, sets its mode
to `Yolo`, and binds the child workspace policy to that state. A child launched
from a foreground `Safe` or `Plan` session can therefore execute a mutating
tool without the approval or hard-block policy that applies to the parent.

This behaviour is confirmed by both the implementation and its tests. The
source comments describe every subagent as autonomous/Yolo, and the integration
test `test_subagent_always_uses_isolated_yolo_policy` explicitly verifies that
a child write succeeds while the parent approval service denies it. The pool
unit test also expects a child workspace request to return `yolo_bypass` when
the parent is `Safe`.

The child’s workspace *scope*, parent model, selected provider options, retry
settings, visible-tool ceiling, conversation correlation IDs, and durable
journal are partially carried across. The child’s effective mode, blocked
capabilities, approval requirements, mode prompt suffix, and several other
turn-level policies are not. The result is a mixed and difficult-to-reason
contract: a child cannot necessarily see tools excluded from the parent’s
visible-tool list, but any visible mutating tool is evaluated under a new Yolo
policy.

This PRD defines a safe inheritance contract. A subagent receives an immutable
snapshot of the parent turn’s *effective* execution policy and uses an isolated
local representation of that policy. It does not share mutable parent state or
the parent transcript. A child may be narrower than its parent because of its
role allow-list, but it must never be broader unless a trusted workflow phase
explicitly establishes a broader effective policy before spawning it.

## 2. Evidence-backed diagnosis

### 2.1 Current data path

The current path is:

```text
foreground TUI / workflow turn
        │
        ▼
AgentTurnContext.app_state.active_mode()
        │
        ├── visible_tools is filtered for the parent turn
        └── make_spawn_subagents_tool(..., app_state=parent_state,
                                      approval_svc=parent_approval,
                                      workspace_access=parent_policy)
                │
                ▼
        SubagentWorker.__init__
                │
                ├── _make_yolo_app_state(parent_state)
                │       └── new AppState with RuntimeMode("Yolo")
                ├── _make_yolo_workspace_access(parent_policy, child_state)
                │       └── same scope, Yolo mode provider, no approval service
                └── retain parent approval service reference for hook compatibility
                        │
                        ▼
                child ToolCapabilityGate + ApprovalGate
                        │
                        ▼
                child AgentRunnerBase executes role-filtered tools
```

The child has a fresh `ShortTermMemory(max_tokens=8_000)` and receives its
task description and optional task context, not the parent’s messages. That
history isolation is intentional and is not itself the defect addressed by
this PRD.

### 2.2 Confirmed source facts

The investigation found the following current contracts:

| Context or policy | Current behaviour | Result |
|---|---|---|
| `RuntimeMode` | Replaced with a fresh `Yolo` mode | Safe approvals and Plan hard blocks are bypassed |
| `blocked_capabilities` | Not copied | Child capability gate is broader than parent |
| `approval_required` | Not copied; child workspace policy drops approval service | Mutations do not ask the foreground user |
| Mode prompt suffix | Not forwarded to the child system prompt | Child is not told the parent’s operational restrictions |
| Parent `AppState` signal | Not shared or mutated | Good isolation, but no policy inheritance |
| Workspace root/scope | Copied into a new policy | Child remains scoped to the same workspace |
| Parent visible tools | Passed as the child’s input ceiling | Child cannot recover tools omitted before spawn |
| Role `allowed_tools` | Intersected with the supplied tool list | Role-specific narrowing works |
| Model | Parent model ID is reused | Provider/model identity is consistent |
| Provider options | Temperature, top-p, completion limit, and request options are copied | Only a subset of execution configuration is inherited |
| Retry and usage correlation | Retry settings, conversation ID, run ID, ledger, and journal are passed | Observability and retry integration are partially inherited |
| Conversation history | Not copied; worker memory is fresh | Concurrent workers remain isolated |
| Skills, MCP, browser, prompt contract, and other turn fields | Not directly forwarded; only tools already visible can be used | Behaviour depends on indirect tool exposure |

The current tests are not evidence that inheritance works; they encode the
opposite contract. They must be changed as part of implementation.

### 2.3 Why this is a product and security defect

Runtime modes are an execution policy, not only a foreground UI decoration:

- `Safe` requires approval for side-effecting or undeclared capabilities.
- `Plan` hard-blocks write, execute, network, git-write, and undeclared tools.
- `Yolo` allows the capabilities exposed to the session without a prompt.

The parent user selected or was assigned one of those policies, yet the child
currently receives another one. This violates least privilege, makes a Plan
turn capable of changing the workspace through delegation, and makes the
visible mode badge misleading. It also means workflow authors cannot reason
about whether a subagent is read-only from the parent turn alone.

The defect is not solved by sharing the parent `AppState`: that would create a
mutable signal race, allow a child or mode change to affect the foreground
session, and make concurrent workers observe policy changes at unpredictable
times. The fix needs a policy snapshot, not shared mutable state.

## 3. Problem statement

The subagent boundary currently inherits selected services and data but not the
effective runtime policy that governs tool execution. The system needs one
explicit, auditable delegation contract that answers:

1. Which mode and capability restrictions apply to a child?
2. How are Safe approvals routed without sharing mutable UI state?
3. How are Plan hard blocks enforced even when the child has a role that can
   normally write files?
4. Which prompt and provider settings are inherited, and which remain isolated?
5. How can a workflow deliberately grant a child a broader policy without
   turning an arbitrary tool parameter into a permission escalation?

## 4. Goals

The implementation MUST:

1. Make a spawned worker inherit the parent turn’s effective runtime policy by
   default: mode identity, blocked capabilities, approval requirements, and
   policy prompt instructions.
2. Ensure a child’s effective permissions are the intersection of:
   - the parent turn’s effective policy;
   - the parent’s already-filtered visible-tool set; and
   - the subagent role’s `allowed_tools` set.
3. Preserve an isolated child state object. No child may mutate the parent
   `AppState`, mode signal, approval state, workspace object, or transcript.
4. Enforce `Plan` hard blocks in the child even for roles whose normal
   allow-list contains `write_file`, `run_command`, browser, or network tools.
5. Route `Safe` approval requests through the owning foreground approval
   service with a stable child/pool/task identity, so the user can distinguish
   the request from a foreground tool call.
6. Keep the existing workspace scope and path authorization guarantees. A
   policy snapshot must never broaden the allowed root or outside-workspace
   grants.
7. Inherit the effective mode’s system-prompt suffix and bounded policy
   instructions in a cache-stable position in the child prompt. Do not copy the
   complete parent system prompt, transcript, hidden reasoning, secrets, or
   tool results.
8. Continue to inherit the effective model and provider request settings that
   are safe and relevant to a worker, while making the copied field set
   explicit and typed.
9. Keep worker memory and conversation history isolated by default. A worker
   receives its task and explicit task context, not all parent messages.
10. Preserve existing retry, usage-ledger, durable-journal, cancellation, and
    complete-pool resume behaviour.
11. Allow trusted workflow execution to establish an explicit effective phase
    policy. A workflow phase that intentionally runs in Yolo remains able to
    spawn Yolo children, but that decision must come from the phase/runtime
    policy rather than an arbitrary model-supplied `spawn_subagents` argument.
12. Make the effective child policy observable without logging credentials,
    prompt contents, or sensitive tool arguments.
13. Provide a typed, bounded communication channel between the main agent and
    each child, including child clarification requests and parent replies.
14. Provide same-pool peer-to-peer communication without sharing private
    worker memory or allowing cross-session routing.
15. Persist, resume, cancel, expire, and idempotently recover communication
    state alongside the existing pool/journal lifecycle.

## 5. Non-goals

This PRD does not:

- merge the child’s private transcript into the parent conversation. Structured
  messages may cross the boundary through the protocol defined in Section 6.6;
- give children the full parent transcript or hidden reasoning;
- make all subagents globally Yolo for convenience;
- grant a child tools omitted from the parent’s visible-tool set;
- replace role-specific `allowed_tools` filtering;
- share a mutable `AppState` or `WorkspaceAccessPolicy` between tasks;
- copy API keys, authorization headers, cookies, plugin secrets, or MCP
  credentials into prompts or durable records;
- change the semantics of the foreground mode selector;
- remove explicit workflow-level permission decisions; or
- require every child to use the parent’s exact turn memory budget.

## 6. Proposed policy model

### 6.1 Immutable delegation snapshot

Introduce a typed, immutable boundary object, tentatively named
`SubagentExecutionPolicy` or `SubagentRuntimeContext`, created by the parent
turn at the point where `spawn_subagents` is registered. It MUST contain only
bounded, serializable or immutable policy data, for example:

```text
mode_name: str
blocked_capabilities: frozenset[ToolCapability]
approval_required: frozenset[ToolCapability]
system_prompt_suffix: str
visible_tool_names: frozenset[str]
workspace_scope_identity: bounded scope reference
effective_workflow/phase identity: bounded optional strings
policy_revision: stable diagnostic value
```

The object MUST represent the policy effective for the current parent turn,
not read a mutable foreground mode after the child has started. The snapshot
should be constructed from the canonical `RuntimeMode` and existing
capability/workspace policy owners rather than duplicating mode definitions.

The snapshot may contain references to services that are intentionally owned by
the parent, such as the approval coordinator, but those references are not
serialized and must not appear in prompts or checkpoints.

### 6.2 Child policy derivation

For each child:

```text
parent effective policy
        ∩ parent visible tools
        ∩ role allowed_tools
        ∩ child task/tool registry
        = child effective tool surface
```

The child-local `AppState` or equivalent policy adapter is initialized from
the snapshot’s mode, not from a hard-coded Yolo lookup. The child hooks then
use that local state. A child role can narrow access, but it cannot turn
blocked capabilities back on.

The workspace policy is rebuilt with the same scope and the child-local mode
provider. In `Safe`, it retains an approval adapter that annotates requests
with the pool ID, task ID, role, and tool-use ID. In `Plan`, workspace
authorization must fail before an outside-workspace approval path can grant
access. In `Yolo`, it behaves as the current no-prompt policy, subject to the
same scope and visible-tool ceiling.

### 6.3 Effective mode versus raw foreground mode

The source of inheritance is the parent turn’s effective policy, not blindly
the last mode displayed by the TUI. This matters for workflow phases that have
an explicit, trusted mode/policy override. For example, an implementation
phase may intentionally run under Yolo while the surrounding conversation is
in Plan mode. The phase’s effective policy must be recorded and passed to the
child, and the decision must be visible in diagnostics.

No model-supplied task field may contain `mode="Yolo"`, `approval=false`, or an
equivalent permission escalation. If a future API supports a child-policy
override, it MUST be validated by the same trusted workflow/mode boundary and
MUST be an explicit opt-in with a narrowing-by-default rule.

### 6.4 Bidirectional agent communication

Policy inheritance and agent communication are separate concerns. A child may
need information that was not present in its task, the parent may need to
correct or redirect a child, and two workers may need to coordinate. The
implementation MUST provide a session-scoped, per-pool message broker rather
than asking agents to share `ShortTermMemory`, append arbitrary text to one
another’s prompts, or discover each other through global state.

#### 6.4.1 Message envelope

Every message MUST use a typed envelope with fields equivalent to:

```text
message_id: stable unique ID
conversation_id: parent session identity
parent_run_id: parent turn/workflow identity
pool_id: subagent pool identity
sender: "main" | worker identity
recipient: "main" | worker identity | "pool"
kind: status | information | instruction | clarification_request |
      clarification_response | handoff | error | cancel
payload: bounded JSON-safe object or text
reply_to: optional message/question ID
correlation_id: optional request chain ID
sequence: monotonic per-sender sequence
created_at: wall-clock timestamp
expires_at: optional deadline
requires_response: boolean
policy_revision: bounded inherited-policy identity
```

The broker MUST validate that all identifiers belong to the same live parent
session and pool. Routing metadata is authoritative; message payloads are
untrusted agent content and MUST NOT grant tools, alter policy, or override a
system instruction.

#### 6.4.2 Main-agent and subagent APIs

The implementation MUST expose session-bound, role-filtered communication
operations. Names are provisional, but the semantics are required:

| Operation | Sender → recipient | Semantics |
|---|---|---|
| `send_parent_message` | child → main | Fire-and-forget bounded information, status, handoff, or error message |
| `ask_parent` | child → main | Request a clarification and await a correlated answer or cancellation |
| `answer_subagent` | main → child | Resolve a pending child question using its question ID and pool identity |
| `send_subagent_message` | main → child | Deliver a bounded instruction or clarification to one child |
| `poll_subagent_messages` | child → broker | Read ordered parent/peer messages not yet consumed by the child |
| `send_peer_message` | child → child | Send bounded information or instruction to another worker in the same pool |
| `ask_peer` | child → child | Request a correlated answer from an active peer |
| `answer_peer` | child → child | Resolve a peer question after validating same-pool membership |
| `collect_subagent_results` | main → pool | Resume/collect a pool that returned early because clarification was pending |

The parent-facing operations MUST be available to the main agent only while
the corresponding pool is owned by that session. Child-facing operations MUST
be added to a worker’s tool surface only after normal parent-visible-tool,
role-allow-list, and capability-policy filtering. Communication tools are
control-plane tools; they do not grant file, command, browser, network, or
workflow capabilities.

#### 6.4.3 Clarification rendezvous

`ask_parent` MUST be a real rendezvous, not a fabricated answer:

1. The child creates a durable `clarification_request` with a question ID,
   bounded question/context, expected answer shape, and deadline.
2. The broker persists a bounded `question_wait_started` lifecycle record and
   exposes the pending question in the structured result returned by the pool
   tool.
3. The pool returns a resumable `awaiting_clarification` result to the main
   agent when it cannot complete without an answer. The result contains the
   pool ID, worker ID, question ID, question text, and safe answer guidance;
   it does not contain the child’s private transcript.
4. The main agent reasons about the question and calls `answer_subagent`, or
   asks the human through the existing question/approval surface and then
   calls `answer_subagent` with the user-approved answer.
5. The broker validates the correlation, records the response, wakes only the
   requesting child, and the main agent calls `collect_subagent_results` when
   it is ready to receive the remaining aggregate.
6. A timeout, cancellation, denied request, pool shutdown, or lost owner
   resolves the question with an explicit terminal outcome. A child MUST NOT
   wait forever on an unanswered question.

The parent turn MUST NOT be re-entered recursively while a tool call is still
on the stack. Returning a structured pending result and resuming through a
later session-bound tool call avoids nested provider calls, preserves the
existing transaction boundary, and works with providers that do not support
parallel conversational branches.

#### 6.4.4 Main-to-child delivery

`send_subagent_message` writes to the target worker’s mailbox. The worker
runtime drains that mailbox between provider turns and presents messages as a
clearly delimited, untrusted `AGENT MESSAGE` input block. If the worker is in a
provider request, the message remains queued; the default implementation MUST
NOT cancel an in-flight request merely to deliver an instruction. A worker may
also call `poll_subagent_messages` or `wait_for_parent_message` at a safe turn
boundary.

Parent instructions MUST carry a sender identity and sequence number. They
are context, not a replacement for the worker’s system prompt or capability
policy. A child cannot use a message to enable a blocked tool, change its
recipient, or impersonate the main agent.

#### 6.4.5 Peer-to-peer delivery

Peer communication MUST be limited to workers in the same active pool. The
broker validates recipient membership, preserves per-sender ordering, and
records `reply_to`/`correlation_id` for request-response exchanges. A peer
cannot address an arbitrary session, parent run, or worker in another pool.
The main agent remains able to observe bounded message summaries and can
cancel a peer exchange, but peer messages do not automatically become part of
the parent transcript.

#### 6.4.6 Result and artifact handoff

Messages are for coordination, clarification, and bounded findings. Large
research notes, source files, screenshots, and other artifacts MUST be written
through the existing authorized artifact tools and referenced by a validated
workspace-relative artifact ID/path. The broker MUST reject oversized payloads
and MUST not turn a large message into an unbounded prompt append. The final
worker result and durable journal remain the authoritative work-product
handoff.

#### 6.4.7 Persistence and restart

The broker MUST append lifecycle records to the existing session journal:

```text
agent_message_sent
agent_message_delivered
question_wait_started
question_answered
question_timed_out
question_cancelled
agent_message_rejected
```

Records contain bounded envelopes, pool/worker identity, sequence, outcome,
and policy revision. They MUST NOT contain secrets, full prompts, hidden
reasoning, or arbitrary tool arguments. On process restart, the parent run
rehydrates unconsumed messages and pending questions. A question whose worker
or pool no longer exists is resolved as `orphaned`, not silently answered or
discarded. Resume MUST never restore a worker under a different policy without
the normal effective-phase policy validation.

#### 6.4.8 Concrete implementation components and data flow

The implementation SHOULD be divided into the following ownership boundaries:

1. `AgentMessageEnvelope` — a frozen, typed value object that validates IDs,
   kind, payload size, recipient, sequence, deadline, and correlation fields.
2. `SubagentMessageBroker` — owned by one `SubagentPool`; maintains worker
   membership, per-recipient mailboxes, pending-question futures, sequence
   counters, quota accounting, and idempotency keys. It is the only component
   allowed to route messages.
3. `ParentCommunicationTools` — session-bound tools registered with the main
   agent for `answer_subagent`, `send_subagent_message`, and
   `collect_subagent_results`.
4. `ChildCommunicationTools` — worker-bound tools registered only for the
   worker’s own identity for `send_parent_message`, `ask_parent`, mailbox
   polling, and same-pool peer operations.
5. `CommunicationJournalAdapter` — translates broker state transitions into
   the durable journal and rehydrates them without replaying terminal events.
6. `PoolContinuationRegistry` — retains an active or resumable pool handle
   after `spawn_subagents` returns `awaiting_clarification`, validates its
   parent owner, and exposes it to the continuation tools. It must use the
   existing session ownership/lease boundary rather than introduce a second
   owner model.

The principal data flows are:

```text
child ask_parent
    → ChildCommunicationTools validates own identity and policy
    → SubagentMessageBroker creates question + durable journal record
    → SubagentPool returns awaiting_clarification to main agent
    → main agent calls answer_subagent(question_id, answer)
    → broker validates owner/correlation + records answer
    → child future resumes with structured answer
    → main agent calls collect_subagent_results(pool_id)

main send_subagent_message
    → ParentCommunicationTools validates pool ownership + target membership
    → target mailbox + durable sent record
    → child turn boundary drains ordered AGENT MESSAGE block
    → child acknowledges delivery; parent can observe bounded summary

child ask_peer
    → broker validates both workers belong to this pool
    → peer mailbox receives request + correlation ID
    → peer calls answer_peer(reply_to, answer)
    → requesting child resumes; no parent transcript merge occurs
```

The broker state machine MUST distinguish `created`, `queued`, `delivered`,
`acknowledged`, `answered`, `expired`, `cancelled`, `denied`, `orphaned`, and
`rejected`. A terminal state is immutable. Every transition is keyed by the
envelope/question ID and is safe to replay. `spawn_subagents` MUST continue to
return its current aggregate shape when no message is pending; the
communication extension is activated only when a worker sends or receives a
message.

### 6.5 Prompt and cache contract

The child’s role system prompt remains its primary instruction. The inherited
mode suffix and bounded policy instructions are appended in a deterministic,
stable section, before task-specific context. The implementation MUST:

- preserve the existing role prompt and final-response contract;
- avoid injecting the entire parent prompt or transcript;
- keep the policy section stable for all children spawned under the same
  effective mode;
- avoid embedding volatile pool IDs, timestamps, or tool results in the
  cacheable policy prefix; and
- place task-specific context after the stable policy section.

This keeps runtime safety instructions visible to the child without creating a
new cache-invalidation source for every worker.

### 6.6 Context inheritance matrix

The implementation and documentation MUST publish and test this matrix:

| Context | Child default |
|---|---|
| Effective mode and capability blocks | Inherit immutable snapshot |
| Approval requirements | Inherit; route Safe requests to parent service with child identity |
| Mode prompt suffix | Inherit in stable policy section |
| Parent visible tools | Inherit as hard ceiling |
| Role allow-list | Apply as an additional narrowing filter |
| Workspace scope | Inherit exact scope, never broaden |
| Provider/model and safe request options | Inherit the resolved effective values |
| Retry policy | Inherit current retry contract |
| Usage/journal/run correlation | Inherit existing correlation contract |
| Skills/MCP/browser integrations | Use only integrations represented by the inherited visible tool surface; do not auto-discover broader tools |
| Parent transcript and hidden reasoning | Do not inherit |
| Parent mutable reactive state | Do not share |
| Worker memory | Fresh and isolated |
| Agent communication | Typed, bounded, correlated messages through the per-pool broker; never implicit transcript sharing |
| Secrets | Never copy to prompt, task context, or diagnostics |

## 7. Functional requirements

### FR-1 — Capture the effective policy at spawn time

`AgentTurnRunner`/`AgentTurnContext` MUST pass a typed effective-policy
snapshot to `make_spawn_subagents_tool`. The implementation MUST not make the
pool infer policy by looking up `Yolo` or by consulting mutable parent state
later.

### FR-2 — Remove unconditional Yolo derivation

`_make_yolo_app_state` and `_make_yolo_workspace_access` MUST be replaced by
policy-aware constructors or retained only as explicitly named compatibility
helpers that are no longer used by the default delegation path. A child
started from Safe or Plan MUST not silently become Yolo.

### FR-3 — Reapply capability and approval gates

Every child tool call MUST pass through a capability gate configured with the
child-local snapshot. Safe approval and Plan hard-block behavior MUST be
preserved even when the role’s normal tool list contains a mutating tool.

### FR-4 — Preserve role and parent filtering

The tool list sent to a child MUST remain the intersection of the parent
visible tools and the role allow-list. The policy gate is an additional
runtime defense; filtering alone is not sufficient because a tool may be
invoked through a dynamically populated registry.

### FR-5 — Route approvals safely

Safe-mode child approval requests MUST:

- use the existing session approval service or a documented child-aware adapter;
- include a bounded worker identity (`pool_id`, `task_id`, role, and tool name);
- render as a child request in the foreground approval UI;
- resume only the requesting child when approved or denied; and
- remain cancel-safe if the parent turn or pool is interrupted.

Concurrent child approvals MUST not overwrite one another’s pending request.
If the current single-slot approval service cannot represent this safely, the
implementation MUST extend its durable request identity rather than bypassing
approval.

### FR-6 — Preserve workspace authorization

The child policy MUST retain the parent scope and all path canonicalization and
outside-workspace rules. Safe approvals may grant only the existing supported
scope and operation, and Plan cannot grant a side effect merely because the
child is a subagent.

### FR-7 — Inherit policy instructions

The worker system prompt MUST tell the child which effective mode it is in and
what that means for tools. The prompt MUST explicitly state that tool calls
blocked by the policy must not be attempted and that Safe requests require
user approval. The instructions are supplementary; hooks remain authoritative.

### FR-8 — Preserve execution configuration

The factory MUST use one typed representation for effective model/provider
options, retry settings, request options, timeout, and relevant session
identity. It MUST not accidentally omit a setting merely because the current
implementation copied only four `AgentConfig` fields. Secret-bearing values
remain process-local and are never placed in prompts or journal text.

### FR-9 — Preserve intentional isolation

The worker MUST continue to use fresh private memory and task context. No
implementation may solve policy inheritance by sharing the parent memory or
replaying the parent transcript into every child. The parent receives only the
existing bounded/complete aggregate contract from the pool.

### FR-10 — Preserve workflows and explicit phase policy

Built-in and generated workflows, including `create_workflow`, MUST inherit
the same runtime contract without generated code having to implement custom
subagent security hooks. A trusted workflow phase may deliberately select an
effective policy, but the selection must be checkpointed/observable and passed
to all children spawned in that phase.

### FR-11 — Explain denied child actions

When a child action is blocked, the tool result MUST identify the blocked
capability and effective mode in bounded, model-readable language. It MUST NOT
suggest switching to Yolo as an automatic remedy when the parent policy is
Safe or Plan; it should instruct the parent/ user to change the trusted mode or
approve the action according to the normal UI.

### FR-12 — Migration compatibility

Existing callers that construct `make_spawn_subagents_tool` without runtime
policy context (headless tests, lightweight integrations) MUST retain their
current no-hook compatibility behaviour or receive an explicit safe default.
Production interactive sessions MUST always supply the policy snapshot.

### FR-13 — Create a per-pool communication broker

Every production subagent pool MUST own a typed message broker bound to the
parent conversation, parent run, pool ID, and effective-policy identity. The
broker MUST provide authenticated mailboxes for the main agent, each child,
and the pool broadcast channel. It MUST not be process-global and MUST reject
messages from a different session, run, pool, or worker identity.

### FR-14 — Provide structured communication tools

The implementation MUST provide the operations in Section 6.4.2, or
equivalent names with the same semantics. The public `spawn_subagents` task
schema MUST remain compatible. Communication tools MUST be session-bound,
role-filtered, capability-neutral control-plane tools and MUST not be usable to
grant a child additional file, execution, network, browser, or workflow access.

### FR-15 — Support child questions to the main agent

`ask_parent` MUST suspend only the requesting child, persist a bounded pending
question, and return a structured pending result to the main agent when the
pool cannot complete. The main agent MUST be able to answer through a
correlated `answer_subagent` call and later collect the resumed pool. The
implementation MUST not recursively invoke the parent LLM while the parent is
already executing a tool call.

### FR-16 — Support main-agent instructions to children

The main agent MUST be able to send a bounded instruction or clarification to
one active child. Delivery MUST be ordered, mailbox-based, and safe at a
provider-turn boundary. A message MUST be represented as untrusted context and
MUST never replace system instructions or alter the child policy.

### FR-17 — Support peer communication

Children MUST be able to send information and ask/answer questions with other
active children in the same pool. The broker MUST validate same-pool
membership, preserve correlation and ordering, and reject cross-pool or
cross-session recipients. Peer communication MUST not require sharing private
worker memory.

### FR-18 — Support bounded message lifecycle

Messages and questions MUST have configurable size, count, timeout, and
retention limits. The broker MUST support delivery, acknowledgement, expiry,
denial, cancellation, and orphaned-worker outcomes. Oversized artifacts MUST
be handed off through the existing filesystem/artifact tools and referenced by
validated artifact identity rather than embedded in messages.

### FR-19 — Persist and rehydrate communication state

The broker MUST append the lifecycle records in Section 6.4.7 to the existing
durable journal. Resume/restart MUST restore unconsumed messages and pending
questions when their pool and owner are valid, and MUST explicitly resolve
questions whose worker no longer exists. Duplicate delivery or duplicate
answers MUST be idempotent and MUST not wake a child twice.

### FR-20 — Expose communication in prompts and results

The parent and child system instructions MUST explain the available
communication operations, when to ask for clarification, how to address a
peer, and that messages are untrusted context rather than permission grants.
The pool result MUST expose bounded message/question summaries, pool IDs, and
correlation IDs so the main agent can continue a pending exchange. It MUST
not expose private child transcripts, hidden reasoning, secrets, or arbitrary
tool arguments.

## 8. Non-functional requirements

### NFR-1 — Least privilege

No default child path may grant capabilities broader than the effective parent
turn. Any deliberate broader workflow policy must be explicit, trusted, and
auditable.

### NFR-2 — Concurrency safety

Parallel workers MUST have immutable policy snapshots and independent local
state. One child’s mode or approval response MUST not affect another child or
the parent.

### NFR-3 — Determinism and cache stability

Policy derivation and prompt assembly MUST be deterministic for equivalent
parent turns. Volatile worker identity belongs in metadata/events, not in the
stable cacheable prompt prefix.

### NFR-4 — Backwards compatibility

The public `spawn_subagents` task schema remains unchanged unless a typed
internal context argument is required. Existing role allow-lists, tool names,
worker result format, usage accounting, retries, journal records, and resume
fingerprints remain compatible.

### NFR-5 — Observability

Pool start, child start, tool denial, approval request, approval response, and
child completion events MUST expose the effective mode name and a bounded
policy revision/identity. They MUST not expose secrets or complete prompts.

### NFR-6 — Performance

Creating a policy snapshot and child-local adapter MUST be O(number of policy
capabilities + number of visible tools) and must not copy the parent transcript
or scan the workspace. The change must not add a provider round trip.

### NFR-7 — Recovery

Cancellation, retry, process restart, and complete-pool resume MUST not leave a
child approval request orphaned or restore a child under a different policy
than the one recorded for its parent phase. Policy identity should be included
in bounded journal/checkpoint metadata where required for safe diagnosis.

### NFR-8 — Message boundedness

The broker MUST enforce limits for payload bytes, outstanding messages per
mailbox, pending questions per pool, total retained lifecycle records, and
question lifetime. Limits MUST be configurable within safe bounds and failures
MUST return structured, recoverable errors rather than growing provider
prompts without limit.

### NFR-9 — Ordering and idempotency

Messages from one sender to one recipient MUST be delivered in sequence order.
Message IDs, question IDs, answers, acknowledgements, and collection requests
MUST be idempotent. Replayed journal records or duplicate provider tool calls
MUST not duplicate a message, answer a question twice, or wake a cancelled
worker.

### NFR-10 — No re-entrant provider execution

Communication MUST not invoke the parent or a peer provider recursively from
inside an active provider/tool call. Clarification uses a durable rendezvous
and a subsequent session-bound tool call, preserving the existing conversation
transaction and retry boundaries.

### NFR-11 — Message confidentiality

Message payloads, journal records, diagnostics, and TUI summaries MUST be
redacted and bounded. The protocol MUST reject credentials, authorization
headers, cookies, hidden reasoning, and raw provider request bodies when they
are identifiable, and MUST never use message content as a permission source.

### NFR-12 — Liveness and cancellation

Every pending question and peer request MUST have a deadline or cancellation
path. Pool cancellation, parent interruption, worker failure, owner loss, and
process restart MUST resolve pending futures and release mailbox resources.

## 9. Acceptance criteria

### AC-1 — Safe parent

Given a Safe parent with an approval service that denies writes, an
implementer child attempting `write_file` MUST produce an approval request and
the file MUST NOT be changed when the user denies it. The parent remains Safe.

### AC-2 — Plan parent

Given a Plan parent, a child with write, execute, network, or undeclared tools
MUST receive a blocked result and MUST NOT invoke the underlying callable. No
approval overlay may turn a Plan hard block into execution.

### AC-3 — Yolo parent

Given a Yolo parent and a role/tool combination already allowed by the parent
visible-tool ceiling, the child MAY execute without an approval prompt. Its
workspace scope remains unchanged.

### AC-4 — Role narrowing

Given a Yolo parent and a role that does not allow `write_file`, the child MUST
not see or invoke `write_file`. Inherited Yolo policy must not defeat the role
allow-list.

### AC-5 — Parent isolation

Changing the child-local mode signal, resolving a child approval, or running
several children concurrently MUST not change the parent mode, pending
approval, workspace policy, visible tools, or transcript.

### AC-6 — Effective workflow phase policy

When a trusted workflow phase explicitly establishes Yolo as its effective
policy, a child spawned during that phase inherits Yolo. When the same workflow
phase runs under Plan, the child inherits Plan. A model-supplied task field
cannot change either result.

### AC-7 — Prompt contract

The child prompt contains the role instructions, the effective mode policy
section, the final-response contract, and task-specific context in a
deterministic order. The parent transcript and hidden reasoning are absent.

### AC-8 — Workspace boundary

A child cannot access a path outside the parent scope unless the existing
workspace authorization contract grants it. Safe approval and Yolo execution
must not widen the canonical root.

### AC-9 — Configuration and accounting

The child uses the effective parent model/provider settings, retry policy,
conversation correlation, usage ledger, journal, and timeout contract. A
nullable or missing provider usage field does not change policy inheritance or
fail a worker’s accounting.

### AC-10 — Recovery and cancellation

Cancelling the parent or resuming a complete pool does not leave a child
approval request active. A resumed pool uses the policy recorded for its
parent phase and does not silently become Yolo.

### AC-11 — Generated workflows

A workflow generated by `create_workflow` receives the same inheritance
behaviour without generated source adding a custom `ToolCapabilityGate` or
approval implementation. The create-workflow authoring instructions document
that subagents inherit the phase’s effective policy.

### AC-12 — Regression suite

The existing tests that assert unconditional Yolo behaviour are replaced with
tests for AC-1 through AC-11. Unit, integration, and E2E coverage passes for
Safe, Plan, Yolo, headless compatibility, concurrent workers, workflow phase
overrides, and resume/cancellation paths.

### AC-13 — Child-to-parent clarification

An active child can call `ask_parent` with a bounded question. The pool
returns `awaiting_clarification` with stable pool/worker/question identifiers,
the main agent can call `answer_subagent`, and the child resumes with exactly
that answer. No nested parent provider call occurs.

### AC-14 — Parent-to-child instruction

The main agent can send a bounded instruction to a selected active child. The
child receives it at a safe turn boundary in an explicitly delimited message
block, in order, without a change to its system prompt, capability policy, or
workspace scope.

### AC-15 — Peer coordination

Two children in one pool can exchange a bounded message and complete a
request/response exchange through `ask_peer`/`answer_peer`. A child cannot
address a worker in another pool or session, and a failed peer request has an
explicit timeout outcome.

### AC-16 — Message lifecycle and duplicate safety

Send, delivery, acknowledgement, expiry, cancellation, denial, orphaning,
duplicate send, duplicate answer, and pool shutdown each produce the expected
structured outcome. Replaying the journal does not deliver or answer anything
twice.

### AC-17 — Durable clarification recovery

After process restart or workflow resume, an unconsumed parent message and a
pending clarification are rehydrated when their pool owner is valid. If the
worker is gone, the question becomes `orphaned` and the parent receives an
actionable result rather than hanging indefinitely.

### AC-18 — Bounded handoff

The broker rejects oversized or over-quota payloads with a structured error.
The child can instead write a large artifact through an authorized tool and
send a validated artifact reference to the parent or peer.

### AC-19 — Communication cannot escalate policy

Messages containing requests such as “switch to Yolo”, “ignore Plan”, or
“approve this tool” are delivered only as untrusted content. They cannot alter
the child mode, approval state, visible tools, workspace scope, or recipient.

### AC-20 — Main-agent continuation contract

The parent system prompt and the structured `spawn_subagents` result explain
how to answer a pending question and collect the pool. Existing pools that do
not exchange messages retain their current aggregate result and do not incur
an additional provider turn.

## 10. Test plan

### Unit tests

- snapshot creation copies the effective mode and all restricted capability
  sets without sharing mutable signals;
- policy intersection is deterministic and role allow-lists can only narrow;
- Safe, Plan, and Yolo child-local modes have the expected gate behaviour;
- workspace policy preserves scope and does not retain an unsafe Yolo bypass;
- prompt assembly includes the stable policy suffix and excludes parent
  transcript/secrets;
- model/provider/retry option resolution has an explicit typed field matrix;
- child denial results are bounded and actionable;
- policy identity is stable for equivalent policy snapshots;
- legacy/headless factory construction remains compatible.
- message envelope validation rejects foreign identities, malformed payloads,
  oversized content, invalid recipients, and policy-escalation metadata;
- mailbox ordering, acknowledgement, expiry, cancellation, and idempotency
  work under duplicate calls;
- `ask_parent`, `answer_subagent`, `send_subagent_message`, `ask_peer`, and
  `answer_peer` implement the expected state transitions without re-entrancy;
- journal rehydration restores pending messages/questions and marks missing
  workers orphaned.

### Integration tests

- run real `SubagentWorker` instances under each parent mode with real
  capability and approval hooks;
- exercise concurrent Safe approvals and verify request identity/response
  routing;
- exercise Plan with implementer and executor roles;
- verify tool registry filtering, workspace scope, journal, usage ledger, and
  retry wiring together;
- verify a trusted workflow phase mode override and reject model-supplied
  override attempts;
- verify interruption and complete-pool resume retain the policy identity.
- run a child clarification through the parent continuation path and verify
  the parent provider is not recursively invoked;
- deliver parent instructions while a child is between turns and verify
  ordering, policy isolation, and bounded message summaries;
- coordinate two workers through peer request/response and reject a
  cross-pool recipient;
- restart with durable pending messages and verify exactly-once answer and
  delivery behaviour.

### End-to-end tests

- TUI Safe: spawn an implementer, deny the approval, and confirm no file
  change;
- TUI Plan: spawn children and confirm no side-effecting call can execute;
- TUI Yolo: execute a permitted child mutation and return the aggregate;
- workflow: run a generated workflow phase with subagents and resume it after
  interruption without a policy downgrade or escalation;
- workflow: have a generated workflow subagent ask the main agent for a
  missing requirement, answer it through the structured continuation tools,
  and complete the phase;
- headless: verify the documented no-policy compatibility path and explicit
  configuration behaviour.

## 11. Rollout and migration

1. Add the typed policy snapshot and unit tests behind the existing subagent
   factory boundary.
2. Replace the current unconditional-Yolo production path and update the
   tests whose names/documentation encode that behaviour.
3. Audit built-in workflows for phases that intentionally depend on Yolo
   subagents. Mark those phases with the existing trusted effective-policy
   mechanism; do not add a user/model-controlled `mode` task field.
4. Update `create_workflow` instructions and workflow documentation so new
   workflows rely on the runtime policy rather than embedding permission hooks.
5. Implement the per-pool broker and communication tools behind the existing
   `spawn_subagents` session boundary. Keep the no-message path behaviourally
   unchanged.
6. Add the clarification continuation path, parent/child mailboxes, peer
   routing, bounded lifecycle, journal records, and restart rehydration.
7. Add observability for policy inheritance and communication: monitor denied
   child calls, approval latency, pending-question age, orphaned requests,
   message rejection, and accidental compatibility-path usage.
8. Remove obsolete `_make_yolo_*` production helpers and any ad-hoc message
   bypass after all callers migrate; no legacy policy or communication path
   should remain in the final implementation.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Existing workflows rely on child writes while the foreground is Plan | Use explicit trusted phase policy and audit workflows before cutover |
| Concurrent approval requests collide in the current service | Add stable child request identity and serialize/render requests safely |
| A child receives too little context to complete work | Continue passing explicit task context and visible tools; do not copy the transcript |
| Prompt changes reduce provider cache hits | Keep the mode policy section deterministic and separate volatile diagnostics |
| Headless callers lack a mode | Preserve their documented no-hook compatibility path or use an explicit Safe default |
| Resume restores a stale policy | Persist a bounded policy identity and revalidate it against the resumed parent phase |
| Tool filtering and runtime gates drift | Keep filtering as optimization and hooks as the authoritative enforcement layer |
| A child waits forever for a parent or peer | Enforce deadlines, cancellation, owner-loss handling, and durable terminal outcomes |
| Clarification re-enters the parent provider recursively | Return a structured pending result and require a later answer/collect tool call |
| A malicious message attempts prompt or policy injection | Delimit messages as untrusted context; authenticate routing and keep hooks authoritative |
| Message traffic overwhelms the parent context | Enforce payload/quota limits and use artifact references for large outputs |
| Restart duplicates delivery or answers | Persist message IDs and state transitions with idempotent rehydration |

## 13. Documentation requirements

The implementation MUST update:

- `docs/guides/workflows.md` with the subagent inheritance and effective-phase
  policy contract;
- the relevant subagent/tool reference with the context inheritance matrix;
- the relevant subagent/tool reference with the communication envelope,
  clarification rendezvous, mailbox, and peer-coordination contract;
- `docs/guides/architecture.md` or the security reference with the immutable
  policy snapshot, approval routing boundary, and per-pool broker;
- `create_workflow` authoring prompts/tools so generated workflows do not
  reimplement or bypass the runtime policy;
- `llms-full.txt` if public symbols are added or changed; and
- this PRD’s status and implementation evidence when the work is completed.

## 14. Open implementation decisions

The implementation plan MUST resolve these before coding, with tests:

1. Whether the policy snapshot is a new public type or an internal typed
   dataclass at the `subagents` boundary.
2. Whether Safe approval routing extends `ApprovalRequest` directly or uses a
   child-aware adapter while retaining the existing UI protocol.
3. Which resolved provider options are safe to inherit when a configuration
   object contains secret-bearing request headers.
4. How a durable resume validates a recorded policy identity against the
   current workflow phase without making a valid run permanently unrestorable
   after a harmless mode-label change.
5. Which existing workflow phase mechanism is the canonical trusted source for
   an intentional Yolo override.
6. Whether the broker should be a new in-process service owned by
   `SubagentPool` or an adapter over the existing session event processor and
   journal, while preserving one authoritative message state machine.
7. Whether a pending clarification should be rendered through the existing
   question overlay, a dedicated subagent-question overlay, or both. The
   answer path must still end at the correlated `answer_subagent` operation.
8. The exact bounded defaults for message bytes, mailbox depth, pending
   questions, peer-request lifetime, and journal retention.

The default decision for all unresolved cases MUST be least privilege,
immutable state, no transcript sharing, no re-entrant provider calls, and no
model-controlled escalation.

## 15. Implementation evidence

This PRD is implemented in the current source tree.

### Source components

- `src/agenthicc/subagents/policy.py` defines the frozen
  `SubagentExecutionPolicy`, isolated child mode/workspace adapters, stable
  policy revision, and inherited prompt section.
- `src/agenthicc/subagents/communication.py` defines the per-pool
  `AgentMessageBroker`, immutable envelopes, quotas, membership validation,
  parent/child/peer tools, clarification futures, continuation registry, and
  journal rehydration.
- `src/agenthicc/subagents/pool.py` applies the policy as a hard ceiling,
  preserves Safe approvals and Plan blocking, injects bounded communication
  tools, drains parent/peer messages only at provider-turn boundaries, and
  resumes a child with its private memory after an answer.
- `src/agenthicc/subagents/tool.py` includes the policy revision in complete
  pool fingerprints, returns a structured `awaiting_clarification` result, and
  exposes correlated answer/send/collect tools only through the session-bound
  spawn callable.
- `src/agenthicc/runners/agent_turn.py` captures the effective parent policy
  after phase/mode/tool filtering and registers the continuation tools in the
  parent turn.
- `src/agenthicc/memory/journal.py` persists bounded communication lifecycle
  records and exposes the fold used by broker rehydration.

### Verification

The implementation adds focused unit, integration, and end-to-end coverage for
policy snapshots, Safe/Plan behavior, bounded routing, ordering, duplicate
answers, peer membership, journal rehydration, and a real clarification
continuation. The focused subagent set passes (`77 passed`), the integration
suite passes (`243 passed`), and the E2E suite passes (`130 passed, 1
skipped`). The repository-wide unit suite passes (`3460 passed, 14 skipped`).
The type-audit ratchet, targeted mypy, full Ruff check, and strict MkDocs build
pass. Full-repository mypy and format-check commands retain unrelated baseline
findings in `process_lease.py`, `name_that_ui.py`, `session_service.py`, and
nine existing unformatted files; all changed subagent files pass their
corresponding gates.
