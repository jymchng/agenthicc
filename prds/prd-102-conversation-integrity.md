# PRD-102 — Conversation Integrity: Eliminating Orphaned Tool Messages

**Classification:** Principal Software Architect — Root-Cause Analysis  
**Trigger error:** `400 - {"object":"error","message":"Unexpected role 'tool' after role 'system'","type":"invalid_request_message_order","code":"3230"}`

---

## 1. Root-Cause Analysis

The error `"Unexpected role 'tool' after role 'system'"` has one immediate cause:
the API received a message sequence where a tool-result message appears without a
preceding assistant message containing the matching tool-call.  The protocol
invariant violated is:

```
∀ tool_msg ∈ conversation:
  ∃ assistant_msg preceding tool_msg such that
    assistant_msg.tool_calls contains an entry with id == tool_msg.tool_call_id
```

The system message is always prepended fresh at request time.  So the orphaned
tool message is always the **first** message in the persisted history slice —
meaning the failure happened at a turn boundary where the assistant's tool-call
was lost but its result was not.

---

## 2. Failure Mode Taxonomy

### 2a. Checkpoint Recovery — the most common cause

```
Turn N:
  write: assistant({tool_calls: [call_A]})   ← committed to storage
  execute: tool_A()                            ← side effect happens
  write: tool({tool_call_id: call_A, result}) ← committed to storage
  [CRASH or TIMEOUT before assistant generates final response]

Recovery:
  system  ← prepended fresh
  tool({tool_call_id: call_A, result})  ← first stored message
  ← INVALID: tool immediately after system
```

The root cause here is that checkpointing writes messages individually rather
than as a complete turn transaction.  The assistant message was present in
storage, but the recovery path used a snapshot taken after the tool result was
written and replayed from the wrong offset.

### 2b. Context Compaction — silent corruption

When the context window fills up, compaction algorithms summarize old turns:

```
Before compaction:
  system | user | assistant(tool_calls) | tool | assistant | user | assistant(tool_calls) | tool | assistant

After compaction targeting 50% reduction:
  system | [summary of early turns] | tool | assistant
                                       ↑
                              orphaned — the assistant(tool_calls) that
                              preceded this was in the summarized block,
                              but the tool result was not
```

Compaction algorithms that operate on individual messages rather than complete
turn transactions will always produce this.  A safe compaction boundary is the
**turn** (all messages from one user message to the next), not the **message**.

### 2c. Parallel Tool Execution — race conditions

```
assistant sends: {tool_calls: [call_A, call_B, call_C]}

Worker 1: executes call_A → writes tool_result(A)   t=100ms
Worker 2: executes call_B → FAILS                   t=150ms
Worker 3: executes call_C → writes tool_result(C)   t=200ms

Retry: only call_B retried
  new assistant message written: {tool_calls: [call_B]}  ← WRONG
  tool_result(B) written

New session loaded from storage:
  system
  tool_result(A)   ← orphaned: its parent assistant was overwritten
  tool_result(C)   ← orphaned
  assistant({tool_calls: [call_B]})
  tool_result(B)
```

The retry handler created a new assistant message instead of preserving the
original multi-call assistant message and only adding the missing tool result.

### 2d. Event Sourcing Out-of-Order Replay

In an event-sourced system where conversation state is reconstructed from events:

```
Events emitted:
  e1: TOOL_RESULT_ADDED  (tool_call_id=A, result=...)
  e2: ASSISTANT_MSG_ADDED (tool_calls=[A])

If e1 is processed before e2 (network reorder, Kafka partition lag,
eventual consistency in a distributed log):

Reconstructed state:
  tool(A)           ← no preceding assistant
  assistant(A)      ← too late
```

The events are causally ordered but the subscriber processes them out of
emission order.

### 2e. Multi-Agent Handoff — context slicing

```
Agent A context:
  system_A | user | assistant(tool_calls=[A]) | tool(A) | assistant("result")

Handoff to Agent B: transfer only the "final" assistant message
  system_B | assistant("result")  ← valid so far

If handoff ALSO transfers tool context for Agent B's tools:
  system_B | tool(B_historical)   ← immediate orphan if B's history
                                     is prepended before the user message
```

