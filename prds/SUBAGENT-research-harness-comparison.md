---
title: "SUBAGENT research: how coding-agent harnesses manage subagents and their tools"
status: Historical
version: 0.2.0
date: 2026-10-26
snapshot_commit: f843040644db374260ebb272577f4f3d8f976953
scope: "cross-harness subagent research, agenthicc evidence baseline, capability taxonomy, and PRD house-format contract for the SUBAGENT series"
related_prds:
  - PRD-124  # concurrent typed subagents
  - PRD-180  # subagent artifact writing and research handoffs
  - PRD-197  # subagent runtime policy inheritance (implemented)
  - PRD-214  # reliable subagent findings and file-backed handoff
  - PRD-232  # durable agent-team task graph and communication
  - PRD-233  # declarative agent profiles and per-agent policy
  - AGX-08   # subagent panel and child-session navigation
tags:
  - subagents
  - research
  - competitive-analysis
  - tooling
  - orchestration
---

# SUBAGENT research — how coding-agent harnesses manage subagents and their tools

This document is a dated evidence snapshot, not a requirements document. It
records what the `agenthicc` subagent subsystem actually does at commit
`f843040`, what six other coding-agent harnesses do, and the resulting
capability taxonomy. The requirements live in the `SUBAGENT-NN` PRDs that this
report links to; nothing here is an implementation obligation by itself.

`status: Historical` is deliberate and uses the vocabulary fixed by
[`prds/README.md`](README.md): the document is frozen at a commit and is
expected to drift out of date as the source moves. Re-verify every line
reference before citing it in a new PRD.

**Document roadmap**

| Section | Content | Written by |
|---|---|---|
| §1 | Evidence baseline: current agenthicc source, confirmed gaps, prior art | implemented |
| §2 | Harness-by-harness findings (Grok CLI, Claude Code, Codex CLI, opencode, DeepSeek harness, Qoder CLI) | implemented |
| §3 | Cross-harness feature matrix | implemented |
| §4 | Gap analysis: competitor capability → agenthicc evidence | implemented |
| §5 | Locked capability taxonomy and area-to-PRD bijection | implemented |
| §6 | SUBAGENT PRD house-format contract | pending |

Snapshot identity for the external evidence: open-source harnesses were cloned
at `--depth 1` and are cited by full commit SHA; closed-source harnesses were
read from their vendor documentation on 2026-10-29 and are cited by page URL
and quoted heading, never by a fabricated file path.

---

## 1. Evidence baseline: the current `agenthicc` subagent subsystem

### 1.1 Method

