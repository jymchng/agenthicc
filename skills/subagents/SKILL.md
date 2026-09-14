---
name: subagents
version: 1.0.0
tags: [subagents, concurrency, delegation, spawn, workers]
description: >-
  Delegate work to isolated concurrent subagents: the nine built-in types and
  their tool ceilings, the spawn_subagents tool contract, pool lifecycle and
  aggregation, custom types and aggregators, and how worker isolation works.
---

# Skill: Subagents

A subagent is a short-lived worker agent that runs its own bounded turn loop
with a restricted tool set and reports a plain-text digest back to the parent.
Subagents let one parent turn fan out independent work without giving up
isolation: workers never see the parent's transcript.

## When to use this skill

Use this skill when you need to:

- Understand what `spawn_subagents` can and cannot do
- Pick the right built-in subagent type for a task
- Register a custom type with its own prompt, tool ceiling, and budget
- Interpret a failed or partial aggregate
- Reason about why a worker could not see a file the parent just wrote

Do **not** use this skill for workflow phase design — see
`authoring-workflows`. For the approvals a worker's tools pass through, see
`security-modes-and-approvals`.

---

## The nine built-in types

Types are declared as `SubagentTypeSpec` instances
(`src/agenthicc/subagents/types.py:61`) and preloaded into a process-level
`DEFAULT_REGISTRY` (`subagents/types.py:385`). The registry contains exactly nine types:

| Type | `max_turns` | `max_turn_time_s` | Intended work |
|---|---|---|---|
| `explorer` | 15 | 3600 | Read-only repository investigation and fact gathering |
| `planner` | 10 | 3600 | Read-only production of a numbered implementation plan |
| `implementer` | 20 | 180 | A single scoped code change |
| `executor` | 24 | 300 | End-to-end build or implementation with commands |
| `tester` | 20 | 180 | Write or run tests for a described component |
| `reviewer` | 10 | 3600 | Review a change for correctness |
| `documenter` | 15 | 150 | Write or update documentation |
| `verifier` | 12 | 150 | Adversarially check a specific requirement or invariant |
| `researcher` | 8 | 3600 | Answer specific technical questions by searching |

The default per-worker timeout constant is one hour
(`DEFAULT_SUBAGENT_TIMEOUT_S = 3_600.0`, `subagents/types.py:31`), but only the read-only
types actually use it. The write-capable types are deliberately given tight
wall-clock budgets — an `implementer` gets 180 seconds, a `documenter` 150.

### Tool ceilings

`allowed_tools` is a **ceiling applied after** the parent's phase and mode have
already filtered the session tool list (`subagents/types.py:87-89`). A worker can never
recover a tool the parent phase did not have.

| Type | `allowed_tools` |
|---|---|
| `explorer` | reads, listing, search, `git_blame`, `git_grep`, `git_log`, `git_show` |
| `planner` | reads, listing, search |
| `implementer` | reads, listing, search, `write_file`, `patch_file`, `append_file`, `run_python_expr` |
| `executor` | the implementer set plus `run_bash`, `run_command`, `run_python`, `shell`, `run_tests` |
| `tester` | reads, listing, `write_file`, `patch_file`, `run_bash`, `run_tests`, `run_python_expr` |
| `reviewer` | reads, listing, search, `git_diff`, `git_log`, `run_python_expr` |
| `documenter` | reads, listing, search, `write_file`, `patch_file` |
| `verifier` | reads, listing, search, `git_diff`, `git_log`, `run_tests`, `run_python_expr` |
| `researcher` | reads, listing, search only |

Note what is absent: no type has `git_add`, `git_commit`, or `git_checkout`.
Workers cannot commit. The `reviewer` and `verifier` see `git_diff` but no git
write tools, so they report rather than fix.

---

## The `spawn_subagents` tool

`make_spawn_subagents_tool` (`src/agenthicc/subagents/tool.py:83`) builds the
`@tool()`-decorated callable. Its model-facing schema comes from
`_SpawnTaskInput` (`subagents/tool.py:65`):

```python
class _SpawnTaskInput(TypedDict):
    type: BuiltinSubagentType
    task: str
    context: str | None
```

`context` is `str | None` rather than `NotRequired[str]` on purpose: the
lauren-ai schema generator treats `Optional` annotations as non-required, while
its TypedDict walker does not yet understand PEP 655's `NotRequired` marker. The
comment records that omitting `context` previously made the model-facing
contract disagree with the actual validator.

A call looks like:

```json
{
  "tasks": [
    {"type": "explorer", "task": "Find every caller of load_skill", "context": "Focus on src/agenthicc"},
    {"type": "verifier", "task": "Confirm no caller expects a tuple"}
  ],
  "max_concurrent": 4
}
```

`max_concurrent` defaults to `4` and may be overridden per call
(`subagents/tool.py:87`). The tool validates that `tasks` is a list, that each entry is a
dict with `type` and `task`, and that `max_concurrent` is an integer but not a
`bool` (`subagents/tool.py:201-239`). An empty list returns an error rather than an empty
aggregate.

---

## Isolation model

The worker isolation rules are stated in the tool docstring (`subagents/tool.py:105-115`)
and are worth internalising:

- Workers do **not** receive the parent's message history.
- The task description arrives as a new user message; `context` is folded into
  the role system prompt.
- Each worker gets a fresh short-term memory window (`SubagentTypeSpec` describes
  it as 8,000 tokens).
- The aggregate is the **only** value returned to the parent turn.