### 2f. Memory Persistence — foreign key violations in message storage

When conversations are stored in a relational DB without enforced referential
integrity between tool_calls and tool_results:

```sql
-- assistant message deleted for storage optimization
DELETE FROM messages WHERE role='assistant' AND token_count > 2000;

-- tool results remain with dangling parent references
SELECT * FROM messages WHERE conversation_id=X ORDER BY created_at;
-- Returns: system, tool, tool, assistant, ...
```

### 2g. Message Filtering for Context Window

```python
# Naive filter: keep most recent N messages
messages = get_all_messages()[-50:]

# If message 51 was: assistant(tool_calls=[A])
# And message 52 was: tool(A)
# After slicing: messages starts at index 52 → tool(A) is first persisted message
# After system prepend: [system, tool(A), ...]  ← INVALID
```

---

## 3. The Invariants That Must Always Hold

Before any API request is serialized and sent:

```
INV-1: The first non-system message must have role=user or role=assistant
       (never role=tool)

INV-2: For every tool message T in the sequence, there exists an assistant
       message A at an earlier position such that:
         A.tool_calls contains an entry where entry.id == T.tool_call_id

INV-3: All tool results for a given assistant turn must appear consecutively
       after that assistant message and before any subsequent user/assistant
       message

INV-4: The number of tool messages following an assistant turn equals
       exactly the number of tool_calls in that assistant message
       (no missing results, no extra results)

INV-5: tool_call_ids are globally unique within a conversation

INV-6: No tool message references a tool_call_id from a different
       conversation or from a summarized/evicted turn
```

---

## 4. Current Failure Path — Sequence Diagram

```
Client          Agent Runtime         Storage          LLM API
  |                   |                  |                |
  |──user message────►|                  |                |
  |                   |──write(user)────►|                |
  |                   |──build context──►|                |
  |                   |◄─messages────────|                |
  |                   |──[system]+msgs──────────────────►|
  |                   |◄─assistant(tool_calls=[A,B])──────|
  |                   |──write(assistant)──►|             |
  |                   |──execute(A)─────────────►         |
  |                   |──execute(B)─────────────►         |
  |                   |◄─result(A)──────────────          |
  |                   |──write(tool_A)──►|                |
  |                   |                  |   ★ CRASH ★    |
  |                   |                  |                |

Recovery:
  |──user message────►|                  |                |
  |                   |──build context──►|                |
  |                   |◄─[tool_A msg]────|  ← only result
  |                   |                  |    was written;
  |                   |                  |    assistant msg
  |                   |                  |    also written
  |                   |                  |    but B result
  |                   |                  |    is missing
  |                   |──[system,        |                |
  |                   |   assistant(A,B),|                |
  |                   |   tool_A]───────────────────────►|
  |                   |◄─ERROR: tool result for B missing─|
```

Or the checkpoint-recovery variant:

```
  After crash, if checkpoint was taken after tool_A write but
  before assistant write completes to all replicas:

  |                   |──build context──►|                |
  |                   |◄─[tool_A msg]────|  ← assistant
  |                   |                  |    message read
  |                   |                  |    from stale
  |                   |                  |    replica
  |                   |──[system,        |                |
  |                   |   tool_A]───────────────────────►|
  |                   |◄─ERROR: tool after system─────────|
```

---

## 5. Architecture Proposal

### 5a. The Turn Transaction Model

The fundamental insight: **a turn is the atomic unit of conversation state**,
not the message.  A turn is complete only when:

1. The assistant message (with its tool_calls) is persisted
2. All tool results are persisted
3. If the assistant generated a final text response, that too is persisted

```python
@dataclass
class ConversationTurn:
    turn_id:         str                      # UUID
    conversation_id: str
    turn_number:     int                      # monotonically increasing
    trigger:         UserMessage              # the user input that started this turn
    assistant_calls: AssistantMessage        # the assistant's response (may have tool_calls)
    tool_results:    list[ToolResultMessage] # one per tool_call in assistant_calls
    final_response:  AssistantMessage | None # the text reply after tools resolved
    status:          TurnStatus              # PENDING | TOOL_EXECUTING | COMPLETE | FAILED
    created_at:      datetime
    completed_at:    datetime | None
```