Every claim in this section is a *read* of the tree at
`f843040644db374260ebb272577f4f3d8f976953` (2026-10-26, *"feat: fixed agents
not working"*). Line numbers were produced with `grep -n` and `sed -n` against
the working tree at that commit, not from memory.

Two kinds of statement appear below and they are labelled differently:

- **Present** claims cite `file:line` and can be re-checked by reading that
  line.
- **Absent** claims are only as strong as the search that failed to find the
  thing. Each one names the exhaustive pattern and the search space, so a
  reader can reproduce the negative result. `src/agenthicc/subagents/` is
  small enough (4,309 lines total) that an exhaustive grep there is
  conclusive; a negative result anywhere else in the repository is not claimed
  unless stated.

### 1.2 Subsystem inventory

| File | Lines | Responsibility |
|---|---:|---|
| `src/agenthicc/subagents/pool.py` | 1,685 | `SubagentWorker` execution, `SubagentPool` orchestration, result models, TUI state projection, kernel event emission |
| `src/agenthicc/subagents/communication.py` | 944 | `AgentMessageBroker` (messages, questions, answers), `PoolContinuationRegistry`, child- and parent-facing communication tools |
| `src/agenthicc/subagents/tool.py` | 563 | `make_spawn_subagents_tool`, task validation, `_augment_type_schema`, result caching |
| `src/agenthicc/subagents/transcripts.py` | 442 | `TranscriptWriter`, `read_transcript`, `TranscriptTail`, redaction and compaction |
| `src/agenthicc/subagents/types.py` | 441 | `SubagentTypeSpec`, `SubagentTypeRegistry`, `SubagentAggregator`, the nine built-in specs |
| `src/agenthicc/subagents/policy.py` | 181 | `SubagentExecutionPolicy` snapshot, child `AppState` and workspace-policy construction |
| `src/agenthicc/subagents/__init__.py` | 53 | public re-exports (20 names in `__all__`) |

Public surface re-exported from `src/agenthicc/subagents/__init__.py:32-53`:
`SubagentTypeSpec`, `SubagentAggregator`, `SubagentTypeRegistry`,
`DEFAULT_REGISTRY`, `DEFAULT_SUBAGENT_TIMEOUT_S`, `SubagentTask`,
`SubagentResult`, `AggregatedResult`, `SubagentWorker`, `SubagentPool`,
`run_pool`, `make_spawn_subagents_tool`, `SubagentExecutionPolicy`,
`AgentMessageBroker`, `AgentMessageEnvelope`, `CommunicationError`,
`PoolContinuationRegistry`, `PoolHandle`, `make_child_communication_tools`,
`make_parent_communication_tools`.

### 1.3 The type model as it stands

`SubagentTypeSpec` is declared at `src/agenthicc/subagents/types.py:60-91` and
carries exactly five fields:

| Field | Line | Meaning |
|---|---:|---|
| `name` | `types.py:87` | type identifier used in `spawn_subagents(tasks=[...])` |
| `allowed_tools` | `types.py:88` | ceiling applied after parent phase/mode filtering |
| `max_turns` | `types.py:89` | maximum tool-call → response cycles per worker |
| `system_prompt` | `types.py:90` | self-contained role instruction |
| `max_turn_time_s` | `types.py:91` | wall-clock timeout, default `DEFAULT_SUBAGENT_TIMEOUT_S` |

There is no `description`, `model`, `reasoning_effort`, `output_schema`,
`deny_tools`, `budget`, or `priority` field. That is the whole type contract.

Nine built-in specs are hard-coded in a module-level list at
`types.py:309-371` (`_BUILTIN_SPECS`), over the tool-name frozensets at
`types.py:182` (`_EXPLORER_TOOLS`), `:199` (`_PLANNER_TOOLS`), `:211`
(`_IMPLEMENTER_TOOLS`), `:233` (`_EXECUTOR_TOOLS`), `:243` (`_TESTER_TOOLS`),
`:258` (`_REVIEWER_TOOLS`), `:273` (`_DOCUMENTER_TOOLS`), `:284`
(`_VERIFIER_TOOLS`), `:299` (`_RESEARCHER_TOOLS`).

`SubagentTypeRegistry` (`types.py:375-431`) offers `register`, `get`,
`register_aggregator`, `get_aggregator`, `names`, and `__contains__`.
`DEFAULT_REGISTRY` is built once at import time (`types.py:433-441`, assignment at `:441`), so a
project cannot declare a type without importing Python and mutating
process-global state.

The model-facing schema has two layers. `BuiltinSubagentType` is a `Literal` of
the nine names at `src/agenthicc/subagents/tool.py:55-65`, which gives
lauren-ai a static enum; `_augment_type_schema` (`tool.py:512-563`) then merges
runtime registry names into that enum at `tool.py:559-563`. Plugin types are
therefore *selectable* by the model, but only as bare name strings — the enum
carries no description, so the parent model chooses between types with no
stated distinction beyond the name itself. `_SUBAGENT_TYPE_ALIASES`
(`tool.py:68`) maps `research` → `researcher`.

### 1.4 Execution path and concurrency

`spawn_subagents` is declared at `tool.py:187-191` with three parameters:
`tasks`, `max_concurrent`, and `timeout_s`. The default concurrency is `4`,
set at `tool.py:95` and again at `pool.py:1120`; `run_pool` repeats it at
`pool.py:1643`.

The pool bounds *active provider calls* with a semaphore —
`semaphore = asyncio.Semaphore(self._max_concurrent)` at `pool.py:1309`,
acquired at `pool.py:1313`. That is the only concurrency control in the
subsystem.

`SubagentPool.run()` (`pool.py:1217`) awaits every worker and returns an
`AggregatedResult`, so the parent turn blocks until the whole pool is done.
`PoolContinuationRegistry` (`communication.py:727-760`) plus
`collect_subagent_results` (`communication.py:927`) exist, but they serve
resumed-collection of an already-finished pool, not a fire-and-return spawn.

### 1.5 Model, memory, and policy inheritance

Workers are bound to the parent's model. `self._parent_model` is stored at
`pool.py:648` and `pool.py:1143`, and the worker is decorated at
`pool.py:849`:

```python
@agent_decorator(model=self._parent_model, system=system)
```

Usage recording reuses the same value at `pool.py:940`, so per-worker ledger
rows are attributed to the parent's model ID. A subagent cannot run a cheaper
or stronger model than its parent, and there is no per-type override.

Worker memory is fixed at 8,000 tokens: `memory = _ObservedMemory(max_tokens=8_000)`
at `pool.py:958` and `memory = ShortTermMemory(max_tokens=8_000)` at
`pool.py:960`. The `_ObservedMemory` subclass (`pool.py:951`) exists only to
observe pressure; the budget itself is a constant.

Policy inheritance is the one area that is already current.
`SubagentExecutionPolicy` (`policy.py:25-45`) snapshots `mode_name`, `badge`,
`color`, `description`, `system_prompt_suffix`, `blocked_capabilities`,
`approval_required`, `visible_tool_names`, `workspace_scope_identity`,
`effective_workflow`, `effective_phase`, and a derived `policy_revision`
(`policy.py:75-88`). `from_app_state` (`policy.py:48-58`) captures the parent
mode without sharing its signal, and `pool.py:332` (`_make_child_app_state`)
and `pool.py:339` (`_make_child_workspace_access`) build an isolated child
`AppState` and workspace policy from that snapshot. This is
[PRD-197](prd-197-subagent-runtime-policy-inheritance.md), status `Implemented`.

The consequence for this study: mode, capability block, approval requirement,
prompt suffix, and workspace scope *are* inherited, but the only per-type
narrowing mechanism is the single `allowed_tools` ceiling
(`types.py:88`, described as a ceiling at `types.py:83-84`).

### 1.6 Results and aggregation

`SubagentResult` (`pool.py:318-330`) carries `task_id`, `agent_type`, `label`,
`ok`, `text`, `error`, `duration_ms`, `tool_calls`, `changed_paths`. `text` is
documented at `pool.py:325` as *"AgentResponse.content or a tool-evidence
fallback (plain text)"*.

`AggregatedResult` (`pool.py:353-361`) carries `pool_id`, `total`, `succeeded`,
`failed`, and `text`, documented at `pool.py:360` as the *"labelled
concatenation delivered to parent as tool result"*.

`SubagentAggregator` (`types.py:36-58`) lets a plugin replace that concatenation
for one type, but its contract at `types.py:51` is
`aggregate(results: list[SubagentResult]) -> str`, so the replacement is also
unstructured text.

### 1.7 Communication and observability

`AgentMessageBroker` (`communication.py:147`) supports directed messages
(`send`, `:263`), questions and answers (`open_question` `:288`,
`answer_question` `:326`, `wait_for_answer` `:352`), polling (`:383`),
acknowledgement (`:417`), and pending-question projection (`:460`). Child-side
tools are built at `communication.py:780-876`; parent-side tools at
`communication.py:878-944` (`answer_subagent` `:893`, `send_subagent_message`
`:909`, `collect_subagent_results` `:927`).

Durable transcripts exist: `TranscriptWriter` (`transcripts.py:147`),
`read_transcript` (`:317`), `TranscriptTail` (`:343`), with redaction
(`transcripts.py:209`, and `pool.py:1446`) and compaction
(`transcripts.py:267`). The pool emits `SubagentPoolStarted`
(`pool.py:1480`), worker started (`:1504`), worker done (`:1522`), and pool
completed (`:1545`).

The TUI surface is real and shipped: `src/agenthicc/tui/workspace/subagent_panel.py`
(6,258 bytes), `src/agenthicc/tui/workspace/overlays/subagents.py`
(17,293 bytes), and the ten modules under
`src/agenthicc/tui/workspace/agents_hub/`. [AGX-08](agx-08-subagent-panel-and-navigation.md)
is `Implemented (phase 1)`.

What is missing is not the mechanism but the *contract*: the transcript event
shape is defined by `TranscriptEvent` (`transcripts.py:63-107`) while the
kernel events are defined by the pool's emit methods, and no document states
that these two surfaces are the same schema. Consumers bind to whichever one
they found.

### 1.8 Test surface

Existing coverage that a new PRD must not break and should extend:

| Level | Files |
|---|---|
| unit | `tests/unit/test_subagent_pool.py`, `test_subagent_pool_runtime_coverage.py`, `test_subagent_communication.py`, `test_subagent_resume.py`, `test_subagent_plugin.py`, `test_subagent_tui.py`, `test_subagent_panel_agx08.py` |
| integration | `tests/integration/test_subagent_tool_execution.py`, `test_subagent_executor_integration.py`, `test_subagent_panel_agx08.py` |
| e2e | `tests/e2e/test_subagent_executor_e2e.py` |

Focused verification command for any SUBAGENT PRD:

```bash
python -m pytest tests/unit tests/integration tests/e2e -k subagent -q
```

### 1.9 Confirmed gaps

Each gap is stated as an evidenced claim, then the absence proof where the gap
is a missing capability.

| ID | Gap | Evidence |
|---|---|---|
| G-1 | Type definitions are code-only and carry no model-facing description | `types.py:60-91` (five fields, no `description`); `types.py:309-371` hard-coded specs; `DEFAULT_REGISTRY` at import `types.py:433-441`; enum merged as bare names `tool.py:559-563` |
| G-2 | No per-type model, reasoning-effort, or context-budget routing | `pool.py:849` worker decorated with `model=self._parent_model`; `pool.py:940` usage attributed to parent model; `pool.py:958`/`:960` memory fixed at `8_000` |
| G-3 | Results are unstructured text with no output contract | `pool.py:325` (`text`, plain text), `pool.py:360` (labelled concatenation), `types.py:51` (`aggregate() -> str`), and the PRD-124 amendment at `prds/prd-124-concurrent-typed-subagents.md:6-10` |
| G-4 | Workers cannot delegate; no depth or fan-out governance | `spawn_subagents` appears in `types.py` only inside docstrings (`:29`, `:67`, `:82`) and in none of the nine `allowed_tools` frozensets (`types.py:182-306`); the only bound is the semaphore at `pool.py:1309` with default `4` (`pool.py:1120`) |
| G-5 | Per-type permission scoping is one `allowed_tools` ceiling | `types.py:88`; ceiling semantics stated at `types.py:83-84`; no deny-list, approval override, or per-type workspace narrowing anywhere in `policy.py` or `types.py` |
| G-6 | No token, cost, or aggregate-runtime budget | `grep -rniE "budget\|max_cost\|cost_limit\|token_limit\|abort_threshold" src/agenthicc/subagents/` returns zero matches |
| G-7 | Spawning blocks the parent turn | `SubagentPool.run()` at `pool.py:1217` awaits all workers; `PoolContinuationRegistry` (`communication.py:727-760`) only collects finished pools |
| G-8 | Worker lifecycle controls are disjoint | `stop_worker` `pool.py:1188`, `dismiss_worker` `pool.py:1204`, `send_subagent_message` `communication.py:909`; no unified per-worker steer/cancel/retry/salvage lifecycle |
| G-9 | No published transcript/event schema shared by kernel events and durable transcripts | `TranscriptEvent` `transcripts.py:63-107`; pool emit methods `pool.py:1480-1566`; no contract document |

G-6's proof is worth restating plainly: the negative grep covers the whole
`subagents/` package, which is the entire implementation of pool budgets. A
budget capability therefore cannot exist elsewhere in a way that would reach
these workers.

### 1.10 Prior art to reconcile

| Document | Status | Reconciliation stance |
|---|---|---|
| [PRD-124](prd-124-concurrent-typed-subagents.md) | Architecture spec, no YAML frontmatter, plus an amendment at lines 6-10 | Still the definition of the concurrent-pool architecture. Its amendment *removed* typed result schemas on the grounds that LLM output cannot be relied on to satisfy a Pydantic schema. G-3 must qualify that amendment explicitly rather than silently reverse it. |
| [PRD-180](prd-180-subagent-artifact-writing-and-research-handoffs.md) | `Proposed` 1.0.0 | Artifact writing and research handoffs. Overlaps the result-contract area; the new PRD must state which parts it inherits. |
| [PRD-197](prd-197-subagent-runtime-policy-inheritance.md) | `Implemented` 1.1.0 | The current, shipped policy-inheritance contract. New permission work must *narrow* the inherited policy and cite PRD-197 instead of restating it. |
| [PRD-214](prd-214-reliable-subagent-findings-handoff.md) | `Proposed` 1.0.0 | File-backed Markdown findings handoff. Complementary to a structured output contract, not a substitute; the two must be reconciled in one place. |
| [AGX-08](agx-08-subagent-panel-and-navigation.md) | `Implemented (phase 1)` 0.1.0 | The shipped panel and child-session navigation. An observability contract must formalise what AGX-08 already renders, not redesign it. |
| [PRD-232](prd-232-durable-agent-team-coordination.md) | `Proposed` 0.1.0 | Durable agent-team task graph and communication. The broadest overlapping proposal; per-worker lifecycle and durable pool identity must not fork a second coordination model. |
| [PRD-233](prd-233-declarative-agent-profiles-and-policy.md) | `Proposed` 0.1.0 | Declarative agent profiles and per-agent policy. Directly overlaps G-1 and G-5; a declarative-types PRD must state supersession or explicit delegation. |

Two further documents are dependencies rather than overlaps and are cited where
relevant instead of being reconciled here: [PRD-141](prd-141-background-sessions-and-session-manager-tui.md)
(background sessions) and [PRD-204](prd-204-goal-runner-and-detached-orchestration.md)
(detached orchestration), both relevant to G-7.

### 1.11 What this baseline does and does not establish

It establishes, with reproducible citations, the shape of the current type
contract, the execution path, what is inherited from the parent, what results
look like, which observability surfaces exist, and nine gaps with absence
proofs.

It does not establish that any of those gaps is *worth closing*, or in what
order. That judgement depends on what other harnesses do, which is §2 onward,
and it does not establish that the absence proofs are complete outside the
`subagents/` package. A capability that exists in, say, the workflow runner and
merely fails to reach workers would not be caught by a `subagents/`-scoped
grep, so each PRD must re-run the search at the layer it proposes to change.

---

## 2. Harness findings

### 2.1 Evidence depth, stated before the claims

The six harnesses are not equally knowable. Two are closed source and can only
be described from vendor documentation; one of the "Grok CLI" candidates is a
community project rather than the vendor's own product; one open-source
project's subagent design is documented mainly in archived architecture notes.
Saying so up front is the point of this table — the alternative is a comparison
that looks uniform and is not.

| Harness | Openness | Evidence used | Pinned ref | Depth | Main caveat |
|---|---|---|---|---|---|
| opencode | open source | cloned repo, read code + tests | `726cf096396d5014c3b1845e231cc01efe0421ee` | **code-level** | TypeScript monorepo; read `packages/` only |
| Codex CLI | open source | cloned repo, read code + test suite | `9cc14e79882ff8885cd5d33b4adadae014045aef` | **code-level** | Rust workspace; multi-agent surface is feature-flagged and partly V1/V2 divergent |
| DeepSeek Harness | open source | cloned repo, read packages + archived architecture notes | `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` | **code + design notes** | Key contracts are documented in `.agents/notes/archived/…` and describe intent; I did not execute the harness |
| Grok CLI | open source, **community** | cloned repo, read code | `fb97af83f06dca873281d60168430f06c8de6324` | **code-level** | This is `superagent-ai/grok-cli`, a third-party agent for the Grok API — **not** an xAI product. Treat as one data point, not vendor policy |
| Claude Code | closed source | vendor docs | `code.claude.com/docs/en/sub-agents`, fetched 2026-10-29 | **docs-only, high detail** | No source to read; several behaviours are version-gated (v2.1.198–v2.1.257) and may drift |
| Qoder CLI | closed source | vendor docs | `docs.qoder.com/cli/subagent`, fetched 2026-10-29 | **docs-only** | No source; no version markers in the page, so behaviour is as-documented, not as-verified |

Two consequences for the PRDs that follow. First, a capability observed in only
one closed-source harness is weaker evidence than the same capability observed
in two open-source ones, and the PRDs should weight it accordingly. Second, the
absence of a capability in a docs-only harness is genuinely weak evidence — the
docs may simply not mention it — whereas absence in a cloned repo is a bounded,
reproducible claim about that commit.

### 2.2 Grok CLI — `superagent-ai/grok-cli`

A small, readable TypeScript agent. Subagent customisation is deliberately
minimal: a user-defined subagent is exactly three fields.

- **Type definition.** `CustomSubagentConfig` is `{ name: string; model: string;
  instruction: string }` (`src/utils/settings.ts:109-112`). There is no tool
  list, no turn limit, and no permission field — the type is a name, a model,
  and a prompt.
- **Validation at load.** `isReservedSubagentName` blocks collisions with
  built-ins (`src/utils/settings.ts:125-126`); `parseSubAgentsRawList` requires
  a non-empty name, a model present in the known-model set, and dedupes by
  lowercased name (`:140-152`), so a malformed entry is dropped rather than
  failing the session.
- **Per-type model routing.** This is the clearest small-scale precedent: the
  model is *part of the type declaration*, and the editor exposes it as one of
  three fields (`SUBAGENT_EDITOR_FIELDS = ["name", "model", "instruction"]`,
  `src/ui/agents-modal.tsx:11`), displayed per row at `:116`.
- **Two spawn tools, split by synchronicity.** `task` delegates a *foreground*
  task and enumerates the selectable types inline, appending user-defined names
  to the built-in enum (`src/grok/tools.ts:350-370`). `delegate` launches a
  *read-only background* agent — "Launch a read-only background agent that can
  keep researching while you continue working… You will be notified when it
  completes" (`src/grok/tools.ts:593-595`).
- **Tool scoping.** Built-in types are distinguished by prompt and purpose
  (`general`, `explore`, `vision`, `verify`, `verify-detect`, `verify-manifest`,
  `computer`), but I found no per-type tool allowlist equivalent to agenthicc's
  `allowed_tools`; the `src/tools/` directory holds `bash`, `computer`, `file`,
  `grep`, `schedule` as a shared vocabulary.
- **Depth / fan-out.** No depth limit or concurrent-spawn cap was found in the
  cloned tree (`grep -rniE "subagent|depth|concurren|limit" src/` surfaced only
  unrelated `MAX_FILE_SIZE` and poll-timeout constants). I record this as "not
  located", not "absent by design" — it is a small codebase and the negative
  result is plausible but not authoritative.

**What is worth copying:** the three-field type declaration with the model
inside it, and the foreground/background split as two *separate tools* rather
than one tool with a boolean. The latter is a real design choice with a
consequence: the model cannot accidentally background work that needs its
result.

**What is not worth copying:** no tool scoping per type, so `explore` and
`general` differ only by prompt — which is exactly the weakness agenthicc
already has with description-free types.

### 2.3 Claude Code

Closed source, but the vendor maintains an unusually detailed subagent page, and
it is the most complete public description of subagent semantics of the six.

- **Definition.** Markdown files with YAML frontmatter. Scopes: user
  (`~/.claude/agents/`), project (`.claude/agents/`, discovered by walking up
  from the working directory, nearest definition wins), plugin, managed
  (organisation), and CLI `--agents` JSON for ephemeral definitions. Identity
  comes only from the `name` field, not the filename or subdirectory.
- **Frontmatter surface.** `description, tools, disallowedTools, model,
  permissionMode, mcpServers, hooks, maxTurns, autoCompactWindow, skills,
  initialPrompt, memory, effort, background, omitClaudeMd, isolation`.
  Plugin-provided subagents deliberately ignore `hooks`, `mcpServers`, and
  `permissionMode` for security.
- **Description is a first-class budget.** Claude selects a subagent *by its
  description*, and when the combined non-built-in descriptions exceed **15,000
  tokens** the CLI warns at startup with the total. This is the single most
  directly relevant finding for agenthicc's G-1: descriptions are the selection
  interface, and they are metered.
- **Per-type model routing with a documented precedence chain.** Resolution
  order: (1) per-invocation `model` parameter on the Agent tool call,
  (2) `model` frontmatter, where `inherit` selects the parent model,
  (3) the `CLAUDE_CODE_SUBAGENT_MODEL` environment variable, (4) the main
  conversation's model. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` collapses the chain
  onto one model. Organisation `availableModels` allowlists substitute a
  permitted model rather than failing. Extended thinking is **inherited** and
  there is no per-subagent thinking setting — an explicit refusal to make every
  dimension per-agent.
- **Nesting is on by default, bounded, and synchronously waited on.** A subagent
  may spawn subagents up to **three layers** below the main conversation;
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` changes it and `1` disables nesting. At
  the limit the CLI *withholds the Agent tool* from every subagent except a
  fork, whose call returns an error instead of spawning. A spawning subagent
  waits for each nested subagent's report, so nesting does not multiply
  concurrency behind the parent's back.
- **Fan-out is capped separately from depth.** Two independent limits: the depth
  limit above, and a concurrency limit of **20** running subagents
  (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`). Hitting it fails the spawn with
  "Concurrent subagent limit reached" and instructs the model *not to retry*.
  There is explicitly **no** cap on how many subagents a session spawns in
  total.
