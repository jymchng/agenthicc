---
title: "PRD-161: Exploratory Tool-Call Consolidation in the TUI"
status: Implemented
version: 1.1.0
created: 2026-07-31
implemented: 2026-07-31
scope: "Presentation-only grouping of exploratory tool activity in the TUI"
related_prds:
  - PRD-60
  - PRD-66
  - PRD-76
  - PRD-93
  - PRD-94
  - PRD-150
  - PRD-158
tags:
  - tui
  - tools
  - capabilities
  - rendering
  - replay
  - observability
---

# PRD-161 — Exploratory Tool-Call Consolidation in the TUI

## 1. Executive summary

Exploratory agent turns commonly make many read-only calls while locating
relevant source files and symbols. Rendering every call as a full tool card
makes the TUI noisy and hides the useful transition from exploration to an
action or answer.

This PRD defines and implements rendering contiguous exploratory calls as
one derived `Explored` block while retaining every individual call in the
canonical event stream, model context, journal, session log, workflow
checkpoint, replay, and export paths.

The target presentation is:

```text
● Explored
  └ Read command.py, builtins.py, and 5 more files.
  └ Search def _emit|event_sinks|run_sinks in _runner.py
  └ Read agent_turn.py
  └ Search async def _emit|def _emit in _runner.py
  └ Read _signals.py
  └ Read _runner.py, agent_turn_context.py
```

The feature is feasible if it remains a presentation projection. The primary
design decision is how to classify a tool. A new `ToolCapability.EXPLORATORY`
value is technically possible, but `ToolCapability` currently controls mode
blocking and workflow tool filtering. Adding a display-only value to that
permission set could unintentionally remove tools from phases whose allowlist
contains only `READ` or `GIT_READ`.

This PRD therefore evaluates two options and recommends a separate,
presentation-only metadata field or tag that is persisted into the existing
`tool_complete` payload. If the project chooses to reuse `ToolCapability`, it
must first split security capabilities from presentation tags and prove that
all consumers preserve their current semantics.

## 2. Evidence-backed current state

The current source tree establishes these facts:

1. `src/agenthicc/tools/capabilities.py` defines `ToolCapability` values and
   stores them in lauren-ai tool metadata. `ToolCapabilityGate` uses those
   values for mode enforcement.
2. `PhaseSpec.allowed_capabilities` treats a tool's complete capability set as
   an allowlist contract. A tool is included only when its capabilities are a
   subset of the phase's allowed set.
3. `AgentTurnRunner` tracks tool names, arguments, bounded output previews,
   completion status, and durations before appending a `tool_complete` event
   to `ConversationStore`.
4. `ConversationStore` is the reactive TUI source of truth and retains one
   event per tool call. Its existing `tool_group_count` supports the current
   generic tool-call overflow presentation.
5. `ScrollBufferAppender` owns terminal rendering and already maintains local
   presentation state for consecutive tool completions.
6. `ConversationReplayer` re-injects stored events through the normal event
   path. Any grouping decision that depends only on a live registry lookup can
   therefore change when a plugin or classification changes between the
   original run and replay.
7. Session logs and workflow/session state must remain granular for audit,
   resume, idempotency, and model context. A rendered summary cannot become a
   replacement event.

## 3. Problem statement

The current one-row-per-tool presentation has four costs:

- a discovery-heavy turn can occupy most of the scroll area with repetitive
  `Read`, `Search`, and `Inspect` rows;
- the user has difficulty distinguishing harmless exploration from a
  mutation, command, approval, or failure;
- the existing generic tool-call overflow behavior is count-based rather than
  semantically aware;
- inferring exploratory behavior from tool names is brittle for plugins,
  custom workflows, MCP tools, and optional browser/terminal integrations.

The desired UX is semantic compression: reduce visual repetition without
reducing execution, persistence, safety, or diagnostic information.

## 4. Goals

The feasibility work and any follow-on implementation MUST:

1. Render contiguous, explicitly classified exploratory calls as a readable
   `Explored` block.
2. Preserve one canonical event and one result for every underlying tool call.
3. Keep mutations, commands, approvals, failures, and assistant responses
   prominent and outside successful exploration summaries.
