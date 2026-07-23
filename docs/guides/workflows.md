# Workflows

A workflow is a Python plugin that defines a named sequence or graph of agent
phases. A phase chooses an agent role, prompt context, capability ceiling, and
transition rules; the runner supplies the session's tools, memory, approvals,
and model configuration.

There are two supported authoring levels:

- A `WorkflowPlugin` with `PhaseSpec` values uses the generic
  `WorkflowRunner`.
- A plugin can override `build_runner()` and provide a custom
  `BaseWorkflowRunner`, which is how the built-in `code_plan` workflow and
  composite workflows add specialized behaviour.

## Built-in workflow path

The built-in `code_plan` workflow provides the most complete implementation:

```text
plan → execute → review → summarize
  └──── rejection/retry loops ────┘
```

The generic `WorkflowRunner` executes `WorkflowPlugin` phase specifications.
Workflow selection is influenced by the active mode, registry mappings, and the
session-local `/workflow` override.

## How user workflows are discovered

Workflow discovery happens when a TUI or headless session starts. The registry
loads sources in this order:

1. Built-in workflows
2. User-global Python files in `~/.agenthicc/workflows/`
3. Project-local Python files in `.agenthicc/workflows/`

Later sources replace an earlier workflow with the same `name`, so a project
workflow can intentionally override a user-global or built-in workflow. Files
whose names start with `_` are skipped. A single Python file may define more
than one named `WorkflowPlugin` subclass.

Workflow files are imported as Python code during discovery. There is no
workflow-specific trust prompt at import time, so only place code there that
you trust. Tool capabilities, modes, and approvals still apply when phases
run.

The registry is built once per session. Editing a workflow file requires a
session restart; `/skills reload` does not reload workflows.

## CLI and headless execution

Workflows can run without the interactive workspace, which makes them usable in
automation and CI:

```bash
uv run agenthicc workflows list --json
uv run agenthicc workflows run code_plan --intent "Implement the requested change"
printf '%s\n' "Run the verification workflow" \
  | uv run agenthicc --headless --workflow code_plan
```

`workflows run` emits a single result. `--headless --workflow NAME` emits a
ready record followed by one JSON result per non-empty stdin line, reusing one
durable session. Both paths construct the selected plugin through
`WorkflowPlugin.build_runner`, so specialized built-ins and project workflows
use the same runner contract as the TUI.

Headless approvals fail closed. Approval-gated actions are denied unless the
invocation explicitly supplies `--dangerously-skip-permissions`; this flag
should only be used in a trusted automation environment.

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

The filename does not have to match the class name, and the class does not
need a separate registration call. Give the plugin a non-empty `name`, then
verify discovery with `uv run agenthicc workflows list --json`.

## PhaseSpec essentials

| Field | Purpose |
|---|---|
| `name` | Stable phase identifier |
| `agent_type` | Agent registry role such as `planner`, `executor`, `reviewer`, or `auto` |
| `max_turns` | Agent-loop bound for the phase |
| `next` | Normal next phase |
| `on_reject` | Phase to run when the output is rejected |
| `on_error` | Reserved error-transition metadata; not currently executed by the generic runner |
| `max_iterations` | Bound for a rejection/retry loop; `-1` has sentinel semantics in current code |
| `mode_override` | Runtime mode used while the phase runs |
| `allowed_capabilities` | Phase ceiling for tool capabilities |
| `allowed_capabilities_override` | Explicit capability ceiling taking precedence over the role default |
| `parallel_with` | Other phases that may be launched together |
| `output_schema` | Structured output extraction label |
| `system_prompt_override` | Replaces the role's default system prompt for the phase |
| `require_explicit_completion` | Continue until `mark_execute_complete()` is called |
| `require_plan_finalization` | Continue until `finalize_plan()` is called |
| `require_explicit_review` | Continue until `approve_review()` or `reject_review()` is called |

Inspect `workflows/plugin.py` before relying on a field. The code-plan runner
has specialized state-machine behaviour and not every declarative field is
necessarily its execution source of truth today.

## Parameters and model overrides

`WorkflowParams` and `[workflows.<name>]` configuration allow tunable workflow
values. For example, the built-in `code_plan` workflow accepts:

```toml
[workflows.code_plan]
plan_model = ""
execute_model = "claude-haiku-4-5"
review_model = ""
summary_model = ""
```

Custom plugins receive the raw section through `build_params()`. A generic
plugin gets the base `WorkflowParams`, which has no custom settings, unless it
overrides `build_params()` with its own typed `WorkflowParams` subclass.

## Composite workflows

To extend `code_plan`, subclass `CodePlanRunner`, call `super().run(intent)`,
and use the public `run_phase()` method for the additional work. The plugin
must override `build_runner()` so the registry selects the custom runner:

```python
from agenthicc.workflows.code_plan import CodePlanRunner
from agenthicc.workflows.code_plan.definition import CodePlan


class DocumentationRunner(CodePlanRunner):
    workflow_name = "code_plan_docs"
    total_phases = 5

    async def run(self, intent: str):
        ctx = await super().run(intent)
        if ctx.plan or ctx.execute_summary:
            await self.run_phase(
                intent=intent,
                text=(
                    f"[PLAN]\n{ctx.plan}\n\n"
                    f"[IMPLEMENTATION]\n{ctx.execute_summary}\n\n"
                    "Review and update the project documentation."
                ),
                system_prompt="You are the documentation update phase.",
                mode="Auto",
                max_turns=12,
                shared_memory=ctx.shared_memory,
            )
        return ctx


class CodePlanDocs(CodePlan):
    name = "code_plan_docs"
    description = "Plan, implement, review, summarize, then update docs."
    mode_bindings = ["Plan"]

    @classmethod
    def build_runner(cls, config, mode_manager):
        return DocumentationRunner(config, mode_manager)
```

This is the same extension pattern used by the working
`.agenthicc/workflows/code_plan_docs.py` example in the sibling
`python-password-generator` project. `runner_factory()` is historical API
terminology; current dispatch calls `build_runner(config, mode_manager)`.

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

Current implementation caveats to account for when authoring workflows:

- Phase graph references are not validated at discovery time; invalid `next`
  or `on_reject` names are found only during execution.
- `on_error` is declared on `PhaseSpec` but is currently reserved rather than
  an active transition hook.
- Generic parallel-phase failures are logged while the workflow may continue;
  do not rely on parallel execution for an all-or-nothing result without
  testing that policy.
- `CodePlanRunner` owns its own state machine and prompts. Changing
  `CodePlan.phases` does not redefine the built-in `code_plan` execution path;
  use a custom runner for changes to that flow.
- Generic workflows do not automatically receive every specialized
  `code_plan` question/completion tool. Test the tools exposed to each custom
  phase explicitly.

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
- Custom runner is ignored: implement `build_runner()`, not the historical
  `runner_factory()` hook, and restart the session after changing the file.

The known workflow correctness findings are retained in
`docs/reference/workflow-review.md` and prioritized in PRD-138 P1.1.