- **Spawn restriction is an allowlist in the tool list.** `tools: Agent(worker,
  researcher), Read, Bash` permits spawning only those two types; omitting
  `Agent` forbids spawning entirely; `permissions.deny` blocks specific types
  while allowing the rest. Notably, inside a *subagent* definition the type list
  inside `Agent(...)` is **ignored** — nesting is all-or-nothing there.
- **Background subagents have different semantics, not just different
  scheduling.** They run with a *smaller* built-in tool set, surface permission
  prompts in the main session (with session-wide consequences for grants), and
  deliver results as a completion notification in a later turn.
- **Partial work survives failure.** A foreground subagent cut off by a rate
  limit or server error returns its partial output with a note that it was cut
  off; a background one is marked failed but the message names the error *and
  includes the subagent's last output, so partial work isn't lost*.
- **Observability is a tree.** Nested subagents appear as a tree in the panel
  below the prompt input, each row marked with a `(+N)` count of descendants;
  opening a row shows siblings and direct children with a path back to main.
  A `/tasks` list retains finished rows briefly (30 seconds) and subagents can
  be resumed.

### 2.4 Codex CLI — `openai/codex`

A Rust workspace whose multi-agent feature is flag- and version-gated, which
makes it the best public evidence for *tool-surface* design rather than policy
design.