4. Support built-in tools and explicit opt-in for plugin, MCP, and
   workflow-scoped tools.
5. Make live rendering, replay, resume, and export deterministic for the same
   event sequence.
6. Reuse existing redaction, argument formatting, capability gates, session
   logging, and `ScrollBufferAppender` ownership boundaries.
7. Permit a feature-flagged rollout with no change to tool execution when the
   feature is disabled.

## 5. Non-goals

- Do not combine tool calls into one executor request or one model-visible
  tool result.
- Do not remove, merge, reorder, or rewrite canonical conversation events.
- Do not change mode permissions, approvals, sandbox policy, network policy,
  retries, idempotency, checkpoints, or workflow transitions.
- Do not classify tools from names alone.
- Do not group writes, shell/python execution, terminal lifecycle changes,
  browser navigation/interactions, approvals, errors, or arbitrary unknown
  plugin calls by default.
- Do not create a second transcript, journal, event store, or tool registry.
- Do not persist only a summary in place of the underlying tool records.

## 6. Ownership and architecture

| Concern | Canonical owner | Feasibility requirement |
|---|---|---|
| Security capabilities | `src/agenthicc/tools/capabilities.py`, `capability_gate.py` | Must retain current permission/filter semantics. |
| Tool lifecycle | lauren-ai executor and `src/agenthicc/tools/hooks.py` | Continue one call, one result, one completion lifecycle. |
| Completion projection | `src/agenthicc/runners/agent_turn.py` | Add only safe presentation metadata if required. |
| Canonical reactive history | `src/agenthicc/tui/conversation_store.py` | Continue storing individual `tool_complete` events. |
| Terminal rendering | `src/agenthicc/tui/workspace/appender.py` | Derive and render the exploration group. |
| Replay | `src/agenthicc/tui/runtime/replay.py` | Reuse the same raw event payload and grouping rules. |
| Session persistence | `src/agenthicc/tui/runtime/session_log.py` | Persist individual events; no summary-only records. |
| Workflow filtering | `PhaseSpec` and workflow runners | Must not lose tools because of a UI-only marker. |

The appender remains the only component responsible for `console.print()`.
The grouping state is presentation state and must not become domain state in
the kernel or an alternate conversation store.

## 7. Classification options

### Option A — Add `ToolCapability.EXPLORATORY`

Add a value to the existing enum and compose it with security capabilities:

```python
{ToolCapability.READ, ToolCapability.EXPLORATORY}
```

Advantages:

- tool metadata already travels with the callable;
- `get_tool_capabilities()` is available at the existing integration points;
- custom workflow authors already understand capability decorators.

Risks:

- `PhaseSpec` currently uses subset semantics, so a phase allowing only
  `READ` would exclude `{READ, EXPLORATORY}` unless every filter is revised;
- mode and approval code may accidentally treat a presentation tag as a
  permission requirement;
- serialized capability lists, introspection output, and third-party tools
  would gain a new value with no security meaning;
- a future security policy could accidentally block or require the tag.

This option is acceptable only if security capabilities and UX tags are split
at every consumer, with regression tests proving that all existing mode and
workflow tool sets are unchanged.

### Option B — Separate presentation metadata (recommended)

Keep `ToolCapability` exclusively for permission and execution policy. Add a
typed tool metadata field, for example:

```python
ToolPresentation(
    exploratory=True,
    operation="Read",
    target_kind="path",
)
```

or the smallest compatible equivalent under the existing lauren-ai metadata
store. `exploratory` is a UI classification, not an authorization grant.

Advantages:

- no changes to mode blocks or `PhaseSpec` capability subsets;
- the distinction between security and display policy is explicit;
- plugin and workflow authors can opt in without weakening or changing tool
  permissions;
- the event payload can carry a resolved, stable presentation decision for
  replay.

Risks:

- a second metadata lookup/API must be documented;
- the projection must resolve metadata for dynamically loaded tools;
- the metadata contract needs validation and safe defaults.

**Recommendation:** use Option B. If compatibility with the public
`ToolCapability` API is judged more valuable, create an explicit adapter that
maps a capability declaration to presentation metadata without including the
display marker in security-capability subset checks.

