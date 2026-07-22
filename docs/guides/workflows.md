# Workflows

A workflow is a named sequence or graph of phases. A phase chooses an agent
role, tools, model and prompt context, then transitions based on output,
approval, rejection, retry, or error.

## Built-in workflow path

The built-in `code_plan` workflow provides the most complete implementation:

```text
plan → execute → review → summarize
  └──── rejection/retry loops ────┘
```

The generic `WorkflowRunner` executes `WorkflowPlugin` phase specifications.
Workflow selection is influenced by the active mode, registry mappings, and the
session-local `/workflow` override.

## Minimal plugin

Place a Python file in `.agenthicc/workflows/` or
`~/.agenthicc/workflows/`:

```python
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class ResearchWorkflow(WorkflowPlugin):
    name = "research"
    description = "Inspect a project and report findings."
    mode_bindings = ["Auto", "Plan"]
    phases = [
        PhaseSpec(
            name="research",
            agent_type="explorer",
            max_turns=20,
        ),
    ]
```

The loader scans both project-local and user-global directories. Project
definitions take precedence over user definitions with the same name. Files
starting with `_` are skipped.

## PhaseSpec essentials

| Field | Purpose |
|---|---|
| `name` | Stable phase identifier |
| `agent_type` | Agent registry role such as `planner`, `executor`, `reviewer`, or `auto` |
| `max_turns` | Agent-loop bound for the phase |
| `next` | Normal next phase |
| `on_reject` | Phase to run when the output is rejected |
| `on_error` | Phase or terminal policy for errors |
| `max_iterations` | Bound for a rejection/retry loop; `-1` has sentinel semantics in current code |
| `mode_override` | Runtime mode used while the phase runs |
| `allowed_capabilities` | Phase ceiling for tool capabilities |
| `parallel_with` | Other phases that may be launched together |
| `output_schema` | Structured output extraction label |
| `system_prompt_override` | Phase-specific prompt suffix |
| `require_*` | Approval/completion handshake metadata used by selected workflow paths |

Inspect `workflows/plugin.py` before relying on a field. The code-plan runner
has specialized state-machine behaviour and not every declarative field is
necessarily its execution source of truth today.

## Parameters and model overrides

`WorkflowParams` and `[workflows.<name>]` configuration allow tunable workflow
values. Per-phase model selection is passed through the execution config and
must be tested with a real `dataclasses.replace` path rather than a mock-only
assertion.

## Tools and context

The runner can supply project tools, MCP tools, memory tools, skills, mention
content, approval/question tools, and semantic search. A phase must receive the
same context dependencies whether it is generic or code-plan based. Missing
memory or question tools in a runner is a correctness bug, not a documentation
choice.

## Resume and failure behaviour

Workflow state is represented by `WorkflowRun`, phase outputs, kernel events,
and durable conversation state. A resumable implementation must preserve:

- the current phase and run id;
- completed phase history and outputs;
- plan, execution, and review summaries;
- rejection/retry counters and approval state;
- idempotent tool results for interrupted turns.

Parallel failures must not be logged and ignored as if the phase succeeded.
They should produce explicit workflow state and a test for the chosen policy.

## Troubleshooting

- Workflow missing: check syntax/import warnings and the exact plural
  `.agenthicc/workflows/` directory.
- Unknown agent type: inspect the agent registry and its project/user
  precedence.
- No write tools: check active mode, agent role capabilities, and approvals.
- Resume loses context: inspect the journal and `WorkflowRun` phase outputs,
  not only the visible transcript.
- `/workflow` does nothing: ensure it is in the canonical built-in command
  registry and intercepted before generic slash dispatch.

The known workflow correctness findings are retained in
`docs/reference/workflow-review.md` and prioritized in PRD-138 P1.1.