- **Six dedicated tools, not one.** `spawn_agent`, `send_message`,
  `followup_task`, `wait_agent`, `interrupt_agent`, `list_agents`
  (`codex-rs/core/tests/suite/multi_agent_tool_descriptions.rs:18-25`). The
  split matters: *continuing* an agent (`send_message`), *queueing more work*
  (`followup_task`), *blocking on it* (`wait_agent`), *stopping it*
  (`interrupt_agent`), and *enumerating it* (`list_agents`) are distinct
  model-facing operations rather than flags on a spawn call.
- **Hierarchical agent identity.** `AgentPath` is a validated absolute path with
  `ROOT = "/root"` and a reserved `MORPHEUS = "/morpheus"`
  (`codex-rs/protocol/src/agent_path.rs:19-23`), parsed from and into a string.
  Agent identity is therefore a *tree position*, which is what makes
  `list_agents(path_prefix)` meaningful.
- **Versioned protocol.** `MultiAgentVersion { Disabled, V1, V2 }`
  (`codex-rs/protocol/src/protocol.rs:3321`) travels with turn and model
  metadata, so the tool surface and prompt sections can change per version.
- **Concurrency is a config integer.** `max_concurrency:
  config.max_concurrent_threads_per_session`
  (`codex-rs/core/src/session/multi_agents.rs:59`). The TUI carries a snapshot
  for an *"ultra reasoning selection high multi-agent concurrency warning"*,
  i.e. a deliberate interaction between reasoning effort and fan-out.
- **Model override is an exposed capability, not an assumption.**
  `expose_model_overrides: config.expose_spawn_agent_model_overrides`
  (`multi_agents.rs:61`); under V2, `spawn_agent` is registered only when
  `multi_agent_v2.expose_spawn_agent_model_overrides` is true — the V1 arm
  registers it unconditionally
  (`codex-rs/core/src/session/world_state.rs:389-403`). When overrides are
  available *and* the `ModelCatalogInContext` feature is on, a model-catalog
  section is injected into context (`…/context/world_state/model_catalog.rs`),
  and it can be invalidated with the note "The previous spawn_agent model
  catalog no longer applies." So the model list the agent may choose from is
  itself context that goes stale.
- **Blocking is an explicit, disableable tool.** `wait_agent` exists and
  `multi_agent_v2.wait_agent_enabled` defaults to `true`
  (`codex-rs/core/src/config/mod.rs:1404`, `:1428`). A model waits only by
  calling a tool, not by the harness blocking its turn.
- **Root handoff.** `spawn_agent`, `send_message` and `followup_task` are
  grouped as handoff tools with a `target` argument
  (`codex-rs/core/src/agent/control/root_handoff.rs:17`, `:88`), and a control
  API exposes `interrupt_agent` (`codex-rs/core/src/agent/control.rs:315`) and
  `list_agents` (`:366`) with a path prefix.
- **Metadata can be hidden.** `multi_agent_v2.hide_spawn_agent_metadata` exists
  (`config/mod.rs` test fixtures), i.e. the harness distinguishes what the model
  sees from what the operator sees.

### 2.5 opencode — `sst/opencode`

The richest declarative model of the six: an agent is a typed record with a
permission ruleset, and the subagent tool is a first-class plugin.

- **Agent as a declarative record.** `Agent.Info`
  (`packages/schema/src/agent.ts:23-35`) is `{ id, name, model?: Model.Ref,
  request: Provider.Request, system?: string, description?: string, mode:
  "subagent" | "primary" | "all", hidden: boolean, color?, steps?:
  PositiveInt, permissions: Permission.Ruleset }`.
  - `mode` answers "may this agent be spawned?" declaratively.
  - `steps` is a per-agent turn budget.
  - `model` is per-agent.
  - `permissions` is a **ruleset**, not a flat allowlist: the default agent
    gets `allow *` with `ask` on `*.env` and `*.env.*`
    (`packages/schema/src/agent.ts:47-49`), i.e. action × resource × effect
    rules with a deny-by-default fallback for agents that declare nothing
    (`packages/core/src/permission.ts:19`, applied at `:151`).
- **The `subagent` tool is a plugin with a typed contract.**
  Input `{ agent, description, prompt, model?, sessionID?, background? }`,
  output `{ sessionID, status: "completed" | "running", output }`
  (`packages/core/src/tool/plugin/subagent.ts:30-54`).
- **Depth is a config value, counted by walking the session chain.** The tool
  walks parent sessions to compute `depth` (`:124-134`), reads
  `experimental.subagent_depth` **defaulting to 1** (`:135`), and fails with
  "Subagent depth limit reached (N). Increase \"experimental.subagent_depth\" to
  allow nested subagents." (`:136-139`). This is the closest structural analogue
  to agenthicc's situation: depth 1 by default, nesting as an opt-in integer.
- **Per-call model override with real validation.** The `model` parameter takes
  `"providerID/modelID"` or `"providerID/modelID#variant"`, is resolved against
  available models, and is rejected with a specific message for non-chat
  (image/decision) models or an unknown variant
  (`packages/core/src/tool/plugin/subagent.ts:75-104`). The tool description
  tells the model *not* to set it unless the user asked.
- **Background is a first-class mode with anti-polling instructions.** With
  `background: true` the tool returns immediately with `status: "running"` and a
  message telling the model it "will be notified automatically when it
  finishes", plus "DO NOT sleep, poll for progress, ask the subagent for status,
  or duplicate this subagent's work" (`:19-28`).
- **Continuation by session id.** Passing `sessionID` sends a follow-up to an
  already running *or finished* subagent, so steering and resumption are the
  same mechanism (`:41-43`).
- **Lifetime is tested against eviction.** `script/drive/subagent-eviction.md`
  documents a drive test that starts a background subagent, moves the child to
  another directory, evicts and rebuilds the parent location, then lets the
  child finish — with real session movement, job scopes, completion delivery,
  SQLite persistence and parent wake, and a 60-minute inactivity TTL shortcut
  via a debug endpoint. Dedicated tests exist for lifetime and configuration
  (`packages/core/test/subagent-lifetime.test.ts`,
  `packages/core/test/tool-subagent.test.ts`,
  `packages/core/test/config/command-subagent.test.ts`).
- **Observability is a projection.** `packages/tui/src/mini/stream-v2.subagent.ts`
  and `packages/session-ui/src/message/agent-label.ts` render subagents from
  session events; there is no separate subagent event vocabulary.

### 2.6 DeepSeek Harness — `deepseek-ai/deepseek-harness`

Motto: "Everything is a Plugin." The relevant design is not a subagent feature
but an *agent composition* architecture, and the most transferable artifact is
its tool-output contract.

- **The agent is a registration scope.** Every live agent owns one flat
  registration layer (`agent.ctx`); scope-aware services combine
  deployment-global registrations with exactly one agent layer
  (`.agents/notes/archived/architecture/2026-07-08-agent-scope-contexts.md`).
  Tools, prompt sections, policies and listeners are contributions scoped to an
  agent rather than fields on a type record.
- **Presets are declarative composition.** A preset is a directory holding one
  `agent.cordis.yml`, mounted as a subtree into that agent's scope
  (`…/2026-08-03-per-session-agent-presets.md`). The note splits composition into
  two planes — a process-wide *host* plane (registries, persistence, subagent
  providers) and a per-session *agent* plane (tool plugins, persona, prompt
  sections, compaction policy) — and states the measured cost of per-session
  isolation: ~3 ms and ~600 KB per session for a twelve-row composition.
- **Model routing is an explicit per-agent seam, deliberately kept out of
  presets.** Quoting the note: "Model routing stays out of presets.
  `installAgentLlmTarget` is already the per-agent seam for provider, model, and
  reasoning effort." This is a clean separation worth citing: *composition* and
  *model selection* are different concerns with different owners.
- **Subagent identity and lifecycle are owned by a stable service.** The
  Agent Teams note records that "the generic caller-reserved continuable child
  identity and selective direct-child drain remain in the stable Subagent
  service", with the experimental Team service consuming them in one permitted
  direction (`…/2026-08-18-experimental-agent-teams-packages.md`). Two ideas
  worth stealing: *caller-reserved* identity (the parent names the child) and
  *direct-child drain* (teardown targets a subtree, not the world).