### Rejected option — Name-based registry

A central set of names such as `read_file`, `search_files`, and `git_diff`
would be quick to prototype but would drift when tools are renamed, wrapped,
loaded from plugins, or provided by MCP. It also makes custom workflow tools
impossible to classify without modifying a global list.

## 8. Initial classification policy

Unknown or unmarked tools MUST render individually. The initial built-in audit
should consider these candidates:

| Tool family | Initial recommendation | Reason |
|---|---|---|
| `read_file`, `read_lines`, `batch_read` | Exploratory | Bounded workspace inspection. |
| `list_directory`, `search_files`, `grep_file`, `grep_files`, `file_exists`, `get_file_info`, `checksum_file` | Exploratory | Discovery and metadata inspection. |
| `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`, `git_grep`, `git_branch` | Exploratory candidate | Repository inspection; confirm output and latency in the spike. |
| `semantic_search` and read-only memory tools | Exploratory candidate | Knowledge discovery; confirm that result previews are safe and useful. |
| `inspect_terminal` | Explicit review required | Reads a live process surface and may be part of an operational sequence. |
| browser snapshots/search observations | Explicit review required | Read-only output still crosses a network/browser trust boundary. |
| `playwright_open`, browser clicks/fills/presses/waits/screenshots | Not by default | Navigation and browser state can have side effects or require attention. |
| shell, Python, command, test, terminal start/wait/stop | Not by default | A read-looking command can mutate state or control a process. |
| file/git writes, workflow controls, approvals | Never by default | Preserve action and decision prominence. |

The final table is a deliverable of the feasibility spike, not an implicit
authorization policy. `EXPLORATORY` MUST never be used to bypass an existing
capability gate.

## 9. Proposed event and data model

The canonical event remains one `tool_complete` event per call. Add an
optional presentation-only field, backward-compatible with existing logs:

```json
{
  "kind": "tool_complete",
  "payload": {
    "tool_use_id": "call-123",
    "name": "read_file",
    "success": true,
    "args_str": "('src/agenthicc/config.py')",
    "output_lines": ["..."],
    "output_more": 14,
    "presentation": {
      "exploratory": true,
      "operation": "Read",
      "target": "src/agenthicc/config.py"
    }
  }
}
```

The exact field shape is subject to the spike. Required invariants are:

- absent or malformed presentation metadata means individual rendering;
- `target` is already bounded, redacted, and safe for display;
- raw tool arguments and raw tool output are not copied into a new UI field;
- presentation metadata is not sent to the model as a new tool result;
- the marker is stored with the event if replay must reproduce the original
  classification after plugin metadata changes;
- old session logs without the field continue to render normally.

The projection may derive a target from the same safe argument formatter used
by `AgentTurnRunner`, but it must not parse markup strings in the appender.
Prefer producing structured safe display data before the event is appended.

## 10. Grouping and rendering behavior

### 10.1 Group boundaries

An exploration group starts on the first successful, marked exploratory
`tool_complete` event in a rendering scope. Subsequent marked exploratory
completions join the group while they remain contiguous.

The group MUST flush before rendering:

- an unmarked or non-exploratory tool completion;
- an exploratory failure or tool error;
- assistant text or a user message;
- a turn-start, turn-end, workflow/phase boundary, or replay boundary;
- an approval, question, system, terminal, file-diff, or other visible event;
- appender unmount, session shutdown, or feature disablement.

The feature must not wait indefinitely for a future event. The first
exploratory call should render promptly as the group header and first child;
later children can be appended to the same visual structure while the group
is open. If a look-ahead buffer is chosen instead, its maximum delay must be
bounded and failures/responses must flush it immediately.

### 10.2 Target presentation

The first implementation should use existing operation labels (`Read`,
`Search`, `Inspect`) and a safe bounded target supplied by the projection.
Repeated calls may be coalesced by operation, but distinct targets must not be
silently discarded. The renderer must preserve the user's ability to
distinguish:

```text
Read command.py, builtins.py
Search def _emit|event_sinks|run_sinks in _runner.py
```