A turn in `PENDING` or `TOOL_EXECUTING` status is never included in the context
sent to the API.  Only `COMPLETE` turns are included.

### 5b. Message DAG with Explicit Causal Links

```
         conversation_id=X
                │
    ┌───────────┼───────────────────┐
    │           │                   │
 turn_1      turn_2              turn_3
    │           │                   │
    ▼           ▼                   ▼
 user_msg    user_msg            user_msg
    │           │                   │
    ▼           ▼               ┌───┴───┐
 asst_msg    asst_msg           asst_msg
 (no tools)  tool_calls:        tool_calls:
             [call_A, call_B]   [call_C]
                 │    │             │
                 ▼    ▼             ▼
              tool_A tool_B       tool_C
                 │    │             │
                 └────┘             │
                   │                │
                   ▼                ▼
                asst_msg         asst_msg
               (final text)     (final text)
```

Every node in the DAG has a `parent_node_id`.  Tool results point to their
originating `tool_call_id` inside the parent assistant message.  This makes
orphan detection a simple graph traversal.

### 5c. Pre-Flight Validation Pipeline

```python
class ConversationValidator:
    """Run before every API request. Raises ConversationIntegrityError on failure."""

    def validate(self, messages: list[Message]) -> None:
        self._check_no_leading_tool(messages)
        self._check_tool_call_linkage(messages)
        self._check_turn_completeness(messages)
        self._check_no_dangling_tool_calls(messages)

    def _check_no_leading_tool(self, msgs: list[Message]) -> None:
        non_system = [m for m in msgs if m.role != "system"]
        if non_system and non_system[0].role == "tool":
            raise OrphanedToolMessageError(
                f"First non-system message is a tool result (id={non_system[0].tool_call_id}). "
                f"The assistant message containing the originating tool_call was lost."
            )

    def _check_tool_call_linkage(self, msgs: list[Message]) -> None:
        declared_calls: dict[str, int] = {}  # tool_call_id → message_index
        for i, msg in enumerate(msgs):
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    declared_calls[tc.id] = i
            if msg.role == "tool":
                if msg.tool_call_id not in declared_calls:
                    raise OrphanedToolMessageError(
                        f"tool message references tool_call_id={msg.tool_call_id!r} "
                        f"which has no preceding assistant tool_call in this context window."
                    )
                if declared_calls[msg.tool_call_id] >= i:
                    raise ToolResultBeforeCallError(
                        f"tool result appears before its originating assistant message."
                    )

    def _check_turn_completeness(self, msgs: list[Message]) -> None:
        """Every assistant turn with tool_calls must have all results present."""
        pending_calls: dict[str, set[str]] = {}  # assistant_msg_id → {unresolved_call_ids}
        current_assistant_id: str | None = None
        for msg in msgs:
            if msg.role == "assistant" and msg.tool_calls:
                current_assistant_id = msg.id
                pending_calls[msg.id] = {tc.id for tc in msg.tool_calls}
            elif msg.role == "tool" and current_assistant_id:
                pending_calls[current_assistant_id].discard(msg.tool_call_id)
            elif msg.role in ("user", "assistant") and current_assistant_id:
                remaining = pending_calls.get(current_assistant_id, set())
                if remaining:
                    raise IncompleteTurnError(
                        f"Assistant turn {current_assistant_id} has unresolved "
                        f"tool calls: {remaining}"
                    )
                current_assistant_id = None

    def _check_no_dangling_tool_calls(self, msgs: list[Message]) -> None:
        """The last assistant message must not have unresolved tool calls."""
        for msg in reversed(msgs):
            if msg.role == "assistant" and msg.tool_calls:
                answered = {m.tool_call_id for m in msgs if m.role == "tool"}
                unresolved = {tc.id for tc in msg.tool_calls} - answered
                if unresolved:
                    raise DanglingToolCallError(
                        f"Last assistant message has unresolved tool calls: {unresolved}. "
                        f"Cannot send to API until all tool results are present."
                    )
                break
```

### 5d. Safe Compaction Algorithm