- **Canonical tool output contract — the strongest answer to plain-text
  results.** Every tool declares a mandatory canonical output and returns only
  the value it describes (`…/2026-07-20-canonical-tool-output-contract.md`):

  ```text
  output: {
    schema: OutputSchema
    render(args, value): ContentBlock[]
    presentationMeta?(args, value): JsonValue
  }
  ```

  The registry snapshots the returned value as lossless JSON, validates it
  against `output.schema`, deep-freezes it, then invokes a *pure renderer* and
  an optional metadata projector; renderer, projector, schema or JSON failures
  are contained as `ToolOutputError` results, and a cached result returned from
  another call is re-normalised under the active declaration. The decisive
  property for agenthicc: **the schema is declared by the tool, not by the
  model, and rendering is separated from the value.**
- **Layout.** `packages/core/{agent, agent-loop, agent-default-model,
  agent-tool-presentation}`, `packages/preset/{agent-preset,
  agent-preset-registry}`, and experimental `subagent-oar`,
  `tool-subagent-oar`, `agent-team`, `tool-agent-team`,
  `agent-team-profile`, `client-ui-agent-team`.

Caveat: these are archived design notes. Their status fields say
"implemented", but they document intent at a point in time; I did not run the
harness to confirm behaviour.

### 2.7 Qoder CLI

Closed source; the CLI documents a subagent model that is structurally very
close to Claude Code's, with a more explicit *source precedence*.

- **Definition.** Markdown + YAML frontmatter; the body is the system prompt;
  the name comes from the frontmatter `name` field, not the filename.
- **Five sources with a documented precedence ladder** (low → high): built-in →
  user `~/.qoder/agents/*.md` → project `.qoder/agents/*.md` → plugin →
  `--agents` JSON flags. The highest-priority source wins name conflicts, and
  the `/agents` panel marks shadowed definitions as such. This is the most
  explicit statement of definition precedence among the six.
- **Configuration fields.** `--agents` JSON supports `description, prompt,
  tools, disallowedTools, mcpServers, model, effort, color, maxTurns,
  initialPrompt, skills, permissionMode`; the Markdown form additionally
  supports `timeoutMins, temperature, hooks, memory, background, isolation`.
- **Model routing.** `model: inherit` is available; `effort` is a separate
  field, so reasoning effort is per-subagent rather than only inherited.
- **Nesting via a tool expression.** `Agent(name)` inside `tools` permits calling
  only the named subagents — the same shape as Claude Code's `Agent(...)`
  allowlist.
- **Budget.** `maxTurns` per subagent, with `--max-turns` bounding the whole
  headless query — i.e. per-child and per-run turn budgets are distinct.
- **Isolation.** A subagent can be run in a separate worktree via frontmatter.
- **Orchestration is natural language.** Users are told to describe execution
  order and to "explicitly ask for parallel dispatch" for independent tasks;
  there is no dedicated fan-out primitive documented.
- **Built-ins.** `general-purpose` (the default when a dispatch omits the type)
  and `Explore`, which "inherits available tools, removes write and control
  tools, and uses exploration-oriented model settings".
- **Session-agent mode.** `--agent` promotes a subagent definition to the
  session's main agent, using its `initialPrompt`.

---

## 3. Cross-harness feature matrix

Legend: **●** documented and detailed · **◐** present but partial/flag-gated ·
**○** absent or not located · **–** not applicable to that harness's design.
"Not located" (`○`) in a docs-only harness is weak evidence; see §2.1.

| Dimension | Grok CLI | Claude Code | Codex CLI | opencode | DeepSeek Harness | Qoder CLI |
|---|---|---|---|---|---|---|
| How a type is defined | 3-field settings record | Markdown + YAML frontmatter | Rust role config + model catalog | typed `Agent.Info` record | `agent.cordis.yml` preset subtree | Markdown + YAML frontmatter |
| Model-facing description drives selection | ○ (name only) | ● metered, 15k-token warning | ◐ catalog + role metadata | ● `description?` on `Agent.Info` | ◐ persona/prompt sections | ● matched against description |
| Per-type **model** | ● `model` field | ● `model` + `inherit` | ◐ flag-gated per-spawn override | ● `model: Model.Ref` | ● `installAgentLlmTarget` seam | ● `model` + `inherit` |
| Model resolution precedence documented | ○ | ● 4-step chain + force var | ◐ overrides + catalog invalidation | ◐ per-call override validated | ● per-agent seam, kept out of presets | ◐ `inherit` only |
| Per-type **reasoning effort** | ○ | ◐ inherited, no per-subagent setting | ● reasoning-effort modes | ○ (variant on model ref) | ● part of `installAgentLlmTarget` | ● `effort` field |
| Per-type **tool scoping** | ○ (prompt-only distinction) | ● `tools` + `disallowedTools` | ◐ role/native-tool hiding | ● `permissions` ruleset | ● plugin composition per agent | ● `tools` + `disallowedTools` |
| Permission granularity | ○ | ● `permissionMode` + deny rules | ◐ sandbox/approval config | ● action×resource×effect ruleset | ● per-agent policy contributions | ● `permissionMode` |
| Turn budget | ○ | ● `maxTurns` | ◐ | ● `steps` | ◐ | ● `maxTurns` (+ run-level) |
| Nesting depth control | ○ | ● 3 layers default, env-configurable | ● `AgentPath` tree | ● `experimental.subagent_depth` default 1 | ● caller-reserved child identity | ◐ `Agent(name)` expression |
| Explicit depth-limit *mechanism* | – | ● tool withheld at limit | ◐ path prefix scoping | ● error with remedy text | ◐ direct-child drain | ○ |
| Fan-out concurrency cap | ○ | ● 20 default, env-configurable | ● `max_concurrent_threads_per_session` | ○ (no cap located) | ◐ | ○ |
| Total-spawn cap over a session | – | ● explicitly none | ○ | ○ | ○ | ○ |
| Async / background spawn | ● `delegate` (read-only) | ● `background` + notifications | ● `spawn_agent` + `wait_agent` | ● `background: true` | ◐ continuable child + drain | ● `background` field |
| Steering a running worker | ○ | ● message/resume by name | ● `send_message`, `followup_task` | ● `sessionID` follow-up | ● continuable child identity | ◐ |
| Cancellation | ○ | ● stop, row retained 30 s | ● `interrupt_agent` | ● job/session teardown | ● Activation teardown | ◐ |
| Enumerate workers | ○ | ● `/tasks`, panel tree | ● `list_agents(path_prefix)` | ● session projections | ● | ● `/agents` panel |
| Result contract | ◐ prose return | ● summary + partial-output rules | ◐ protocol items | ● typed `Output` struct | ● **canonical output schema + render** | ◐ prose return |
| Partial-output salvage on error | ○ | ● foreground note / background last output | ◐ | ◐ | ● `ToolOutputError` containment | ◐ |
| Durable/observable transcript | ○ | ● panel tree, `/tasks` | ● protocol events + app-server | ● SQLite + session events + drive test | ● jobs/activity records | ● panel grouped by source |
| Definition precedence ladder | ○ | ● user/project/plugin/managed/flag | ○ | ◐ config layers | ● preset include order | ● 5 explicit tiers + shadowing |

### What the matrix actually shows

Three clusters, not a single ranking:

1. **Policy-rich, docs-transparent**: Claude Code and Qoder CLI. Both push
   nearly every dimension into a declarative file format with an explicit
   precedence ladder. They differ in that Claude Code documents *mechanics*
   (depth-limit enforcement, model resolution order, partial-output rules) while
   Qoder documents *fields*.
2. **Mechanism-rich, code-first**: Codex CLI and opencode. Both express agent
   identity structurally — a path tree in Codex, a typed record with a
   permission ruleset in opencode — and both make the subagent surface a set of
   first-class operations rather than flags.
3. **Composition-first**: DeepSeek Harness. It barely has a "subagent feature"
   in the others' sense; instead everything an agent is, including its tools and
   model target, is a scoped registration, and the transferable artifact is the
   canonical tool-output contract.

Grok CLI is the outlier: it has the smallest surface, and yet it is the only one
where the *entire* type declaration is a name, a model, and a prompt — which
makes it the cleanest evidence that per-type model routing is cheap to add and
is independently valuable.

Nobody in this set leaves results as unstructured prose *and* documents it as a
deliberate choice. That is the single most consequential difference from
agenthicc, whose `SubagentResult.text` is plain text by explicit amendment.

---

## 4. Gap analysis: competitor capability → agenthicc evidence

Each row maps an observed capability to the agenthicc gap from §1.9 and to the
source line that justifies the gap. The "strength" column is my assessment of
how much weight the external evidence can bear, given §2.1.