It must not expose passwords, tokens, cookies, authorization values, full
command contents, or unbounded result text in the aggregate label.

### 10.3 Bounds

The spike must establish and test bounded defaults. Recommended starting
limits are:

- 12 visible child rows per group;
- 3 targets per child row;
- 80 characters per target after redaction/truncation;
- one explicit `…and N more exploratory calls` summary when capped;
- no unbounded in-memory accumulation in the appender.

The existing generic tool overflow counter must not produce a duplicate
summary for calls handled by the exploration renderer.

### 10.4 Failure and error handling

Failures are not successful exploration. An exploratory failure must flush the
successful group, render a red/failed tool result using the existing safe
preview, and leave the next exploratory call eligible for a new group.
Exceptions must remain subject to the existing structured tool-error and
redaction contracts.

## 11. Data flow

```text
Agent selects a tool
        │
        ▼
Callable metadata
  security caps: READ / GIT_READ / SEARCH / ...
  presentation tag: exploratory=true (optional)
        │
        ▼
Existing executor, hooks, mode gate, approval gate, and tool call
        │
        ├── model receives the normal individual tool result
        ├── journal/session log records the individual call
        ├── workflow/checkpoint state remains individual and idempotent
        └── AgentTurnRunner creates one safe tool_complete event
                         │
                         ▼
              ConversationStore
              one event per call
                         │
                         ▼
             ScrollBufferAppender
     checks presentation.exploratory and boundaries
                         │
           ┌─────────────┴─────────────┐
           ▼                           ▼
     append child to group       flush group, then render
           │                           │
           └─────────────┬─────────────┘
                         ▼
               Rich TUI presentation
```

The same raw event payload must drive replay:

```text
session log → ConversationReplayer → ConversationStore → appender
```

No grouped summary is used as an idempotency key, checkpoint identity, or
replacement for the raw tool event.

## 12. Feasibility spike

Build the deterministic prototype behind the feature flag before committing to
the production rendering policy. The spike must answer:

1. Can presentation metadata be resolved for built-ins, project plugins,
   workflow-scoped tools, and MCP tools at completion time?
2. Does the recommended separate metadata field work with lauren-ai's
   callable metadata without changing tool schemas or model prompts?
3. Can the `tool_complete` payload carry a stable safe marker and target
   through session-log serialization and replay?
4. Can the appender render a group without unbounded buffering or delayed
   failures/responses?
5. Can it coexist with the existing generic tool-group overflow behavior?
6. Are turn, workflow phase, approval, system, terminal, and replay boundaries
   observable at the appender's ownership boundary?
7. Does adding `EXPLORATORY` to `ToolCapability` change mode filters,
   `PhaseSpec` tool sets, role defaults, introspection output, or custom
   workflow behavior? If yes, Option A is rejected unless the semantics are
   explicitly split.
8. Do rendered targets remain useful without exposing sensitive arguments or
   result content?
9. Does grouping reduce visual rows on representative planning turns without
   changing event count, execution count, token usage, or persistence?

Required deterministic fixture sequences:

```text
explore → explore → explore → assistant text
explore → explore → write
explore → shell/command
explore → exploratory failure
explore → approval/system boundary → explore
explore → turn boundary → explore
explore → replay/resume
```

The spike should snapshot both the raw event stream and rendered output.

## 13. Functional requirements

### FR-1 — Explicit, presentation-only classification

The system MUST support an explicit exploratory marker for built-in and
opt-in custom tools. The marker MUST NOT grant, revoke, or alter security
capabilities.

### FR-2 — Safe defaults

Unmarked, malformed, unknown, mutating, executing, approval, terminal-control,
and browser-interaction tools MUST remain individually rendered.

### FR-3 — Granular canonical events

Every executed tool call MUST continue to produce its existing individual
completion/result and persistence records. Grouping MUST be a derived TUI
projection.

### FR-4 — Contiguous grouping

Only contiguous marked exploratory successes in one rendering scope MAY be
grouped. All defined boundaries MUST flush the active group.

### FR-5 — Deterministic replay

Live rendering, replay, and resumed rendering MUST make the same grouping
decision for the same event payload sequence, including when tool metadata is
no longer available.

### FR-6 — Failure prominence