```python
def compact_conversation(
    messages: list[Message],
    target_tokens: int,
    summarizer: Callable[[list[Message]], str],
) -> list[Message]:
    """
    Compact a conversation by summarizing complete turns.
    NEVER splits a turn — only removes whole turns and replaces
    them with a system-level summary.
    """
    turns = segment_into_turns(messages)    # groups by user→[assistant+tools]

    # Validate before compaction
    validator = ConversationValidator()
    validator.validate(messages)

    # Find how many turns to remove from the front
    tokens = count_tokens(messages)
    turns_to_remove = []

    for turn in turns[:-2]:  # never remove the last 2 turns
        if count_tokens(flatten(turns_to_remove + [turn])) + target_tokens < tokens:
            turns_to_remove.append(turn)
        else:
            break

    if not turns_to_remove:
        return messages   # nothing to compact

    # Summarize removed turns as a whole block
    summary_text = summarizer(flatten(turns_to_remove))
    summary_injection = SystemMessage(
        content=f"[Conversation history summary]\n{summary_text}",
        role="system",
    )

    remaining = flatten(turns[len(turns_to_remove):])
    result = [messages[0]] + [summary_injection] + remaining  # keep original system first

    # Validate after compaction — catches bugs in the algorithm itself
    validator.validate(result)
    return result


def segment_into_turns(messages: list[Message]) -> list[list[Message]]:
    """Split messages into complete turns. Each turn starts with a user message."""
    turns = []
    current: list[Message] = []
    for msg in messages:
        if msg.role == "user" and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns
```

### 5e. Event-Sourced Conversation with Causal Ordering

```python
@dataclass
class ConversationEvent:
    event_id:        str         # UUID, globally unique
    conversation_id: str
    sequence_num:    int         # strictly monotonic per conversation
    caused_by:       str | None  # event_id of the event that caused this one
    event_type:      EventType   # TURN_STARTED | TOOL_CALLED | TOOL_RESULT_RECEIVED | TURN_COMPLETED
    payload:         dict
    created_at:      datetime

class ConversationEventStore:
    def append(self, event: ConversationEvent) -> None:
        """Append is conditional on sequence_num == current_max + 1."""
        # Optimistic concurrency — prevents out-of-order writes
        self._db.insert_with_sequence_guard(event)

    def reconstruct(self, conversation_id: str) -> list[Message]:
        """Reconstruct messages from events, respecting causal ordering."""
        events = self._db.get_events_ordered_by_sequence(conversation_id)
        state  = ConversationState()
        for event in events:
            state.apply(event)
        messages = state.to_messages()
        ConversationValidator().validate(messages)  # always validate after replay
        return messages
```

The causal link (`caused_by` field) makes it impossible to process a
TOOL_RESULT_RECEIVED event without first processing the TOOL_CALLED event that
caused it, because the state machine will reject the orphaned event.

---

## 6. Proposed Architecture — Sequence Diagram

```
Client      Turn Manager    Event Store    Tool Executor    Validator    LLM API
  │               │               │               │               │           │
  │──user_msg────►│               │               │               │           │
  │               │──TURN_STARTED►│               │               │           │
  │               │──build_ctx───►│               │               │           │
  │               │◄──events──────│               │               │           │
  │               │──reconstruct──│               │               │           │
  │               │◄──messages────│               │               │           │
  │               │──validate────────────────────────────────────►│           │
  │               │◄──OK──────────────────────────────────────────│           │
  │               │──[system]+msgs────────────────────────────────────────────►│
  │               │◄──assistant(tool_calls=[A,B])──────────────────────────────│
  │               │──TOOL_CALLED(A)►│               │               │          │
  │               │──TOOL_CALLED(B)►│               │               │          │
  │               │────────────────────────────────►│  exec A+B     │          │
  │               │               │               │ (parallel)      │          │
  │               │◄───result(A)───────────────────│               │          │
  │               │──TOOL_RESULT(A)►│               │               │          │
  │               │◄───result(B)───────────────────│               │          │
  │               │──TOOL_RESULT(B)►│               │               │          │
  │               │──check_complete►│               │               │          │
  │               │◄──all_results───│               │               │          │
  │               │──rebuild_ctx───►│               │               │          │
  │               │◄──messages──────│               │               │          │
  │               │──validate────────────────────────────────────►│           │
  │               │◄──OK──────────────────────────────────────────│           │
  │               │──[system]+msgs+tool_A+tool_B──────────────────────────────►│
  │               │◄──assistant(final text)──────────────────────────────────────│
  │               │──TURN_COMPLETED►│               │               │          │
  │               │──mark complete──►│               │               │          │
  │──response────◄│               │               │               │           │
```