| Gap | Competitor evidence | agenthicc evidence | Distance | Strength |
|---|---|---|---|---|
| **G-1** types are code-only, description-free | opencode `description?` + `mode` on `Agent.Info` (`packages/schema/src/agent.ts:23-35`); Claude Code selects *by* description and meters 15k tokens; Qoder 5-tier file ladder; Grok 3-field settings record | `SubagentTypeSpec` has 5 fields, no `description` (`types.py:60-91`); enum merged as bare names (`tool.py:559-563`) | Large: agenthicc has plugin *registration* but no declarative *definition* or selection text | **Strong** — four of six harnesses agree, two of them open source |
| **G-2** no per-type model/effort/context routing | Claude Code 4-step model chain + `inherit`; opencode `model` on the record + validated per-call override; Grok `model` in the type; Qoder `model`/`effort`; DeepSeek `installAgentLlmTarget`; Codex flag-gated overrides | worker pinned to `parent_model` (`pool.py:849`, `:940`); memory fixed at 8,000 (`pool.py:958`, `:960`) | Large: no model, effort or context dimension exists per type | **Strongest** — all six have something; unanimous |
| **G-3** plain-text results, no output contract | DeepSeek canonical output schema + render + `ToolOutputError`; opencode typed `Output`; Claude Code partial-output rules | `SubagentResult.text` (`pool.py:325`); `aggregate() -> str` (`types.py:51`); PRD-124 amendment | Medium: agenthicc *chose* plain text, so this is a reversal, not a gap | **Strong** but needs careful argument — see note below |
| **G-4** no nesting, no depth/fan-out governance | Claude Code depth 3 + tool withheld at limit + separate 20-concurrency cap; opencode `experimental.subagent_depth` default 1; Codex `AgentPath` + `max_concurrent_threads_per_session`; Qoder `Agent(name)` | `spawn_subagents` in no `allowed_tools` set (`types.py:182-306`); semaphore only (`pool.py:1309`) | Medium: agenthicc is effectively depth-0-with-flat-fan-out | **Strong** on mechanism; **weak** on whether agenthicc should nest at all |
| **G-5** permissions are one flat ceiling | opencode action×resource×effect ruleset incl. `ask` on `*.env`; Claude Code `permissionMode` + `disallowedTools` + `permissions.deny`; Qoder same shape | `allowed_tools` ceiling only (`types.py:88`, `:83-84`); PRD-197 inherits parent policy | Medium: inheritance exists, *narrowing* does not | **Strong** — three harnesses have richer rules, one open source in detail |
| **G-6** no budget for spend or aggregate time | Claude Code 20-concurrency cap and 15k description budget; Codex `max_concurrency`; Qoder `maxTurns` + run-level `--max-turns`; Grok none | zero matches for budget/cost/token_limit in `subagents/` | Medium-large: agenthicc meters usage but does not *limit* it | **Moderate** — concurrency caps are common, **cost** caps are not universal; do not overclaim |
| **G-7** spawn blocks the parent turn | opencode `background: true` + notification + anti-poll instructions; Claude Code `background` + completion notification; Grok `delegate`; Codex `wait_agent` as an explicit tool | `SubagentPool.run()` awaits all (`pool.py:1217`); continuation registry only collects finished pools | Large: the parent turn cannot proceed | **Strong** — four of six, three open source |
| **G-8** lifecycle controls are disjoint | Codex six split tools (`spawn/send/followup/wait/interrupt/list`); Claude Code stop + resume by name; opencode `sessionID` steering | `stop_worker` (`pool.py:1188`), `dismiss_worker` (`:1204`), `send_subagent_message` (`communication.py:909`) — unconnected | Medium: pieces exist, no unified lifecycle | **Strong** — Codex is a working model of the split |
| **G-9** no published transcript/event schema | DeepSeek canonical output + jobs/activity records; opencode SQLite + session-event projection + eviction drive test; Codex protocol items + app-server | `TranscriptEvent` (`transcripts.py:63`), four emit methods (`pool.py:1480-1566`), AGX-08 panel | Medium: two vocabularies, no contract | **Moderate** — every harness has *an* answer but they are not comparable; mainly an internal-consistency gap |

### Two notes the PRDs must not skip

**On G-3, the PRD-124 amendment was not wrong about the problem.** PRD-124
removed typed result schemas because "LLM output cannot reliably follow a
Pydantic schema". The DeepSeek contract sidesteps that objection rather than
refuting it: the *tool* declares the schema and the runtime validates the
returned value, while a pure renderer produces the model-facing text. The model
never fills a schema. Any structured-result PRD must argue in those terms or it
will be re-litigating a settled decision with the same losing argument.

**On G-4, default-1 is the mainstream position.** opencode ships depth 1; Claude
Code ships depth 3 with the tool withheld at the limit. Both treat nesting as a
bounded opt-in. That supports a PRD which makes depth an explicit, defaulted,
enforced integer — not one that adds unbounded recursive delegation. It does
*not* establish that agenthicc needs nesting at all, which is why the PRD must
also argue the case and offer depth 1 as a valid outcome.

**On G-6, resist the tidy story.** Concurrency caps are near-universal; explicit
token-or-cost budgets are not clearly present in any of the six. Claude Code's
15k *description* budget and Qoder's `maxTurns` are turn/context budgets, not
spend budgets. A budget PRD is therefore the weakest-supported of the nine and
should be sequenced and scoped accordingly.

---

## 5. Locked capability taxonomy

### 5.1 What "locked" means here

An area is **locked** when it passes the two **admission** tests (T1, T3) and
clears the two **calibration** tests (T2, T4). The distinction is deliberate and
it is the reason the set can be honest about a weak area: failing an admission
test means the area does not exist, while failing a calibration test means the
area exists at reduced confidence, narrower scope, later sequencing, or with an
explicit supersession statement — never silently.

| Test | Kind | Question | What a failure admits |
|---|---|---|---|
| **T1 evidence** | admission | Is there a reproducible agenthicc gap with a `file:line`, and for absences a named exhaustive search (§1.9)? | An area invented from competitor reading with no local problem to solve |
| **T2 precedent** | calibration | Does at least one of the six harnesses actually demonstrate the capability (§2, and the §4 Strength column)? | An area with no external justification |
| **T3 distinct owner** | admission | Is the change concentrated in code that no other area owns? | Two PRDs editing the same function, or an area that is really a sub-task of another |
| **T4 prior art** | calibration | Has an existing document already claimed it (PRD-124/148/157/180/197/214/232/233, AGX-08)? | A second coordination or policy model forking the first |

Concretely: T1 or T3 failing means no area and no PRD. T2 failing means the
area keeps its slot but its PRD must state the weakness in its own evidence
appendix and it sorts last (A-6 is the only such area). T4 failing does not
delete an area either — it forces the PRD to open with a supersession-or-
delegation statement, which §6 will make a required section.

Two rules govern the set, and they are what "one PRD per area" means
operationally:

1. **Every locked area maps to at least one `SUBAGENT-NN` PRD.** An area with no
   PRD is a note.
2. **No PRD exists without a locked area.** A PRD that cannot name its area is
   either an addition that this table owes the reader, or scope creep.

Locked IDs are `A-1` … `A-9` and are stable. PRDs cite them; if a PRD and this
table disagree about what an area owns, this table wins.

### 5.2 The nine areas as first proposed, and their disposition

| Proposed | Area as proposed | Gap | Disposition |
|---|---|---|---|
| P-1 | Declarative types and discovery | G-1 | **A-1**, scope widened (M-2) |
| P-2 | Per-type model / effort / context routing | G-2 | **A-2**, as proposed |
| P-3 | Structured result contracts | G-3 | **A-3**, as proposed; mechanism borrowed (M-4) |
| P-4 | Nesting depth and fan-out governance | G-4 | **A-4**, as proposed |
| P-5 | Per-type permission narrowing | G-5 | **A-5**, as proposed; one open question carried (R-3) |
| P-6 | Pool-level budgets and early abort | G-6 | **A-6**, scope narrowed: abort mechanism transferred to A-8 (M-1) |
| P-7 | Non-blocking / background orchestration | G-7 | **A-7**, as proposed |
| P-8 | Worker steering, cancellation, retry, partial-result salvage | G-8 | **A-8**, scope widened to own the abort mechanism (M-1) |
| P-9 | Observability and transcript contract | G-9 | **A-9**, as proposed |

The proposal is sound: each of the nine named areas corresponds to exactly one
of the nine evidenced gaps, and no two areas name the same owner of change. The
work below is therefore not a rewrite but a boundary exercise, because the
failure mode for nine overlapping areas is not "too many areas", it is *two
PRDs quietly implementing the same decision*. Each area therefore carries an
explicit **does not own** list, not just an **owns** list.

### 5.3 The locked areas

#### A-1 — Declarative type definitions and discovery