Exploratory failures and exceptions MUST remain visible as failures and MUST
NOT be represented as a successful `Explored` block.

### FR-7 — Bounded and redacted display

Aggregate labels, targets, child counts, and previews MUST use the existing
redaction/truncation policies and enforce independent output limits.

### FR-8 — Feature control

The implementation MUST be disableable without disabling tools, changing tool
execution, or changing persisted raw events. The shipped implementation
defaults to enabled and retains the legacy renderer when disabled.

## 14. Non-functional requirements

- **Correctness:** no call, retry, approval, mode decision, workflow
  transition, checkpoint, or idempotency operation changes because of grouping.
- **Performance:** grouping is O(events rendered), adds no LLM calls, and
  does not create unbounded memory or a per-event metadata scan of all tools.
- **Latency:** the first exploratory call and every boundary event render
  promptly; a pending group cannot hide a response or failure.
- **Security:** aggregate display data is safe, redacted, bounded, and never
  an authorization path.
- **Compatibility:** old logs and tools without metadata render as today;
  existing tool-collapse behavior remains correct when the feature is off.
- **Determinism:** the same raw event sequence produces the same group
  boundaries in live, replay, and resume paths.
- **Accessibility:** the `Explored` label and child rows are understandable in
  plain terminal output and exported/replayed views.
- **Maintainability:** grouping logic is a small deterministic presentation
  reducer, not a second execution or persistence subsystem.

## 15. Testing and acceptance criteria

### Unit tests

1. Presentation metadata is independent of `ToolCapability` enforcement.
2. Marked and unmarked tools resolve correctly; malformed metadata fails safe.
3. If Option A is prototyped, existing mode blocks and `PhaseSpec` allowlists
   are byte-for-byte equivalent for all current tools.
4. Group state starts, appends, coalesces, caps, and flushes correctly.
5. Every boundary in section 10.1 flushes the group.
6. Exploratory failure paths render as failures and do not join successful
   groups.
7. Existing argument redaction and target truncation are preserved.
8. Old event payloads without presentation metadata render individually.
9. Identical event sequences produce identical grouping decisions.

### Integration tests

1. Built-in filesystem and git metadata reaches the completion projection.
2. A plugin/custom workflow can opt in without changing its security caps.
3. `ConversationStore` receives one `tool_complete` event per tool call.
4. Session-log subscribers receive one record per tool call, not one summary.
5. Replay and resume reproduce live group boundaries.
6. The generic tool overflow behavior does not duplicate exploration summaries.
7. Browser and terminal boundaries remain individual and flush exploration.
8. Feature disablement preserves existing rendering and raw event payloads.

### End-to-end tests

Using deterministic headless TUI fixtures, verify:

1. A source-discovery turn renders one `Explored` block containing the
   representative `Read` and `Search` rows.
2. Exploration followed by `write_file` shows the group before the mutation.
3. Exploration followed by a command shows the group before the command.
4. Exploration followed by assistant text flushes before the response.
5. A failed exploratory call is visibly failed and not hidden in the group.
6. A resumed session renders the same grouping from raw session events.
7. A custom workflow's marked exploratory tools group while its unmarked
   custom tools remain individual.
8. Disabling the feature restores current per-tool rendering with no execution
   or persistence difference.

### Acceptance criteria for feasibility

The feasibility study is successful only when:

- the prototype consolidates at least 90% of targeted exploratory rows in the
  representative fixture without removing any raw event;
- raw event count/order/payloads, journal records, checkpoint data, execution
  count, and model-visible tool results are unchanged;
- no mutation, command, approval, failure, response, terminal event, or
  browser interaction is accidentally absorbed into a successful group;
- live and replay/resume rendering agree on all tested boundaries;
- no measurable LLM-token, tool-latency, or persistence regression is found;
- built-ins and opt-in custom tools can be classified without a global name
  registry;
- the final metadata option does not change current mode or workflow tool
  availability;
- maintainers approve the classification table, limits, feature-flag default,
  and security review.

## 16. Rollout and migration

1. Land documentation and a metadata/classification audit without changing
   rendering.