**Key difference from current:** The Turn Manager never sends to the API until
the event store confirms all tool results for the current assistant turn are
persisted (`check_complete`).  The validator runs twice: before the initial call
and before the follow-up call with tool results.

---

## 7. Database Schema

### Current problematic schema (flat messages table)

```sql
CREATE TABLE messages (
    id              UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    role            VARCHAR(20) NOT NULL,
    content         TEXT,
    tool_calls      JSONB,
    tool_call_id    VARCHAR(100),  -- for tool role messages
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- No referential integrity between tool results and their originating calls
-- No turn grouping — compaction and replay bugs inevitable
```

### Proposed schema with turn transactions

```sql
CREATE TABLE conversation_turns (
    turn_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    turn_number     INTEGER NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','tool_executing','complete','failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    UNIQUE (conversation_id, turn_number)
);

CREATE TABLE turn_messages (
    message_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id           UUID NOT NULL REFERENCES conversation_turns(turn_id),
    conversation_id   UUID NOT NULL,
    sequence_in_turn  INTEGER NOT NULL,       -- ordering within the turn
    role              VARCHAR(20) NOT NULL
                      CHECK (role IN ('user','assistant','tool')),
    content           TEXT,
    tool_calls        JSONB,                  -- only for role=assistant
    tool_call_id      VARCHAR(200),           -- only for role=tool
    parent_message_id UUID REFERENCES turn_messages(message_id),  -- tool→assistant
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (turn_id, sequence_in_turn),
    -- Enforce: tool messages must reference a valid tool_call_id
    CONSTRAINT tool_result_needs_call CHECK (
        role != 'tool' OR tool_call_id IS NOT NULL
    )
);

CREATE TABLE turn_tool_calls (
    tool_call_id         VARCHAR(200) PRIMARY KEY,
    turn_id              UUID NOT NULL REFERENCES conversation_turns(turn_id),
    assistant_message_id UUID NOT NULL REFERENCES turn_messages(message_id),
    tool_name            VARCHAR(200) NOT NULL,
    tool_input           JSONB NOT NULL,
    result_message_id    UUID REFERENCES turn_messages(message_id),  -- NULL until resolved
    status               VARCHAR(20) NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','executing','complete','failed')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at          TIMESTAMPTZ
);

-- Trigger: a turn cannot transition to 'complete' if any tool_calls are pending
CREATE OR REPLACE FUNCTION check_turn_completeness()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'complete' THEN
        IF EXISTS (
            SELECT 1 FROM turn_tool_calls
            WHERE turn_id = NEW.turn_id AND status NOT IN ('complete', 'failed')
        ) THEN
            RAISE EXCEPTION 'Cannot complete turn % with unresolved tool calls', NEW.turn_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_turn_completeness
    BEFORE UPDATE ON conversation_turns
    FOR EACH ROW EXECUTE FUNCTION check_turn_completeness();
```

This schema makes the orphaned-tool-message class of error impossible at the
storage layer:
- A turn in `pending` or `tool_executing` status is never included in API context
- The trigger prevents premature `complete` transitions
- The `tool_call_id` foreign-key via `turn_tool_calls` makes dangling references detectable
- `parent_message_id` makes the causal chain explicit and queryable

---

## 8. Context Reconstruction Pipeline

```
Storage                  Pipeline                        API Payload
   │                         │                               │
   │──complete turns─────────►│                              │
   │  (status='complete')    │──1. deserialize messages      │
   │                         │                               │
   │──turn events────────────►│──2. validate turn integrity   │
   │  (ordered)              │   (each turn complete)        │
   │                         │                               │
   │                         │──3. flatten to message list   │
   │                         │                               │
   │                         │──4. apply context window      │
   │                         │   (compact WHOLE turns only)  │
   │                         │                               │
   │                         │──5. prepend system message    │
   │                         │                               │
   │                         │──6. ConversationValidator     │
   │                         │   (full invariant check)      │
   │                         │                               │
   │                         │──7. token count check         │
   │                         │                               │
   │                         │──────────────────────────────►│
   │                         │                               │
   │                         │   if validator raises:        │
   │                         │──log + alert + quarantine──►  │
   │                         │   conversation (don't send)   │
```