- **Gap:** G-1. Five-field `SubagentTypeSpec` with no model-facing description
  (`types.py:60-91`); nine specs hard-coded in `_BUILTIN_SPECS`
  (`types.py:309-371`) behind a process-global `DEFAULT_REGISTRY`
  (`types.py:433-441`); the type enum reaches the model as bare names
  (`tool.py:559-563`).
- **Precedent (T2):** opencode carries `description?` and `mode` on the record;
  Claude Code selects *by* description; Qoder documents a five-tier definition
  ladder; Grok's whole type is a three-field record. Five of six.
- **Owns:** the definition format, where definitions are discovered from, the
  precedence ladder between sources, and the model-facing selection text.
- **Does not own:** what a type may *do* with its tools (A-5), which model it
  runs on (A-2), or the registry's runtime lookup API, which stays as it is.
- **Prior art (T4):** PRD-233 proposes declarative agent profiles and per-agent
  policy for *agents*; A-1 must state supersession or delegation, and must not
  silently become a second profile system.
- **Absorbs (M-2):** definition precedence, duplicate-name detection and
  shadowing visibility. Claude Code ships a `/doctor` check for same-directory
  name collisions and Qoder marks shadowed definitions in its panel; both are
  consequences of having a ladder, so they belong to the area that introduces
  the ladder rather than to a separate "definition tooling" area.

#### A-2 — Per-type model, reasoning-effort and context-budget routing

- **Gap:** G-2. Workers are decorated with `model=self._parent_model`
  (`pool.py:849`), usage is attributed to the parent model (`pool.py:940`), and
  worker memory is fixed at `8_000` tokens (`pool.py:958-960`).
- **Precedent (T2):** all six. Unanimous, and therefore the strongest area.
- **Owns:** per-type model selection, reasoning-effort selection, worker
  memory/context sizing, and the precedence between a type's provider options
  and the inherited `SubagentExecutionPolicy`.
- **Does not own:** discovery of what models exist (that is PRD / model-catalog
  territory), nor spend limits on the resulting usage (A-6). Context *sizing* is
  here; aggregate *cost* is A-6. The split is deliberate: they read the same
  ledger but one is a per-worker setting and the other is a per-pool policy.
- **Prior art (T4):** none. This is the only area with no overlapping document,
  which is itself evidence that it has been overlooked rather than settled.

#### A-3 — Structured result contracts and schema-validated findings

- **Gap:** G-3. `SubagentResult.text` is plain text (`pool.py:325`), results are
  concatenated as labelled text (`pool.py:360`), and `aggregate()` returns
  `str` (`types.py:51`).
- **Precedent (T2):** DeepSeek's canonical tool-output contract; opencode's
  typed `Output` struct; Claude Code's partial-output rules.
- **Owns:** the optional per-type output contract, its validation and failure
  semantics, and the reconciliation of that contract with PRD-214's file-backed
  Markdown handoff.
- **Does not own:** what happens to a worker's partial output when it is
  *stopped or fails* at runtime (A-8). A-3 defines the shape of a result; A-8
  defines what is salvageable when there is no complete result. This is the
  boundary most likely to be violated, because "partial results" sounds like one
  topic. It is not: one is a schema question, the other a lifecycle question.
- **Prior art (T4):** the PRD-124 amendment at
  `prds/prd-124-concurrent-typed-subagents.md:6-10` removed typed result schemas
  because LLM output cannot reliably satisfy a Pydantic schema. A-3 must argue
  in the terms of §4's G-3 note — the *tool* declares the schema and the runtime
  validates the returned value, so the model never fills a schema — or it will
  lose the same argument twice. PRD-180 also touches result handoff.

#### A-4 — Nesting depth and fan-out governance

- **Gap:** G-4. `spawn_subagents` appears in `types.py` only inside docstrings
  (`:29`, `:67`, `:82`) and in none of the nine `allowed_tools` frozensets
  (`types.py:182-306`); the only bound anywhere is a per-call semaphore at
  `pool.py:1309` with `max_concurrent: int = 4` (`pool.py:1120`).
- **Precedent (T2):** opencode `experimental.subagent_depth` defaulting to 1
  with a named remedy in the error; Claude Code three layers with the `Agent`
  tool *withheld* at the limit and a separate 20-concurrency cap; Codex
  `AgentPath` plus `max_concurrent_threads_per_session`; Qoder's
  `Agent(name)` expression.
- **Owns:** `max_depth`, per-depth fan-out and pool-count limits, tiered
  tool/model escalation by depth, and cycle/runaway prevention.
- **Does not own:** the concurrency semaphore itself, which works and stays.
  A-4 adds *policy above* it; it does not replace it.
- **Prior art (T4):** PRD-232's task graph is the broadest overlapping proposal;
  A-4 must not fork a second notion of a worker tree.
- **Note the honest possibility:** §4 records that the evidence supports
  *governing* depth, not *having* nesting. A-4's PRD is therefore allowed to
  conclude "depth 1 stays the default and nesting is declined", and must say so
  explicitly rather than leaving the reader to infer it.

#### A-5 — Per-type permission narrowing

- **Gap:** G-5. Type scoping is one `allowed_tools` ceiling (`types.py:88`) with
  ceiling semantics stated at `types.py:83-84`; there is no deny-list, approval
  override or per-type workspace narrowing anywhere in `policy.py` or
  `types.py`.
- **Precedent (T2):** opencode's action×resource×effect ruleset including `ask`
  on `*.env`; Claude Code `permissionMode` plus `disallowedTools` plus
  `permissions.deny`; Qoder the same shape.
- **Owns:** per-type deny-lists, approval overrides and workspace-scope
  restrictions, under one hard invariant: **a type may narrow the inherited
  policy and may never widen it.**
- **Does not own:** the inheritance contract itself. PRD-197 is `Implemented`
  and is the base; A-5 cites it and adds the narrowing delta.
- **Prior art (T4):** PRD-197 (implemented), PRD-233 (proposed profiles).
- **Carries (R-3):** per-subagent worktree isolation is raised as an open
  question here rather than as its own area; see §5.5.

#### A-6 — Pool-level budgets and limits

- **Gap:** G-6. `grep -rniE "budget|max_cost|cost_limit|token_limit|abort_threshold"
  src/agenthicc/subagents/` returns zero matches.
- **Precedent (T2):** **weak, and recorded as weak.** Concurrency caps are
  near-universal but explicit token-or-cost budgets are not clearly present in
  any of the six; Claude Code's 15k description budget and Qoder's `maxTurns`
  are context and turn budgets, not spend budgets.
- **Owns:** per-pool and per-worker budget *accounting* on top of the existing
  usage ledger, the limit declarations, and the user-visible budget report.
- **Does not own (M-1 in §5.4):** the *act* of aborting. Pre-emptive abort versus
  graceful drain is a lifecycle decision, it operates on the same worker
  handles as steering and cancellation, and it is therefore owned by A-8. A-6
  decides *when a budget is exceeded*; A-8 decides *what happens to the
  workers*. Without this split the two PRDs would each define a cancellation
  path.
- **Prior art (T4):** PRD-157 is `Implemented` and owns canonical usage
  accounting and TUI token display. A-6 consumes that ledger and must not build
  a second meter or a second cost widget.

#### A-7 — Non-blocking and background orchestration

- **Gap:** G-7. `SubagentPool.run()` awaits all workers (`pool.py:1217`) and
  `PoolContinuationRegistry` (`communication.py:727-760`) only collects pools
  that have already finished.
- **Precedent (T2):** opencode `background: true` with completion notification
  and explicit anti-polling instructions; Claude Code background plus
  notification; Grok's separate read-only `delegate` tool; Codex `wait_agent` as
  an explicit, disableable tool. Four of six.
- **Owns:** the fire-and-return mode, durable pool identity, and collection and
  resumption across turns and process restarts.
- **Does not own:** mid-flight steering or cancellation of the workers it
  launches (A-8), nor the session lifecycle it rides on (PRD-141, PRD-204).
- **Prior art (T4):** PRD-141 background sessions and PRD-204 detached
  orchestration are *dependencies*. A-7 extends them and must not duplicate them.

#### A-8 — Worker lifecycle: steering, cancellation, retry, salvage

- **Gap:** G-8. `stop_worker` (`pool.py:1188`), `dismiss_worker`
  (`pool.py:1204`) and `send_subagent_message` (`communication.py:909`) exist as
  disjoint pieces with no unified per-worker lifecycle.
- **Precedent (T2):** Codex splits the whole surface into six tools
  (`spawn_agent`, `send_message`, `followup_task`, `wait_agent`,
  `interrupt_agent`, `list_agents`); Claude Code stops and resumes by name and
  retains the row for 30 s; opencode steers by `sessionID`.