2. Build the deterministic grouping reducer behind the feature flag.
3. Add unit, integration, headless E2E, replay, and security regression tests.
4. Enable it for maintainers using captured fixtures and review the TUI at
   narrow terminal widths.
5. Enable the default-on presentation after measuring readability and failure
   visibility.
6. Roll back by disabling the flag; no persistence migration is permitted for
   the first release.

Existing tools and custom workflows remain safe by default because missing
exploratory metadata means individual rendering. Documentation must state
that opting in changes presentation only and does not grant permission.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Useful detail is compressed too aggressively | Keep bounded child rows, explicit overflow counts, and raw event/replay access. |
| A read-labelled tool has hidden side effects | Require explicit opt-in and review each built-in; never infer from names. |
| `EXPLORATORY` changes security filtering | Prefer separate presentation metadata; if reusing the enum, test every consumer and separate required capabilities. |
| Group state crosses turns or phases | Flush on all scope/boundary events and test replay/resume. |
| Plugin metadata is unavailable during replay | Persist the resolved safe presentation marker in the event payload. |
| A pending buffer delays failures or responses | Render the first child promptly and flush immediately on every non-exploratory event. |
| Existing generic collapse produces duplicate summaries | Define one appender state machine and add mixed-sequence integration tests. |
| Aggregate labels expose secrets | Build labels before rendering with existing redaction and test secret-shaped arguments. |
| Browser/terminal activity appears harmless | Keep those families unmarked until an explicit trust-boundary review. |

## 18. Open questions and decision record

The implementation PRD must resolve:

1. Is separate presentation metadata accepted, or will the project split
   `ToolCapability` into security capabilities and UX tags?
2. Which exact built-ins are exploratory in the first release?
3. Is `presentation.exploratory` sufficient, or are structured operation and
   target fields needed for the requested display quality?
4. Should repeated targets be coalesced by operation, path type, or neither?
5. What terminal-width and output limits provide the best scanability?
6. Are browser snapshots and terminal inspection included in the first
   release or explicitly deferred?
7. Does the product need an interactive expansion later, or is raw replay and
   session export sufficient?

The decision record MUST include the final metadata shape, classification
table, grouping state machine, feature flag/default, replay contract, security
review, performance measurements, and explicit ungrouped-tool list.

## 19. Verification commands

The eventual implementation must run the relevant repository gates:

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/agenthicc
uv run python scripts/type_audit.py --check docs/reference/type-safety-baseline.json
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest tests/e2e -q
uv run pytest tests/ -q
```

The implementation reports deterministic snapshot output and the raw-event
invariants before any future default-on rollout is approved.

## 20. Implementation decision and evidence

The PRD is implemented with the following decisions:

- `presentation.exploratory = true` is a separate lauren-ai metadata field.
  `ToolCapability` remains unchanged, so mode gates and `PhaseSpec` allowlists
  have identical security semantics.
- Built-in filesystem readers, filesystem search/inspection tools, and
  read-only git inspection tools are marked explicitly. Writes, execution,
  terminal control, browser actions, network tools, and workflow controls are
  unmarked. Plugins and workflow-scoped callables opt in with
  `@tool_exploratory`; class-based `Tool` and `ToolBase` implementations can
  set `exploratory = True`.
- `AgentTurnRunner` resolves the marker once for the visible tool set and
  stores only a bounded, redacted target on each individual `tool_complete`
  event. The target is never sent as a model result and is safe to replay.
- `ScrollBufferAppender` is the only grouping reducer. It groups contiguous
  successful marked events, caps visible children at 12, bounds targets to 80
  characters, flushes on all non-tool/boundary events and unmount, and renders
  failures individually. The canonical `ConversationStore` counter and
  session log remain granular.
- `[tools].group_exploratory_calls` is the feature flag. It defaults to `true`
  for the requested consolidated presentation and can be disabled without
  changing tool execution or persistence. When disabled, marked events use the
  legacy individual renderer.
- The implementation is covered by unit tests for classification, redaction,
  grouping, boundaries, failures, caps, configuration, legacy mode, and typed
  adapters; integration tests for plugin metadata and session-log round trips;
  and E2E tests for live and resumed TUI journeys.