Two consequences follow. First, a worker cannot know what the parent just read,
so task descriptions must be self-contained — `SubagentTypeSpec.system_prompt`
has to stand alone because the task text is appended as the first user message,
not merged into the prompt. Second, a worker that writes a file will not
automatically tell the parent anything beyond its own summary text.

### The Yolo policy exception

Subagents are autonomous implementation workers, and `_make_yolo_app_state`
(`src/agenthicc/subagents/pool.py:246`) gives each child an **isolated** Yolo
policy state. The parent's state is never mutated. The rationale recorded in the
docstring is that workers must not inherit the foreground TUI's Safe/Plan
capability gate — otherwise an `implementer` spawned from a Plan-mode session
would be unable to write the very file it was asked to change.

This is the one place where a child is deliberately *less* restricted than its
parent, so treat the parent's mode as no guarantee about what a worker did.

---

## Pool lifecycle and aggregation

| Type | Location | Role |
|---|---|---|
| `SubagentTask` | `subagents/pool.py:222` | `task_id`, `agent_type`, `task_description`, `context` |
| `SubagentResult` | `subagents/pool.py:232` | `ok`, `text`, `error`, `duration_ms`, `tool_calls`, `changed_paths` |
| `AggregatedResult` | `subagents/pool.py:301` | `pool_id`, `total`, `succeeded`, `failed`, `text` |
| `WorkerState` | `subagents/pool.py:197` | Live per-worker status for the footer grid |
| `SubagentPoolState` | `subagents/pool.py:206` | Live pool summary with a `done` property |
| `SubagentWorker` | `subagents/pool.py:415` | One worker execution |
| `SubagentPool` | `subagents/pool.py:777` | Schedules workers under an asyncio semaphore |
| `run_pool` | `subagents/pool.py:1143` | Convenience wrapper that builds a pool and runs it |

`SubagentResult.label` uses a stable human label form such as `"explorer #1"`
or `"tester #2"` (`subagents/pool.py:237`). `changed_paths` holds the paths that were
supplied to mutation tools, derived from `_MUTATING_TOOL_NAMES` (`subagents/pool.py:311`),
which is why it is trustworthy evidence of what a worker touched: it is captured
from the tool input rather than parsed out of the worker's prose.

`AggregatedResult.text` is the labelled concatenation delivered to the parent as
the tool result, produced by `_aggregate` (`subagents/pool.py:1093`) unless a custom
aggregator is registered.

Worker and complete-pool records are also written to the shared conversation
journal before the tool callable returns, so output survives a parent
cancellation that happens before the parent commits its tool exchange.

---

## Custom types and aggregators

Register a type with `SubagentTypeRegistry.register` (`subagents/types.py:406`) and a
custom digest with `register_aggregator` (`subagents/types.py:414`).

```python
from agenthicc.subagents import (
    SubagentAggregator,
    SubagentResult,
    SubagentTypeSpec,
    SubagentTypeRegistry,
)

SPELUNKER = SubagentTypeSpec(
    name="spelunker",
    allowed_tools=frozenset({"read_file", "read_lines", "grep_files"}),
    max_turns=6,
    system_prompt=(
        "You trace one symbol to its origin. Read only what you need, then "
        "report the defining file and line."
    ),
)


class SpelunkerDigest(SubagentAggregator):
    agent_type = "spelunker"

    def aggregate(self, results: list[SubagentResult]) -> str:
        return "\n".join(f"- {r.text.strip()}" for r in results if r.ok)


registry = SubagentTypeRegistry()
registry.register(SPELUNKER)
registry.register_aggregator(SpelunkerDigest())
```

`SubagentAggregator.aggregate` raises `NotImplementedError` in the base class
(`subagents/types.py:51-58`), so an aggregator that forgets to override it fails loudly
rather than silently returning an empty digest.

The registry also exposes `get`, `get_aggregator`, `names`, and `__contains__`
(`subagents/types.py:410-429`). The spawn tool augments its `type` schema from the
registry it is given (`subagents/tool.py:419`), so a custom type becomes selectable as
soon as it is registered.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `tasks[i] must be a dict with 'type' and 'task' keys` | Malformed task entry | Send `{"type": ..., "task": ...}`; `context` is optional |
| `tasks[i] missing 'type' field` | Type omitted | Name one of the registry's types |
| `max_concurrent must be an integer` | A float or `bool` was passed | Pass a plain integer |
| `tasks list is empty` | No work to do | Omit the call instead of sending `[]` |
| Worker reports it cannot find a file the parent just created | Workers get a fresh memory and no parent transcript | Put the path in `task` or `context` |
| Worker failed with no useful text | Failure is reported per worker, not raised | Inspect `AggregatedResult.failed` and each result's `error` |
| Worker exceeded its budget | Per-type wall clock is tight for write-capable types | Split the work or raise `max_turn_time_s` on your type |
| Worker wrote files you did not expect | Children run with an isolated Yolo policy | Read `changed_paths`; do not infer worker permissions from the parent mode |

---

## Key points

- Nine built-in types live in `DEFAULT_REGISTRY` (`subagents/types.py:385`); the default
  per-worker timeout is one hour, but write-capable types get far less.
- `allowed_tools` is a ceiling applied after the parent's phase and mode filters,
  so a worker can never exceed the parent.
- No subagent type can commit to git.
- Workers get no parent transcript and a fresh short-term memory; task text must
  be self-contained.
- Each worker runs with an isolated Yolo policy so it is not blocked by the
  parent's Safe/Plan gate — a deliberate exception worth remembering.
- The aggregate text is the only value returned to the parent turn.
- Worker and pool records are journaled before the tool returns.
- `changed_paths` is derived from tool input, not from the worker's prose.