- **Owns:** mid-flight steering, per-worker cancellation with cleanup,
  per-worker retry versus whole-pool retry, salvage of partial output when a
  worker is stopped or fails, and (via M-1) the abort-versus-drain decision that
  A-6 triggers.
- **Does not own:** the shape of a completed result (A-3), nor the termination
  semantics of an in-flight *turn* outside a subagent pool — PRD-148 owns
  unified interrupt and graceful cancellation, and A-8 must extend that
  vocabulary rather than invent a parallel one.
- **Prior art (T4):** PRD-232 and PRD-148.

#### A-9 — Observability and a canonical transcript/event contract

- **Gap:** G-9. `TranscriptEvent` exists (`transcripts.py:63-107`), the pool
  emits through four methods (`pool.py:1480-1566`), and there is no contract
  document tying the two to what the TUI renders.
- **Precedent (T2):** moderate, and recorded as such. DeepSeek has canonical
  output plus jobs/activity records; opencode persists to SQLite and projects
  session events, with an eviction drive test; Codex has protocol items and an
  app-server surface. Every harness has *an* answer but they are not mutually
  comparable.
- **Owns:** one documented event and transcript schema shared by kernel events,
  durable transcripts and the TUI rails, including redaction guarantees,
  bounded payloads, replay, and the counters needed to diagnose stuck or
  over-budget pools.
- **Does not own:** the panel's information architecture or navigation, which
  AGX-08 phase 1 already shipped and which A-9 formalises rather than redesigns.
- **Prior art (T4):** AGX-08 (implemented, phase 1).

### 5.4 Change log against the proposed nine

**Two scope transfers, one borrowed mechanism, zero merges, zero deletions, zero
net additions.** The table is deliberately unexciting, and each null result is
argued rather than asserted.

| ID | Change | From → to | Why | Reflected in |
|---|---|---|---|---|
| **M-1** | Scope transfer | P-6 "early abort" → **A-8** | Abort-versus-drain is the same mechanism as cancellation; the handles, cleanup and partial-output salvage are A-8's. Leaving it in A-6 would give two PRDs a cancellation path. | A-6 narrows to accounting and limit declaration; A-8 widens to own the response |
| **M-2** | Scope transfer | "definition precedence and shadowing" (proposed as part of the Claude/Qoder reading, never a numbered area) → **A-1** | A precedence ladder is a property of discovery; a separate area would own no code that A-1 does not already touch, so it fails T3 | A-1 absorbs it as an acceptance criterion |
| **M-3** | Rejected on T1 | "per-type turn/tool-call budget" | Already implemented: `max_turns` is a `SubagentTypeSpec` field at `types.py:89`. T1 fails, so it cannot be an area or a PRD | Becomes a non-goal inside A-2 and A-6 rather than a tenth area |
| **M-4** | Borrowed mechanism | DeepSeek canonical tool-output contract → **A-3** | It is the mechanism by which a structured result contract can avoid PRD-124's schema objection. A "tool output contract" area would be a different product decision about *all* tools, not about subagents | A-3 cites it as precedent, not as scope |

**Why nothing was merged or deleted.** The two credible merge candidates were
P-9 into P-8 ("runtime visibility and control") and P-6 into P-4 ("governance
and limits"). Both were rejected on T3, not on taste: P-9's owner is
`transcripts.py` plus the event emitters, P-8's is the pool's worker handles;
P-6 consumes the usage ledger and P-4 gates spawning, and neither touches the
other's code. Merging them would produce one PRD with two unrelated acceptance
criteria — which is how a PRD ends up unimplementable in one review.

A-6 is the weakest area on T2 (no competitor clearly has spend budgets) and it
is **not** deleted, because the deletion rule is T1 or T3, not T2. It has a
reproducible local gap (the zero-match grep covers the whole `subagents/`
package) and a unique owner, so it survives at reduced confidence: scoped
narrow by M-1, sequenced last, and with its weakness stated in the PRD's own
evidence appendix rather than discovered by a reviewer.

### 5.5 Rejected candidates, and where each one lands instead

This table is the padding guard. Each row is a real capability observed in §2
that is deliberately **not** an area, with the test it fails.

| ID | Candidate | Seen in | Fails | Lands instead in |
|---|---|---|---|---|
| R-1 | Typed hierarchical agent identity (`AgentPath` tree) | Codex CLI | T3 — agenthicc already has `worker_id` and `PoolHandle`; path-based prefix operations are a refactor of A-4/A-9 addressing, not a capability gap | A-4 (depth addressing), A-9 (enumeration) |
| R-2 | Inter-agent message board / durable task graph | Codex CLI, PRD-232 | T1 — `AgentMessageBroker` (`communication.py`) exists and PRD-232 already proposes the durable version | Prior art, not a new PRD |
| R-3 | Per-subagent worktree isolation | Claude Code (`isolation: worktree`), Qoder | T2/T3 — 2 of 6, and it is a workspace-scoping feature that `policy.py` already constructs for children | Open question inside A-5 |
| R-4 | Promoting a definition to session main agent (`--agent`) | Qoder CLI only | T1/T2 — 1 of 6, and it is not a subagent-management gap | Not carried |
| R-5 | Definition linting and duplicate-name tooling (`/doctor`) | Claude Code, Qoder | T3 — consequence of A-1's ladder, owns no separate code | A-1 acceptance criteria (M-2) |
| R-6 | Model-catalog injection and staleness notices | Codex CLI | T3 — a mechanism inside model routing | A-2 |
| R-7 | Cost/token display in the TUI | Claude Code, PRD-157 | T4 — PRD-157 is implemented and owns it | Consumed by A-6 |
| R-8 | Turn and tool-call budgets | opencode `steps`, Qoder `maxTurns`, Claude Code `maxTurns` | T1 — already implemented at `types.py:89` | Non-goal in A-2/A-6 (M-3) |

R-8 and R-2 are the two that most often become padding in this kind of report:
both are real, both are visible in several competitors, and both are already
answered locally. Including them would have looked like a more thorough
taxonomy and produced two PRDs that implement nothing.

### 5.6 The area-to-PRD bijection

One PRD per area, one area per PRD. Filenames are provisional until written;
the numbers and the mapping are not.

| Area | PRD | Working title | Gap | Evidence strength |
|---|---|---|---|---|
| A-1 | `prds/SUBAGENT-01-declarative-subagent-types.md` | Declarative subagent types and discovery | G-1 | Strong (5 of 6) |
| A-2 | `prds/SUBAGENT-02-per-type-model-and-context-routing.md` | Per-type model, effort and context routing | G-2 | Strongest (6 of 6) |
| A-3 | `prds/SUBAGENT-03-structured-result-contracts.md` | Structured result contracts | G-3 | Strong, argument-conditional |
| A-4 | `prds/SUBAGENT-04-nesting-depth-and-fan-out.md` | Nesting depth and fan-out governance | G-4 | Strong on mechanism, open on whether to nest |
| A-5 | `prds/SUBAGENT-05-per-type-permission-narrowing.md` | Per-type permission narrowing | G-5 | Strong (3 of 6, one open source in detail) |
| A-6 | `prds/SUBAGENT-06-pool-budgets-and-limits.md` | Pool budgets and limits | G-6 | Weak (T2 not met) |
| A-7 | `prds/SUBAGENT-07-background-orchestration.md` | Background and non-blocking orchestration | G-7 | Strong (4 of 6) |
| A-8 | `prds/SUBAGENT-08-worker-lifecycle-controls.md` | Worker steering, cancellation, retry, salvage | G-8 | Strong (Codex is a working model) |
| A-9 | `prds/SUBAGENT-09-observability-and-transcript-contract.md` | Observability and transcript contract | G-9 | Moderate |

The invariant is checked mechanically at the end of the series: every locked
area has exactly one PRD, every PRD names exactly one area, and no PRD exists
outside this table. If a PRD later splits into two, this table is updated
*first*, because it is the authority — not the PRD.

### 5.7 Ordering: writing order is not implementation order

The nine PRDs are written in numeric order, `SUBAGENT-01` through
`SUBAGENT-09`, because A-1's vocabulary (what a type *is*) is what A-2, A-3,
A-4 and A-5 all reference. That is a **document** order.

**Implementation** dependencies are different and the PRDs must respect them:

- A-2, A-3, A-4 and A-5 all presuppose A-1's type record; none of them can ship
  a field that A-1 has not defined.
- A-6 depends on A-8: A-6 declares limits, A-8 responds to them (M-1).
- A-7 depends on A-8: a background pool that cannot be steered or stopped is a
  leak.
- A-9 comes last among the areas because the event schema is cheapest to
  finalise once the events of A-6, A-7 and A-8 exist.
- A-2 and A-5 are independent of each other and can be taken in parallel.

Confidence order is A-2, then A-1/A-3/A-4/A-5/A-7/A-8, then A-6 and A-9. That
ordering is *not* a commitment to ship in that order; it is what the PRDs should
say when they claim urgency.