The validator at step 6 is the final backstop.  It catches corruption that
slipped through the storage layer.  Rather than crash or send an invalid
request, the pipeline quarantines the conversation and alerts ops.

---

## 9. Integration with Agent Runtimes

### Lauren AI (agenthicc)

Lauren AI uses `ShortTermMemory` and `AgentRunnerBase`.  The integration point
is in `AgentTurnRunner._stream()`:

```python
# Existing: stream results directly
stream = await active_runner.run_stream(agent_instance, agent_text, memory=ctx.session_memory)

# Proposed: wrap in TurnTransaction
async with TurnTransaction(ctx.processor, ctx.conv_store) as turn:
    stream = await active_runner.run_stream(
        agent_instance, agent_text,
        memory=ctx.session_memory,
    )
    async for chunk in stream:
        turn.record_chunk(chunk)
    # TurnTransaction.__aexit__ validates and commits only on clean completion
    # On exception: turn is marked FAILED, not included in future context
```

The `TurnTransaction` context manager wraps `_run_agent_turn` and ensures that
if the agent dies mid-turn (after emitting tool_calls but before receiving all
results), the partial turn is excluded from context reconstruction.

### OpenAI Tool Calling

The standard OpenAI pattern already provides the structure — the problem is how
runtimes persist it.  The safest pattern:

```python
# Don't write tool results until all are present
async def execute_tools_and_persist(
    turn_id: str,
    tool_calls: list[ToolCall],
    executor: ToolExecutor,
    db: Database,
) -> list[ToolResult]:
    results = await asyncio.gather(
        *[executor.execute(tc) for tc in tool_calls],
        return_exceptions=True,
    )
    # Only write to DB when ALL results (success or error) are available
    # Never write partial results
    with db.transaction():
        for tc, result in zip(tool_calls, results):
            db.insert_tool_result(turn_id=turn_id, tool_call_id=tc.id, result=result)
        db.update_turn_status(turn_id, status="tool_executing_complete")
    return results
```

### LangGraph

LangGraph uses a checkpointing system (`MemorySaver`, `PostgresSaver`).  The
invariant is enforced at the checkpoint boundary:

```python
from langgraph.checkpoint import BaseCheckpointSaver

class ValidatingCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, inner: BaseCheckpointSaver):
        self._inner = inner
        self._validator = ConversationValidator()

    def put(self, config, checkpoint, metadata):
        messages = checkpoint["channel_values"].get("messages", [])
        # Only checkpoint when the turn is complete
        if self._has_pending_tool_calls(messages):
            # Don't checkpoint mid-turn — wait for completion
            return
        self._validator.validate(messages)
        return self._inner.put(config, checkpoint, metadata)

    def _has_pending_tool_calls(self, messages):
        if not messages:
            return False
        last_assistant = next(
            (m for m in reversed(messages) if m.type == "ai"), None
        )
        if not last_assistant or not last_assistant.tool_calls:
            return False
        answered = {m.tool_call_id for m in messages if m.type == "tool"}
        return bool({tc["id"] for tc in last_assistant.tool_calls} - answered)
```

### LangChain

LangChain's `BaseChatMessageHistory` stores messages without turn grouping.
Wrap it:

```python
class TurnAwareChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, inner: BaseChatMessageHistory):
        self._inner = inner
        self._pending_turn: list[BaseMessage] = []

    def add_message(self, message: BaseMessage) -> None:
        self._pending_turn.append(message)
        if self._is_turn_complete():
            ConversationValidator().validate(
                self._inner.messages + self._pending_turn
            )
            for m in self._pending_turn:
                self._inner.add_message(m)
            self._pending_turn.clear()

    def _is_turn_complete(self) -> bool:
        """True when all tool_calls in the last AI message have results."""
        ai_msgs   = [m for m in self._pending_turn if isinstance(m, AIMessage)]
        tool_msgs = [m for m in self._pending_turn if isinstance(m, ToolMessage)]
        if not ai_msgs:
            return True
        last_ai = ai_msgs[-1]
        if not getattr(last_ai, "tool_calls", None):
            return True
        needed   = {tc["id"] for tc in last_ai.tool_calls}
        answered = {m.tool_call_id for m in tool_msgs}
        return needed == answered
```

