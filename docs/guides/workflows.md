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

For the state machine, phase-local transition tools, retry boundaries, and
extension pattern, see the [`code_plan` structure reference](../reference/code-plan.md).

## Command lifecycle gates

Declare command intent in a phase when a build or development server is part
of the workflow contract:

```python
PhaseSpec(
    name="build",
    terminal_wait_policy="foreground",
    command_lifecycle="oneshot",
    require_successful_commands=True,
)

PhaseSpec(
    name="preview",
    terminal_wait_policy="background",
    command_lifecycle="service",
    require_readiness=True,
)
```

The runner receives structured command outcomes from the shared execution
layer. A non-zero exit, timeout, cancellation, spawn failure, rejection, or
orphaned handle prevents `next` from being followed. Service phases retain the
owned terminal handle and readiness evidence in phase metadata; `running` is
not treated as a completed finite command. User-defined workflow validation
rejects invalid lifecycle/policy combinations before activation.

## Create a workflow from the input panel

The built-in `create_workflow` authoring workflow turns a natural-language
request into a project-local workflow artifact:

```text
/workflow create_workflow
Create a workflow that uses Cloakbrowser to parse facebook.com.
```

The first line selects the workflow; the next ordinary input supplies its
intent. The authoring agent generates one complete Python `WorkflowPlugin`
source file directly. Each generated `PhaseSpec` carries a literal
`system_prompt_override` describing its objective, tools, inputs, outputs,
verification, completion signal, and handoff. Declarative workflows use the
inherited generic `WorkflowRunner`; custom `run()`/`resume()` implementations
are reserved for behavior the phase graph cannot express. Publication requires
approval and writes atomically to `.agenthicc/workflows/<name>.py`. A denied
request leaves the staged candidate available for inspection and does not
replace an existing workflow. Every terminal outcome emits a final summary in
the transcript and in the structured `AuthoringResult`.

If the design agent returns analysis, tool-call activity, or incomplete output
instead of source, authoring reports the exact parser finding, emits a visible
retry notice, and sends correction instructions back to the agent. It retries
up to `[execution].authoring_max_generation_attempts` complete attempts (3 by
default, bounded to 1–10) before failing without staging or publishing a
partial artifact.

Authoring phases are explicit state-machine nodes. The definition supplies a
separate phase prompt and turn budget for `interpret`, `design`, `stage`,
`validate`, `review`, `publish`, and `summarize`; the operator can cap every
phase with `[execution].authoring_max_phase_turns` (20 by default). The built-in
`create_workflow` definition gives all seven phases a 20-turn budget; a lower
execution setting remains an intentional global cap. Each
agent-controlled phase owns its agent turn and uses a phase-local completion
tool as its handoff gate. If an inspection or other intermediate turn ends
without that tool, the phase emits a visible retry and continues up to its
bounded limit instead of failing with an uncaught transition error. Deterministic
staging, validation, approval, publication, and summary steps advance only
after their own gate succeeds. `submit_generated_source` captures the complete
raw Python file directly, so the authoring contract does not depend on an XML,
JSON, or Markdown response envelope.

Like `code_plan`, one `ShortTermMemory` is created for each authoring run and
shared by every `create_workflow` phase. The phase tool set also includes
`memory_write`, `memory_read`, `semantic_search`, and `publish_artifact`, so the
authoring agent can carry decisions and relevant context across interpretation,
design, validation, review, and publication without using memory to bypass a
transition or validation gate.

The design agent also receives two read-only built-ins:
`inspect_agenthicc_documentation(path)` reads the installed documentation, and
`inspect_agenthicc_source(module, symbol)` uses Python's `inspect` API against
the installed `agenthicc` package. The TUI preserves complete module and path
arguments in the tool-call preview, so a displayed inspection target matches
the value actually sent to the tool. These tools are intended to keep generated
workflows, tools, and commands aligned with the current API surface. The
documentation tree is included in built distributions under
`share/agenthicc/docs` and remains available from the repository checkout.

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

The authoring agent is also instructed to choose the right configuration
boundary for the generated workflow. It can:

- rely on the inherited `WorkflowPlugin.build_runner()` when declarative
  `PhaseSpec` values are enough;
- define a custom `WorkflowRunner`, `BaseWorkflowRunner`, or `CodePlanRunner`
  and wire it through `WorkflowPlugin.build_runner()` only when the phase graph
  cannot express the required orchestration. Direct lifecycle methods are
  valid; `super()` is needed only for intentional composition;
- define typed `WorkflowParams`, `get_phase_models()`, and `build_params()` for
  values supplied by `[workflows.<name>]` in TOML; and
- include a copy-ready `agenthicc.toml` template when the workflow needs
  configurable model or phase settings.

The authoring run publishes the Python workflow artifact only. It never writes
API keys or silently edits a TOML file. Copy the generated template into the
project's `.agenthicc/agenthicc.toml` (or another explicitly selected config
file), restart the session to load configuration, then run `/workflows reload`
after changing the Python workflow. Provider selection remains session-wide;
the generated workflow must not claim to support per-phase provider switching.

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
values. The end-user setup, custom `build_params()` example, and configuration
precedence are documented in [Custom workflows and TOML configuration](custom-workflows-and-config.md).
For example, the built-in `code_plan` workflow accepts:

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

The model override is phase-specific, but provider selection is not: the
current session uses one `[execution].provider` and phase parameters replace
only the model. Do not add a per-phase `provider` key expecting the default
runner to switch transports.

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
