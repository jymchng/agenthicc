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

## Create a workflow from the input panel

The built-in `create_workflow` authoring workflow turns a natural-language
request into a project-local workflow artifact:

```text
/workflow create_workflow
Create a workflow that uses Cloakbrowser to parse facebook.com.
```

The first line selects the workflow; the next ordinary input supplies its
intent. The authoring run generates one `WorkflowPlugin` plus a customized
`WorkflowRunner` with explicit `run()`/`resume()` context handling, validates
its syntax, phase references, imports, and runner contract without importing
it, then stages the source under `.agenthicc/authoring/`. Publication requires
approval and writes atomically to `.agenthicc/workflows/<name>.py`. A denied
request leaves the staged candidate available for inspection and does not
replace an existing workflow. Every terminal outcome emits a final summary in
the transcript and in the structured `AuthoringResult`.

Run `/workflows reload` after publication so the normal workflow registry
discovers the new file, then select it for a later request:

```text
/workflow cloakbrowser_parse_fb
Parse the requested Facebook page and summarize the results.
```

Use `/workflow resume [run-id]` to resume the newest staged authoring run (or a
specific run), revalidate its manifest and source, and continue at approval
without regenerating it. Use `/workflow reset` to return to the active mode's
default workflow. The authoring result and `WorkflowRunCompleted` event include
the generated name, staged/published paths, manifest, validation findings,
approval state, and the `workflows-reload` activation instruction.

## Create tools and commands

The same two-step journey creates project extensions using the existing loader
contracts:

```text
/workflow create_tool
Create a tool that checks the configured Cloakbrowser endpoint.

/workflow create_command
Create a /cloak-status command that reports the endpoint status.
```

`create_tools` and `create_commands` are the canonical registry names; the
singular `create_tool` and `create_command` spellings are aliases for the TUI
and CLI. Tool authoring must produce a Lauren `@tool`-decorated callable in a
`TOOLS` export and publishes to `.agenthicc/tools/<module>.py`. Command
authoring must produce a `Command` in `COMMAND` or `COMMANDS` and publishes to
`.agenthicc/commands/<module>.py`.

Both are staged under the same run-scoped authoring directory, statically
validated without importing generated code, and require explicit publication
approval. After a tool is published, run `/tools reload` or restart the
session. After a command is published, run `/commands reload`; neither
artifact is active during the authoring run. The structured result identifies
the artifact kind, paths,
hash, approval state, activation instruction, and final summary.

## Inspect tools and workflows

Use the registry overlays to inspect what the current session can execute:

```text
/tools
/workflows
```

Both commands are immediate read-only commands and show selectable entries
with descriptions and details. `/tools` includes built-in, project, and
discovered MCP tools, and labels each tool `builtin` or `plugin`; `/workflows`
includes source, phase topology, runner type, and mode bindings. Press Enter
for details and Esc to close, just as with `/commands` and `/skills`.

Use `/workflows reload` after adding or editing a workflow file. It rebuilds the
session registry in place and reports added or removed workflow names; if
discovery fails, the previous registry remains active. In the workflows
overlay, press Enter on a workflow and then Enter again on its details page to
place `/workflow <name>` in the input panel without submitting it.

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

The registry is built once per session by default. Editing a workflow file can
be picked up with `/workflows reload`; a session restart remains appropriate
when changing dependencies or other process-level state.

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
- `create_workflow` is unknown: restart the session after upgrading and verify
  that the built-in workflow registry contains it. The command selects the
  authoring workflow; the following ordinary input is the intent.
- Custom runner is ignored: implement `build_runner()`, not the historical
  `runner_factory()` hook, and restart the session after changing the file.

The known workflow correctness findings are retained in
`docs/reference/workflow-review.md` and prioritized in PRD-138 P1.1.