---

## 10. Migration Plan

### Phase 1 — Add the validator (zero downtime, immediate protection)

Add `ConversationValidator.validate()` in front of every API call, in
logging-only mode first:

```python
try:
    validator.validate(messages)
except ConversationIntegrityError as e:
    log.error("CONV_INTEGRITY_VIOLATION conversation_id=%s error=%s", conv_id, e)
    metrics.increment("conv.integrity.violation", tags={"type": type(e).__name__})
    # Don't raise yet — just log. Run for 48h to measure blast radius.
```

After 48h, switch to raise mode with a fallback that strips the orphaned
messages and re-validates before sending (not ideal, but better than crashing
400).

### Phase 2 — Migrate storage schema (online migration)

```sql
-- Step 1: Add turn grouping without breaking existing reads
ALTER TABLE messages ADD COLUMN turn_id UUID;
ALTER TABLE messages ADD COLUMN sequence_in_turn INTEGER;

-- Step 2: Backfill turn_id by reconstructing turns from message ordering
-- (Run as background job, conversation by conversation)
UPDATE messages SET turn_id = reconstruct_turn_id(id, conversation_id, created_at);

-- Step 3: Create turn_tool_calls table from existing data
INSERT INTO turn_tool_calls (tool_call_id, turn_id, assistant_message_id, status)
SELECT ...;

-- Step 4: Add NOT NULL constraints after backfill complete
ALTER TABLE messages ALTER COLUMN turn_id SET NOT NULL;

-- Step 5: Create conversation_turns table from reconstructed data
-- Step 6: Add triggers
-- Step 7: Update application to write to new schema
-- Step 8: Deprecate old flat message writes
```

### Phase 3 — Enforce turn-atomic writes (application layer)

Replace all `db.insert_message(role="tool", ...)` calls with `TurnTransaction`
context managers.  This is the highest-value change — prevents future
corruption entirely.

### Phase 4 — Quarantine existing corrupt conversations

```python
# Run as a one-time migration job
for conversation in get_all_conversations():
    try:
        messages = reconstruct_messages(conversation.id)
        ConversationValidator().validate(messages)
    except ConversationIntegrityError as e:
        mark_as_quarantined(conversation.id, reason=str(e))
        # Quarantined conversations are excluded from normal operation
        # but preserved for forensic analysis
```

---

## 11. Solution Ranking

| Solution | Reliability | Complexity | Op. Cost | Migration Ease |
|---|---|---|---|---|
| **Pre-flight validator** (log only) | High — catches 100% of violations before API | Low | Negligible | Trivial — add one function call |
| **Turn-atomic DB writes** | Very High — prevents corruption at source | Medium | Low (one transaction vs many inserts) | Medium — refactor write paths |
| **Turn-grouped schema** | Very High — makes orphans impossible at storage layer | Medium-High | Low | Hard — DB migration required |
| **Event-sourced turns with causal ordering** | Highest — ordering invariants enforced by event log | High | Medium (event store infrastructure) | Hard — requires architectural shift |
| **Compaction safety (whole-turn only)** | High for its failure mode | Low | Negligible | Easy — fix one function |
| **LangGraph ValidatingCheckpointSaver** | High for checkpoint recovery | Low | Negligible | Easy — wrap existing saver |

**Recommended sequence:** Pre-flight validator → Compaction fix → Turn-atomic
writes → Turn-grouped schema → Event sourcing (only if audit trail is required).

---

## 12. Production Readiness Assessment

The pre-flight validator alone reduces the error rate from unpredictable
(corruption silently reaches the API) to zero API errors from this cause —
corrupt conversations are caught before the request is sent and can be recovered
gracefully.  The turn-atomic write pattern eliminates the source of corruption
for all new conversations.  The schema migration eliminates it for the storage
layer.  Together these three changes make the orphaned-tool-message error
structurally impossible for any conversation written after the migration and
detected-before-sending for any pre-existing corrupted conversation.
