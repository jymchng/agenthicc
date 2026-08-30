# Workflows

A workflow is a Python plugin that defines a named sequence or graph of agent
phases. A phase chooses an agent role, prompt context, capability ceiling, and
transition rules; the runner supplies the session's tools, memory, approvals,
and model configuration.

All direct turns and workflows in one interactive session use one
`SessionConversation`: its stable `conversation_id` is the session ID and its
journal-backed provider memory is reused across phase turns. The reactive
`ConversationStore` is only the display projection. Workflow phase state stays
in a typed workflow context and is checkpointed separately when the runner
supports resumable state.

### Outer session ownership

The workflow lease is nested inside the durable session owner lease. The TUI,
headless workflow runner, session picker, and background attach path first
claim `<session-id>/.owner`; only after that succeeds may they restore the
conversation/journal or construct a workflow. A workflow phase and its tools do
not acquire another session lease: they inherit the one held by the parent
`SessionContext`. The existing `<run-id>/.claim` is still acquired separately
before a resumable workflow executes.

```text
select --continue / --resume / picker
  -> SessionOpenCoordinator claims session .owner
  -> restore SessionConversation and workflow checkpoint
  -> WorkflowRunHandle claims workflow .claim
  -> execute turns, tools, and phase transitions
  -> close resources, release workflow claim, release session .owner
```

This ordering prevents two terminals from replaying or writing one durable
conversation. A busy newest session never silently falls back to an older
session or starts a new one. The losing process receives the typed
`session_already_active` diagnostic before TUI, LLM, tool, or transcript
startup. Session owner records are reclaimed only after local process death is
provable; timestamps are diagnostic, not a stale-lock timeout.

### Pause, crash recovery, and `/workflow resume`

Workflow checkpoints live under the session directory and point to the same
journal-backed `SessionConversation`; they do not copy provider messages. A
checkpoint is written before the first phase turn, on every phase/state entry,
after a transition/artifact boundary, during pause finalization, and at the
terminal boundary. If the process disappears while a checkpoint is `running` or
`resuming`, the next `--resume <session-id>` marks it interrupted, validates the
plugin fingerprint, profile, workspace, journal cursor, and typed context, and
offers it without invoking the model.

Use `/workflow resume` when exactly one recoverable run exists, or
`/workflow resume <run-id>` when the notification lists more than one. An
ordinary message continues the selected paused run using the same policy as an
Esc pause; it never silently creates a fresh phase-one run. Use
`/workflow reset` to always clear the session-local workflow override so
subsequent turns use the active mode's default workflow. Use
`/workflow reset <run-id>` to write a terminal discarded checkpoint for a
saved run that is not currently attached. A live claim prevents two
TUI/headless owners from executing one run at once, while a claim from a
provably dead local process can be reclaimed. Claim publication is atomic and
records a process-start identity where the host supports it, so a half-written
claim, zombie, or reused PID cannot strand a recoverable run. If the message
contains `run_already_claimed`, another live agenthicc owner still has the run;
close that process or resume the run there before retrying. The protection is
intentional: forcibly taking a live claim could duplicate tool side effects.

When multiple workflows are recoverable, the TUI recovery notice wraps the
complete run IDs instead of ellipsizing them. Copy one into
`/workflow resume <run-id>` to select the intended run.

An explicit `/workflow resume <run-id>` refreshes the durable checkpoint index
before reporting `run_not_found`; startup discovery is only a snapshot. The
TUI also resolves a unique run ID copied from a claim diagnostic (including
the trailing ID in `tui:...:<run-id>`) and common terminal-font substitutions
such as `O`/`0` and `l`/`1`. It always resumes the canonical stored ID and never
uses these substitutions when they would match more than one run. A claim
conflict leaves the selected handle attached, so the same `/workflow resume`
command can be retried after the live owner exits.

The recovery data flow is:

```text
checkpoint.conversation_id
  → SessionConversation.open()
  → fold conversation-journal.jsonl + repair incomplete tool tail
  → context_from_payload(..., memory=session.memory)
  → claim WorkflowRunHandle
  → WorkflowConfig(session_memory=session.memory, conversation_id=session.id)
  → runner.resume(typed_context)
```

Corrupt checkpoints, incompatible plugins/profiles/workspaces, cursor drift,
and unrecoverable tool tails stay on disk for diagnosis and produce a stable
error plus a reset/reload action. They are never replaced by a new run.

### Workflow errors are saved before the TUI returns to idle

All workflow setup, phase, provider, tool, timeout, and unexpected-cancellation
errors pass through one idempotent failure finalizer. The finalizer captures a
bounded, redacted diagnostic and the latest safe typed context before it
publishes the outcome:

```text
plugin/workflow setup
  -> durable run_id + bootstrap checkpoint
  -> typed context attached before first provider/tool call
  -> phase/provider/tool/timeout error
  -> classify error and capture phase/iteration/artifacts
  -> typed checkpoint(status=paused, failure_kind=...)
  -> release live claim after persistence
  -> /workflow resume <run-id>
  -> validate conversation/tool tail and call runner.resume(context)
```

Recoverable errors use `status="paused"` with `pause_reason`, `failure_kind`,
the last safe boundary, and an incrementing error revision. They are listed by
the recovery coordinator and can be resumed at the same phase. Timeouts are
handled by this path too; they do not merely close the visible turn.
`WorkflowFailureKind` supplies the stable category vocabulary; unknown custom
labels are normalized to `workflow_error` rather than becoming ad hoc recovery
states.

If an error happens before a typed context exists, or encoding/storage fails,
the store writes a mode-600 `recovery-error.json` diagnostic beside the run
checkpoint when possible. This fallback contains only the run/workflow
identity, an intent digest, phase location, and sanitized error metadata. It is
diagnostic-only and is never offered as resumable. The TUI explicitly tells the
user whether `/workflow resume` is safe instead of silently claiming that a
failed run was saved.

Error finalization releases the live claim after the checkpoint or fallback is
durable. A deliberate same-process Esc pause may retain its in-memory owner
claim for the existing fast resume path; terminal completion, failure, reset,
and clean shutdown release it.

The framework creates the durable run identity before `build_params()` and
`build_runner()`. Custom plugins may implement
`WorkflowPlugin.create_initial_context(intent, run_id, memory)` to provide
typed state even earlier; the framework always supplies the already-open
session memory and never serializes that object. Generated workflows are
validated to attach typed context before their first provider/tool call, avoid
terminalizing ordinary errors themselves, re-raise broad exception handlers
instead of silently returning, and use a true `resume(context)` dispatch path.

Workflow runners also inherit the parent session's immutable
`WorkspaceScope` and live `WorkspaceAccessPolicy`. A custom workflow does not
need to reimplement Safe/Plan/Yolo path handling: filesystem, mention, and
command tools receive the same policy, and a live mode change is evaluated at
the next access. Generated or plugin workflows should pass the existing
`WorkflowConfig` through to phase turns rather than reconstructing a root from
`os.getcwd()`.

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

### `make_book` and its standalone builder

The `make_book` workflow produces a technical PDF and, during its compile
phase, exposes `create_build_book`. The tool creates
`<output_dir>/build_book.py` as a normal executable Python program inside the
run's book output directory. The generated builder discovers `front-matter/`,
`chapters/`, and `back-matter/` relative to that directory, invokes Pandoc and
XeLaTeX for at least two passes, writes the result to `dist/`, and can be rerun
later without an agent session:

```bash
python3 <output_dir>/build_book.py --out dist/my-book.pdf
```

It accepts `--out`/`-o` for a custom PDF path and
`--keep-intermediates`/`--keep` for debugging. It uses a KDP-oriented 6×9
inch layout, no-dot table-of-contents styling, and attaches an optional
`assets/cover.png` (or supported JPG variant) when Pillow and pypdf are
available. `mark_book_complete` is verification-only: the agent must call
`create_build_book()` and run the resulting script before the completion gate
will accept the existing PDF. Its path is stored in the workflow checkpoint
and artifact summary.

#### `make_book` phase handoffs

Every `make_book` phase transition uses the same small model-facing contract:

```python
submit_toc(summary="The plan is ready; the manifest is on disk.")
submit_research(summary="All chapter notes and sources are written.")
confirm_assets_ready(summary="The asset inventory is complete.")
confirm_chapter_complete(summary="This chapter is written and checked.")
confirm_front_matter_ready(summary="The preface and contents container are ready.")
confirm_back_matter_ready(summary="The index is ready.")
mark_book_complete(summary="The builder produced and validated the PDF.")
reject_book(summary="The PDF failed validation because …")
```

The short summary is the only transition argument. The agent writes the
artifacts with its ordinary filesystem/tools first; the runner then verifies
existing files and derives paths, counts, inventories, and PDF selection. A
failed gate neither creates an artifact nor advances the phase.

The TOC agent writes its structured plan to the run-scoped `toc.json` path
shown in its prompt before calling `submit_toc`. Research is file-backed under
`<output_dir>/research/`, with one chapter note file per chapter. Chapter
handoffs use the runner-derived `chapters/NN-title-slug.md` path and calculate
word count and Markdown asset references themselves.

The assets phase is intentionally substantial. It must produce at least
`max(6, 3 * chapter_count)` varied supported files—not merely one asset per
chapter—including Mermaid sources/renders and reproducible charts where
appropriate. It must also place at least one raster image from the free
Unsplash service under `assets/unsplash/` and write
`assets/unsplash/manifest.json` with free `unsplash.com` source URLs. Unsplash+
and paid sources are rejected by the gate. The manifest is provenance
metadata; the transition tool does not download or create images.

`create_build_book()` is different: it is a zero-argument compile utility that
creates `<output_dir>/build_book.py`. It is not a phase transition. The
completion transition remains verification-only and requires that the builder
already exists and that `dist/` contains exactly one existing PDF with a
`%PDF-` header. Transition receipts store compact metadata and canonical paths
so checkpoint/resume preserves progress without copying provider memory or
large research bodies.

When `code_plan` reaches its human plan review, the overlay offers
`Approve - Safe` and `Approve - YOLO`. The selected mode is carried into the
execute phase and is preserved in phase metadata for resume; Safe retains
per-action approval prompts, while Yolo runs the approved execution without
those prompts. Design reviews for `create_workflow` keep their existing
feedback/instructions options.

In the interactive input panel, type `/workflow` and press Space to switch the
command picker to live workflow-name completion. The suggestions come from the
current built-in, user, and project workflow registry; selecting one inserts the
executable `/workflow <name>` command without submitting it.

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
request into a project-local workflow package:

```text
/workflow create_workflow
Create a workflow that uses Cloakbrowser to parse facebook.com.
```

The first line selects the workflow; the next ordinary input supplies its
intent. `create_workflow` is modelled on `code_plan`, and shares its shape
exactly:

- an **outer loop** in `CreateWorkflowRunner.run()` that evolves
  `CreateWorkflowState` and nothing else;
- an **inner loop** in each phase method that runs agent turns until that
  phase's transition tool fires;
- **transitions only via tool calls** — assistant prose is never parsed for a
  handoff signal, so a turn that ends without a tool call is simply retried;
- a **typed context**, `CreateWorkflowContext`, that records the artefact each
  phase produced as a `PhaseArtifact`.

Every workflow turn receives a `[PHASE TRANSITION TOOLS]` prompt block. It names
the control tools actually available in that phase and states that only a
successful call can change phase; words such as “done” are never a handoff.
Built-in workflows receive this through their shared runner, including the
`CodePlanRunner` subclasses. A custom transition callable should use
`@tool_control`, so its own name is discovered and included automatically. A
declarative phase with no control tool is told that its declared graph is applied
by the generic runner after the turn.

### Phase graph

| Phase | Mode | Required handoff | On success | On rejection |
| --- | --- | --- | --- | --- |
| `design` | read-only | `request_design_approval(design, workflow_name)` then `finalize_design(design, workflow_name)` | `generate` | retry `design` |
| `generate` | `Yolo` | write the file, then `mark_generation_complete(summary, path)` | `validate` | — |
| `validate` | read-only | `approve_workflow(summary)` or `reject_workflow(reason)` | `summarize` | `generate` |
| `summarize` | read-only | — (single turn) | complete | — |

`design` may also call `exit_create_workflow(suggestion)` when the request is not
actually about authoring a workflow; the run then ends in the `EXITED` terminal
state without writing anything. Terminal states are `COMPLETE`, `EXITED`, and
`FAILED`.

### Design is human-gated

`request_design_approval` raises the plan-review overlay with the proposed name
and phase graph. `finalize_design` refuses with an actionable `ok: false` result
until that approval returned `approved=True`, and a later denial closes the gate
again, so a rejected design cannot be handed off. In headless runs with no
approval service the request auto-approves.

Rejected transitions always return a structured failure containing `ok: false`,
an `error`, a human-readable `message`, and a concrete `fix` naming the tool to
call again. Invalid workflow names, empty designs, empty summaries, missing
paths, and approval-service errors all take that path and keep the phase active.

### The generated workflow ships its own runner

`create_workflow` asks for a workflow that contains its own state-machine runner,
not just a declarative `PhaseSpec` graph. That is the shape of `code_plan` and
`create_workflow` themselves, and it is the only shape that can express retries,
conditional routing, loops, accumulated context, or phase-local transition tools.

The design phase must state, and the generate phase must write:

1. a typed `State(Enum)` with every non-terminal and terminal state, and an
   `is_terminal` property;
2. a typed `@dataclass` context carrying the intent, each phase's output, and the
   failure reason;
3. one bounded async method per non-terminal state, returning the next state;
4. `run(intent)` building the context and driving
   `while not state.is_terminal` + `match state`;
5. `resume(context)` re-entering the same dispatch path;
6. phase tool factories whose `@tool()` closures set an `asyncio.Event`, checked
   after the turn returns — never parsing the agent's prose. Import `tool` from
   `lauren_ai._tools` and the bare `tool_control` decorator from
   `agenthicc.tools.capabilities`; put `@tool_control` above `@tool()` on every
   transition callable, name them in the phase prompt, and state that only a
   successful call changes phase. Never write `@tool_control()`;
7. `build_runner()` on the plugin returning that runner.

The framework owns error disposition. Generated runners should attach their
typed context to `config.workflow_handle` before the first provider/tool call,
let ordinary exceptions escape to the common failure finalizer, and never mark
an ordinary failed phase complete or write a terminal failed checkpoint in
place of a recoverable error pause. Setup failures without typed context are
saved as diagnostic-only records.

`describe_runner_pattern()` returns this checklist to the agent.
`describe_transition_tool_pattern()` returns the canonical handoff-tool
import/decorator contract, while `show_example_workflow()` returns a complete
working runner to adapt (pass
`"declarative"` for the runner-less fallback). The generated runner subclasses
`CodePlanRunner` for the session wiring and its public
`run_phase(intent=, text=, system_prompt=, mode=, max_turns=, shared_memory=, tools=)`
helper — `tools` is how a custom phase injects its own transition tools — and never
calls `super().run()`, which would execute code_plan's own phases. Validation
warns when a multi-phase workflow inherits `build_runner()` instead, and errors
when the runner it ships is abstract or missing `run`/`resume`.

A purely declarative graph is still correct when every phase really is one
unconditional agent turn.

### Cache-stable workflow turns

Workflow runners use a shared prompt contract when `[execution].prompt_cache`
is enabled. The contract keeps the workflow's immutable policy and deterministic
tool schemas in the stable prefix, while phase instructions, artifacts,
validation reports, questions, answers, and rolling summaries are rendered as
append-only dynamic context. A custom runner should preserve that boundary by
passing its literal policy separately:

```python
await self.run_phase(
    intent=intent,
    text=artifact_and_phase_state,
    system_prompt="Review the current artifact and report blockers.",
    stable_system_prompt=CACHE_CONTRACT,
    mode="Safe",
    max_turns=8,
    shared_memory=context.shared_memory,
    tools=phase_tools,
)
```

Stable tools are ordered before phase-local tools after capability and approval
filtering. Generated workflows must use `CodePlanRunner.run_phase()` (or the
shared `build_workflow_prompt_contract()` helper), must not insert messages into
shared history, and must declare a literal `CACHE_CONTRACT`. The contract also
instructs agents to use the existing `ask_user` tool for missing or ambiguous
requirements and to wait for answers instead of guessing. The
`describe_prompt_cache_contract`, `show_workflow_template`, and
`validate_workflow_cache_contract` inspection tools expose these rules during
authoring; strict validation rejects a generated custom runner that omits them.
The built-in `make_agenthicc_tool` runner also passes one immutable contract to
every analyze, generate, validate, and finalize turn, keeping its tool plan,
generated source path, validation report, and retry state in dynamic context.
The same boundary is used by every built-in workflow, including `code_plan`,
`create_workflow`, `site_imitate`, `make_agenthicc_tool`, and `make_book`.

The runtime records only redacted contract fingerprints and cache epochs in the
conversation journal and workflow checkpoint. A phase change does not change
the stable epoch. A provider/model/profile change, a stable tool or policy
change, provider TTL expiry, or history compaction can legitimately invalidate
reuse and is reported separately. Anthropic uses explicit cache controls when
available; OpenAI-compatible providers rely on stable-prefix reuse; providers
without a supported cache contract use the compatibility path without claiming
a cache hit.

The built-in `code_plan` runner supplies its immutable workflow policy to the
stable prefix by default, and `create_workflow` does the same for its authoring,
checkpoint, and question-asking policy. The latter also retains a redacted
`cache_diagnostic` in its phase context and checkpoint (contract version,
regions, fingerprints, and provider capability only). The diagnostic contains
no prompt text, conversation content, secrets, or tool arguments.

### Generation uses a draft and an atomic publication boundary

`generate` runs with `mode_override="Yolo"` so the workspace-guarded write tools
are available, but it writes only to a run-owned draft under
`.agenthicc/workflows/.drafts/<run-id>/<name>/`. The agent writes `runner.py`
and any workflow-local helper modules there, then calls
`mark_generation_complete(summary, path)` with the exact draft directory. The
framework records an exact manifest containing relative paths, byte/line
counts, and SHA-256 hashes. Repair cycles reuse that same draft and reject
symlinks, traversal, undeclared files, stale siblings, and manifest changes.

The normal workflow registry ignores `.drafts`, so a partial package can never
be selected while it is being generated or validated. After design approval,
manifest validation, deterministic import/contract validation, a bounded fake
runtime smoke check, and validation-agent approval, the framework copies the
verified draft to a temporary sibling and atomically renames it into
`.agenthicc/workflows/<name>/`. Existing packages and legacy `<name>.py` files
are moved to a run-specific `.backups/` directory and restored if publication
fails. Publication evidence records the draft and published fingerprints,
catalog snapshot, validation evidence, run ID, and timestamp. A failed
publication leaves the draft and checkpoint recoverable.

A workflow with its own runner is a few hundred lines, which does not fit in one
tool call under a small completion ceiling — the truncated call is discarded and
nothing reaches disk. The generate prompt therefore instructs a chunked write:
`write_file` for the first `runner.py` chunk, `append_file` for each following
chunk of roughly 60–80 lines split between top-level definitions, sibling writes
for local tools, and then `read_file` to
confirm the whole file landed. The retry reminder tells the agent to resume from
what is already on disk rather than start over. See
`[execution].max_output_tokens` in the
[configuration guide](configuration.md#execution) for the ceiling itself.

### Validation is deterministic first, agent second

Before the validating agent is asked for anything, the runner imports the
generated package exactly the way `load_python_workflows` will and checks it against
the real `WorkflowPlugin` contract. `validate_workflow_file` reports:

- refusal, without importing, for a path outside the workspace root, a missing
  file, a directory without `runner.py`, a non-`.py` legacy file or
  underscore-prefixed entry, or an empty Python source file;
- syntax errors with a line number, and import failures with the exception type
  and message;
- a missing `WorkflowPlugin` subclass, a name that does not match the approved
  design, a reserved builtin name, an empty description, non-list
  `mode_bindings`, or a `build_params({})` that raises or returns the wrong type;
- phase-graph faults: duplicate or empty phase names, non-`PhaseSpec` entries,
  `max_turns` below 1, and `next` / `on_reject` / `on_error` edges that do not
  resolve.
- direct network, process, browser, or MCP imports in package sources; generated
  code must use the parent session's capability-gated tools instead;
- missing custom-runner checkpoint codecs, unsafe resume/error handling, direct
  instruction-file reads, cache-contract violations, and invalid transition
  decorator/import order.

Unreachable phases, unknown `agent_type` or `output_schema` values, and a
filename that differs from the workflow name are reported as warnings.

That report is injected into the validating agent's prompt as evidence. The agent
still has to call `approve_workflow` or `reject_workflow` — the transition is
always a tool call — but **an approval is overridden when the report failed**, and
the run loops back to `generate` with the concrete errors attached. A workflow
that does not import can therefore never be accepted, however confident the model
is.

Runtime startup failures are also surfaced in the TUI. This covers errors that
occur before `run_phase()` opens an agent turn, such as a lazy phase-tool
factory import failure: the exception is rendered, the workflow run is marked
failed, and its handle is discarded rather than leaving the session apparently
idle with a running workflow indicator.

### Budgets

Two previously advisory settings drive the loops:

- `[execution].authoring_max_generation_attempts` (20 by default) bounds the
  inner-loop attempts of every phase *and* the number of `validate → generate`
  repair cycles;
- `[execution].authoring_max_phase_turns` (20 by default) bounds the LLM
  sub-turns inside one agent turn.

Both are clamped to at least 1. Exhausting either budget ends the run in `FAILED`
with a reason naming the missing handoff, and the failure is reported in the
transcript.

### Shared context and tools

The session's `ShortTermMemory` is shared by all four phases, so the generating
agent already has the design in context and can also see prior direct or
workflow conversation subject to compaction. Every
phase also receives `memory_write`, `memory_read`, and `semantic_search`, and the
project tool set filtered by the active mode's blocked capabilities. The built-in
tool registry supplies the workspace-guarded canonical `write_file` tool even when
no project tool plugin exports it, so `generate` can always write its file.

The authoring workflow receives read-only inspection tools whose content is read
live from the running code, so the guidance cannot drift from the API. Each
authoring turn also receives a bounded, redacted effective-session snapshot:
the available tool schemas and capabilities, phase/mode decisions, workspace,
cache, checkpoint, browser, and MCP status. The snapshot is fingerprinted and
cached; secrets, headers, prompt contents, and tool arguments are excluded.
`describe_authoring_session()` returns the snapshot and
`explain_authoring_tool_access(name)` explains an individual availability
decision.

| Tool | Returns |
| --- | --- |
| `describe_phasespec()` | every `PhaseSpec` field with its type, default, and purpose |
| `list_tool_capabilities()` | every `ToolCapability` value with a description |
| `list_agent_roles()` | every `PhaseRole` usable as `agent_type` |
| `describe_cloakbrowser_tools()` | the optional CloakBrowser backend, live tool names, defaults, and security boundary |
| `describe_playwright_tools()` | the optional Playwright backend, live tool names, defaults, and security boundary |
| `describe_runner_pattern()` | the custom-runner checklist and when a runner is required |
| `describe_transition_tool_pattern()` | the canonical import/decorator contract for phase handoff tools |
| `show_example_workflow(style)` | a complete `runner.py` package entry point to adapt — `"runner"` (default) or `"declarative"` |
| `describe_prompt_cache_contract()` | stable/dynamic prompt regions, cache policy, and invalidation ownership |
| `show_workflow_template()` | the cache-stable custom-runner template and required `run_phase()` call |
| `validate_workflow_cache_contract(path)` | execute-gated strict validation of a trusted generated runner's cache/question/tool contract |

The static inspection tools are available in Plan mode. The validator is
execute-gated and is available to the Yolo generation phase after files are
written, because validation imports the target package and executes its module
top level just as the workflow loader does. Every workflow phase gets the
existing `ask_user` tool for clarifying questions. Optional browser and MCP
health is reported as unavailable/not-probed data rather than turning a valid
fallback into a generated-code error.
The phase agent is explicitly reminded that it can ask multiple focused questions
in one call, or across several rounds, whenever requirements are unclear. Every
phase can additionally read the installed agenthicc source and documentation with the session-wide
`inspect_agenthicc_source`, `search_agenthicc_source`, `read_agenthicc_doc`, and
`search_agenthicc_docs` tools — see the [tools guide](tools.md).

### Activate the generated workflow

Run `/workflows reload` after the run so the normal workflow registry discovers
the new file, then select it for a later request:

```text
/workflow cloakbrowser_parse_fb
Parse the requested Facebook page and summarize the results.
```

The active session keeps the conversation ID and provider memory while the
generated workflow is selected. Built-in and generic workflow contexts can be
checkpointed; `create_workflow` resumes its typed outer-loop state rather than
silently restarting DESIGN. This includes generated custom workflows that
pass codec validation and re-enter their saved dispatch loop; a generated
`resume()` that calls `run(intent)` is rejected. Use `/workflow resume` to
continue a paused or interrupted run. `/workflow reset` returns subsequent
turns to the active mode's default workflow; use `/workflow reset <run-id>`
when an unattached saved run also needs an auditable discarded record.

Usage accounting is inherited in the same way. The session supplies one
`UsageLedger` through `WorkflowConfig`; the standard `_run_agent_turn()` phase
boundary forwards it to `code_plan`, `create_workflow`, and generated custom
runners. Their calls, retries, subagents, and compaction are therefore included
in the same session total without custom token code. A plugin that bypasses the
standard turn boundary must explicitly use `UsageLedger` and preserve the
session `conversation_id`, or its provider calls are outside the automatic
workflow accounting contract.

### Delegating execution work

The session-wide `spawn_subagents` tool accepts the same `executor` role name
used by workflow phases. It is a write-capable, build-capable worker: its
available command and filesystem tools are still intersected with the parent
session's active mode and capability gate, so the role does not bypass Safe or
Yolo policy. Use it for tasks such as compiling a generated book or running a
build; use `implementer` for a narrower file-only change.

Subagent timeouts and worker failures are returned as failed results. A partial
pool is never reported as `ok: true` and is not placed in the resume cache;
the parent agent can retry only the failed task or complete it directly. The
`spawn_subagents` call accepts `timeout_s` in seconds for the worker wall-clock
deadline; it defaults to `3600` (one hour), for example
`spawn_subagents(tasks=[...], timeout_s=7200)`. Direct `SubagentWorker` users
may still rely on `SubagentTypeSpec.max_turn_time_s` when no invocation override
is supplied.

### Checkpointing a custom runner

#### Phase annotations and completed-boundary checkpoints

`PhaseSpec` is the stable graph declaration; it is not itself the live cursor.
Every stateful runner should derive its phase index and total from that
declaration and project the actual execution state through the shared helpers:

```python
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    checkpoint_phase_boundary,
    publish_phase_annotation,
)

publish_phase_annotation(
    config,
    PhaseAnnotation(
        workflow_name=self.workflow_name,
        phase_name=phase_name,
        phase_index=phase_index,
        total_phases=len(phase_names),
        run_id=context.run_id,
        intent=context.intent,
        model_id=effective_model,
        phase_iteration=context.phase_iteration,
        phase_attempt=context.phase_attempts.get(phase_name, 0),
        plan_version="my-workflow.v1",
    ),
    context,
)
```

The annotation updates both `AppState.update_workflow_phase()` (the TUI
projection) and `WorkflowRunHandle.update_phase()` (the phase-entry recovery
cursor). It is dynamic runtime state and must not be interpolated into the
cache-stable system prompt.

After a transition tool succeeds, the runner commits the next typed state and
phase output, then calls `checkpoint_phase_boundary()` before publishing or
invoking the next phase. This is a completed-boundary checkpoint, not merely a
phase-entry checkpoint. It is required for ordinary transitions, retries,
rejections, loops, and terminal outcomes. `PhaseBoundaryError` must reach the
framework failure finalizer; a UI update is never proof that persistence
succeeded, and the next provider turn must not start after a checkpoint error.

On resume, reconcile the checkpoint, verified phase receipts, and workflow
journal before constructing a phase prompt or asking the user how to recover.
The valid durable cursor and verified contiguous receipts outrank transcript
summaries. A summary can be retained as dynamic context, but it cannot make a
completed `INIT` phase run again when receipts prove that the next phase is
already active. The shared `reconcile_phase_cursor()` helper is pure and
idempotent; workflow-specific evidence stores supply the verified receipt
names. When no durable evidence exists, record the safe first-phase fallback
explicitly rather than presenting it as successful recovery.

Completed boundaries are appended to the same session journal through
`ConversationJournal.workflow_phase_boundary()` only after the workflow
checkpoint is saved. `fold_workflow_phase_boundaries()` is an auxiliary,
run-scoped index; it never replaces the checkpoint or duplicates the
conversation. Journal write failure is logged as degraded diagnostics after
the primary checkpoint has already made the transition safe.

`create_workflow` exposes `describe_phase_lifecycle()` and
`show_phase_lifecycle_template()` to authoring agents. New generated custom
runners are statically validated and smoke-tested for this annotation,
boundary, resume, and failure contract before publication.

The framework codecs cover `WorkflowContext`, `CodePlanContext`, and
`CreateWorkflowContext`. A plugin with a custom runner/context must opt in
explicitly; `create_workflow` rejects a generated custom runner that omits
either hook:

```python
class MyWorkflow(WorkflowPlugin):
    @classmethod
    def checkpoint_context_to_payload(cls, context):
        return {"state": context.state.name, "result": context.result}

    @classmethod
    def checkpoint_context_from_payload(cls, payload, memory=None):
        return MyContext(
            state=MyState[payload["state"]],
            result=payload["result"],
            shared_memory=memory,
        )
```

The payload must be bounded JSON-compatible data and must attach the supplied
session memory rather than creating another conversation. If the hook is not
implemented on a custom runner, deterministic authoring validation fails. A
legacy or manually installed plugin may still be runnable, but an Esc pause
fails closed and requires `/workflow reset`; it is never silently restarted
from its first phase.

Per-phase models come from `[workflows.create_workflow]`:

```toml
[workflows.create_workflow]
design_model   = ""             # empty → execution.model
generate_model = "claude-opus-5"
validate_model = ""
summary_model  = ""
```

### What the generated workflow should look like

The authoring prompt steers the agent toward the right configuration boundary:

- rely on the inherited `WorkflowPlugin.build_runner()` when declarative
  `PhaseSpec` values are enough;
- define a custom stateful runner when the workflow needs conditional branches,
  loops, retries, transformed context, parallel work, phase-specific tools, or
  custom completion gates;
- define typed `WorkflowParams`, `get_phase_models()`, and `build_params()` for
  values supplied by `[workflows.<name>]` in TOML; and
- include a copy-ready `agenthicc.toml` template when the workflow needs
  configurable model or phase settings.

### Custom stateful runners generated by `create_workflow`

For non-trivial specialized behavior, the authoring agent is encouraged to
generate a runner shaped like `code_plan` and `create_workflow` themselves,
rather than hiding control flow in one generic phase prompt. The generated source
should contain:

1. a typed `State(Enum)` with all non-terminal and terminal states;
2. a typed `@dataclass` context carrying the user intent, run id, current state,
   phase iteration, shared memory, phase outputs, failures, and workflow-specific
   data;
3. one bounded asynchronous function for each non-terminal state;
4. a `run(intent)` driver that initializes the context and advances it with
   `while not state.is_terminal` and `match state` dispatch; and
5. a `resume(context)` implementation that uses the same state functions and
   transitions, never `return await self.run(context.intent)`; and
6. `checkpoint_context_to_payload()` and
   `checkpoint_context_from_payload(payload, memory=None)` methods on the
   plugin. They must serialize the state and resumable artefacts, exclude live
   resources such as session memory/events/locks, and reattach the supplied
   session memory during restore. Every phase prompt must also tell the agent
   to ask focused questions through `ask_user` when requirements are missing or
   materially ambiguous instead of guessing.

Each state function should return the next state explicitly after handling its
success, retry, rejection, and failure paths. It should update phase events and
carry structured handoff data just as `CodePlanRunner` and
`CreateWorkflowRunner` do. Use `BaseWorkflowRunner` for an independent state
machine. Use `CodePlanRunner` and its public `run_phase()` only when the
generated workflow intentionally composes with CodePlan; changing
`CodePlan.phases` alone does not change the CodePlan runner. A custom runner is
not needed for a genuinely simple declarative `PhaseSpec` graph.

The workflow never writes API keys and never silently edits a TOML file. Copy any
generated template into the project's `.agenthicc/agenthicc.toml` (or another
explicitly selected config file), restart the session to load configuration, then
run `/workflows reload` after changing the Python workflow. Provider selection
remains session-wide; a generated workflow must not claim to support per-phase
provider switching.

Project tools and slash commands are separate plugin surfaces. Use the
`/create-tools` and `/create-commands` skills when those extensions are needed;
they are not workflow definitions and are not phases of `create_workflow`.

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

Use `/workflows reload` after adding or editing a workflow file or package. It rebuilds the
session registry in place and reports added or removed workflow names; if
discovery fails, the previous registry remains active. In the workflows
overlay, press Enter on a workflow and then Enter again on its details page to
place `/workflow <name>` in the input panel without submitting it.

To resume from a visual list instead of copying a run ID, use:

```text
/workflows runs
```

The selector refreshes the durable checkpoint index when it opens and shows
recoverable runs in descending checkpoint time order. It includes both
explicitly paused runs and runs found in `running`/`resuming` state after an
interruption. The table is paginated; use the arrow keys for adjacent rows,
PageUp/PageDown for page jumps, and Home/End for the bounds. Enter resumes the
selected run immediately. This invokes the same rehydration, compatibility
validation, conversation/journal restoration, and atomic live-owner claim as
`/workflow resume <run-id>`; the overlay never claims or starts a run itself.
If another live agenthicc process owns the run, the normal
`run_already_claimed` diagnostic is shown and no duplicate execution starts.

## How user workflows are discovered

Workflow discovery happens when a TUI or headless session starts. The registry
loads sources in this order:

1. Built-in workflows
2. User-global Python files in `~/.agenthicc/workflows/`
3. Project-local Python files in `.agenthicc/workflows/`

Later sources replace an earlier workflow with the same `name`, so a project
workflow can intentionally override a user-global or built-in workflow. Files
and workflow directories whose names start with `_` are skipped. Legacy single
Python files may define more than one named `WorkflowPlugin` subclass. A
directory workflow is discovered when it contains `runner.py`; its sibling
Python modules are loaded as the same temporary package, so relative imports
for workflow-specific tools work during startup and `/workflows reload`.

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
durable session. Add `--mode MODE` to choose the runtime policy supplied to
the workflow runner. For the interactive TUI, the same flags select the
initial mode and workflow; an explicit workflow overrides the mode default.
Both paths construct the selected plugin through
`WorkflowPlugin.build_runner`, so specialized built-ins and project workflows
use the same runner contract as the TUI.

Headless approvals fail closed. Approval-gated actions are denied unless the
invocation explicitly supplies `--dangerously-skip-permissions`; this flag
should only be used in a trusted automation environment. It does not bypass
the workspace boundary: headless outside-workspace requests require an
explicit scope-aware approval adapter. Recorded workflow approvals retain the
canonical workspace target and operation, so replay cannot authorize a
different parent path by position alone.

## Minimal plugin

Place a legacy Python file, or a package directory containing `runner.py`, in
`.agenthicc/workflows/` or `~/.agenthicc/workflows/`:

```python
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class ResearchWorkflow(WorkflowPlugin):
    name = "research"
    description = "Inspect a project and report findings."
    mode_bindings = ["Yolo", "Plan"]
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

The file/directory name does not have to match the class name, and the class
does not need a separate registration call. Give the plugin a non-empty `name`, then
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
| `system_prompt_override` | Generic-runner phase prompt seed; replaces the selected role prompt, while the base system prompt and framework policies remain in force |
| `require_explicit_completion` | Continue until `mark_execute_complete()` is called |
| `require_plan_finalization` | Continue until `finalize_plan()` is called |
| `require_explicit_review` | Continue until `approve_review()` or `reject_review()` is called |

### Phase prompt precedence and cache placement

`system_prompt_override` is singular and is a field on `PhaseSpec`. It applies
automatically only when the plugin uses the inherited declarative
`WorkflowRunner`. The generic runner resolves the phase prompt in this order:

```text
non-empty PhaseSpec.system_prompt_override
    ↓ otherwise
AgentsRegistry.get_role_system_prompt(PhaseSpec.agent_type)
    ↓ then, for both branches
requirements-clarification policy
phase-transition-tool instructions
```

The override replaces only the role-specific prompt. It does not replace the
global `base_system_prompt`, remove security/capability enforcement, or remove
the framework instructions that tell the agent to ask questions and use
transition tools. The current phase task, original intent, phase artifacts,
and retry context are separate dynamic context supplied by the runner. A
`human` phase does not invoke an agent turn, so there is no agent system prompt
for `system_prompt_override` to affect.

The cache-aware prompt contract makes an important distinction between the
logical phase prompt and the provider request fields. For a contract-native
workflow turn, the provider-facing request is organized as follows:

| Request region | Contains | Cache behavior |
|---|---|---|
| Stable system prefix | Base system prompt, workflow identity, immutable workflow/cache policy | Stable across phase turns when the workflow/provider contract is unchanged |
| Dynamic context | `system_prompt_override` or role prompt, question policy, transition instructions, tool descriptions, artifacts, summaries, and current task | Appended/rebuilt as phase context; not part of the stable cache prefix |
| Tool list | Capability-filtered tools, ordered with stable tools before phase-local tools | Schemas and ordering are controlled by the prompt contract; authorization remains independent of caching |

This means phase-specific instructions can change from `plan` to `execute`
without deliberately invalidating the stable system prefix. Do not put phase
state, rolling summaries, user answers, or per-phase artifacts into
`stable_system_prompt`/the stable contract. The custom-runner cache contract
is described in [Cache-stable workflow turns](../reference/code-plan.md#cache-stable-workflow-turns).

### Custom runner boundary

Plugins that override `build_runner()` and implement phase functions—such as
`code_plan`, `create_workflow`, and the specialized book/site workflows—own
their phase prompts. Their phase methods call the explicit turn API:

```python
await self.run_phase(
    intent=context.intent,
    text=current_phase_context,
    system_prompt="You are in the VERIFY phase. Check the artifact and report blockers.",
    stable_system_prompt=CACHE_CONTRACT,
    shared_memory=context.shared_memory,
    tools=phase_transition_tools,
)
```

In this path, `system_prompt=` is authoritative and
`PhaseSpec.system_prompt_override` is not read automatically. A custom runner
may deliberately reuse the metadata, but it must fetch the phase spec and pass
`spec.system_prompt_override` to `run_phase()` itself. The explicit prompt is
still augmented by the shared requirements/question and transition policy at
the turn boundary, and it remains dynamic for cache purposes.

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
                mode="Yolo",
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

When the optional CloakBrowser or Playwright integration is selected, the same
session-scoped browser tools are also supplied to direct turns and custom
workflow phases. A browser-capable phase should declare `NETWORK` plus `READ`
for observation or `NETWORK` plus `WRITE` for interaction. The generated
workflow itself should not import either optional package; use the injected
`cloakbrowser_*` or `playwright_*` tools documented by the inspection tools.
`create_workflow` keeps design and validation phases browser-free unless a
downstream author intentionally changes that policy.

### `site_imitate` is mobile-first

`site_imitate` always treats responsive mobile behavior as a required workflow
invariant. Its stable phase contract requires mobile-first layouts that work at
approximately 320px, 375px, 768px, and desktop widths without horizontal
overflow, with responsive navigation, readable typography, responsive images,
and usable touch targets. Every component verification must include responsive
evidence, and the final verification tool rejects a success summary that does
not mention mobile, responsive, or viewport checks.

### Website reconstruction workflows: choosing the right one

`copy_website`, `site_imitate`, and `reconstruct_site` are related but serve
different levels of fidelity and project scope. The spelling of the registered
workflow is `reconstruct_site` (not `reconstuct_site`). All three are built-in,
manually selected workflows and are available through the normal workflow
registry:

```text
/workflow site_imitate
/workflow copy_website
/workflow reconstruct_site
```

The next user message supplies the reference URL and/or the desired product
intent, depending on the workflow's phase prompts.

| Workflow | Primary purpose | Reference analysis | Build and validation scope | Choose it when |
|---|---|---|---|---|
| `site_imitate` | Adapt the visual language of a reference site to a new use case | A focused `analyze` phase produces an analysis and component plan; the contract is not organized around a dedicated Playwright study/report phase | `plan → scaffold → build/verify` per planned component → `final_verify`; mobile-first responsive behavior is a hard invariant | The reference is inspiration or a design direction and the new product, content, and component structure matter more than pixel-level copying |
| `copy_website` | Rebuild a known website with high visual and structural parity | Explicit `extract_target` and Playwright-driven `site_study` phases collect target pages, desktop/mobile observations, and screenshots, then create a design specification | `design_spec → scaffold → implement_layout → implement_pages → implement_data → responsive_pass → parity_verify → final_report`; parity rejection can send work back to layout implementation | The target is a live site that should be reproduced closely, including its layout taste, page structure, responsive behavior, and visual details |
| `reconstruct_site` | Produce a broad, production-oriented reconstruction of a reference site | Deep discovery covers routes, assets, content, interactions, architecture, and design-system evidence | A large staged graph covers bootstrap, shell/components, per-route pages, data, responsive/visual/interaction validation, accessibility, performance, fidelity, infrastructure, package commands, scripts, documentation, and final validation | The result must be a substantial application with an audited implementation and operational/deployment artifacts, not only a visual clone |

#### `site_imitate`: lightweight design adaptation

This is the smallest of the three workflows. It first analyzes the reference
and plans the components needed for the new purpose. It then scaffolds the
application and repeatedly enters a component-level `build`/`verify` cycle,
followed by a site-wide final verification. Its dynamic phase count therefore
depends on how many components the planning phase identifies.

The workflow deliberately emphasizes the new use case and a responsive,
mobile-first result. Its verification contract checks mobile, tablet, and
desktop behavior, overflow, navigation, and touch interaction. It does not
promise that every route, asset, interaction, deployment target, or pixel-level
detail from the reference will be inventoried and reproduced.

#### `copy_website`: focused, Playwright-assisted parity

This workflow is a fixed ten-stage pipeline. It extracts and normalizes the
target first, studies the site with Playwright across viewports, turns that
evidence into design tokens/component and data maps, and then implements the
application. The later responsive pass and `parity_verify` phase are explicit
parts of the contract rather than optional follow-up work. The verifier records
discrepancies and can reject back to `implement_layout` for another fix cycle.

`copy_website` is consequently more demanding than `site_imitate` about
observing a specific live target, but narrower than `reconstruct_site` about
production infrastructure and repository operations. It is the appropriate
middle ground for “copy this site closely and make the result mobile-friendly.”

#### `reconstruct_site`: comprehensive reconstruction and hardening

This workflow has the broadest phase graph. Its early stages build inventories
of routes, assets, content, visual evidence, and interactions before the
architecture and design system are fixed. Implementation then proceeds through
the global shell, shared components, dynamic route/page work, and the data
layer. Quality stages separately exercise responsive behavior, visual parity,
interactions, accessibility, performance, and a final fidelity pass.

It also includes project-operability stages that the other two workflows do
not require as part of their normal contract: SQLite, Prisma, TanStack Query,
environment configuration, Docker, Netlify, Caddy, package commands, scripts,
and documentation, each with corresponding verification phases. Its runner
supports controlled re-entry to an earlier phase when a later validation result
exposes a defect. Use it when “reconstruct” means to investigate and deliver a
complete application surface, not merely to imitate the appearance of a page.

#### `reconstruct_site`: evidence, profiles, and cache behavior

`reconstruct_site` uses the authoritative plan in
`agenthicc.workflows.reconstruct_site.evidence_plan`. The plan contains the
41 canonical phase names, handlers, retry limits, model keys, capability
declarations, artifact kinds, and profile membership. The runner validates the
registry `PhaseSpec` names against that plan at import time and uses the same
plan for fresh execution, resume, progress, model lookup, and re-entry. The
overall counter is therefore the selected graph size (20 for `static`, 21 for
`application`, and 41 for `production`); the repeated route phase is rendered
separately as `page completed/total`.

Select a profile explicitly in the workflow parameters when scope matters:

```toml
[workflows.reconstruct_site]
profile = "static"       # static | application | production | custom
max_reentries = 3

[workflows.reconstruct_site.phase_models]
recon = "fast-research-model"
visual_validation = "vision-model"

# Optional deterministic browser matrix. Omit it for the mobile/tablet/
# desktop defaults (390x844, 768x1024, 1440x900).
[[workflows.reconstruct_site.viewports]]
viewport_id = "mobile"
width = 390
height = 844
touch = true
```

`custom_phases` is accepted as a comma-separated value or a list by the
configuration loader. It must follow the canonical order and include `init`,
`research_gate`, and `final_validation`; omitted phases are recorded in the
manifest with a reason. Profile selection is retained on resume and cannot
silently change the graph for an existing run. `research_gate` is a hard,
tool-controlled boundary: bootstrap cannot begin until the route/viewport/
visual-state/interaction/responsive coverage matrix has a complete baseline,
or the user has explicitly accepted named unavailable cells.

The opening research sequence is evidence-first: `recon` discovers every
in-scope surface, `visual_research` records reference screenshots and measured
rendering observations, `interaction_analysis` records action/state traces,
`content_assets` inventories content/fonts/icons/media, and
`responsive_research` compares the same surfaces across the mobile, tablet,
and desktop matrix. `architecture` and `design_system` may write planning
artifacts but do not mutate the application. The typed contracts live in
`agenthicc.workflows.reconstruct_site.research`; implementation and later
validation consume the resulting `fidelity_baseline` by artifact reference.

Every run writes a durable evidence package below the authorized workspace:

```text
.agenthicc/reconstruct_site/<run-id>/
  manifest.json
  phases/<phase>/<attempt>/<kind>-<sha256-prefix>.<suffix>
```

Research observations, phase receipts, validation summaries, and browser
evidence are content-addressed and published with atomic manifest revisions.
The checkpoint stores the manifest path, revision, artifact IDs, hashes,
screenshot IDs, stale IDs, profile, and a bounded digest—not the large research
bodies. On resume, hashes are checked before reusing an artifact. A missing,
unreadable, changed, or malformed artifact produces a recoverable integrity
diagnostic; it is never treated as completed work. No artificial one-megabyte
checkpoint limit is applied: JSON and filesystem/provider limits remain real
operational errors.

Screenshots are linked to the existing Playwright or CloakBrowser artifact
store, not copied through an unapproved browser client. Each record contains
route, sanitized URL, viewport, dimensions, device scale, page state, role,
backend, artifact ID, content hash, and `complete`/`degraded` status. Repeating
the same capture is idempotent. If a browser is unavailable, the manifest says
which capability is unavailable and records degraded evidence without
inventing an image.

All phase turns, retries, page iterations, validation re-entry, and resume use
the parent session conversation and memory. Stable workflow policy and the
compiled capability-filtered tool bundle remain unchanged within a cache epoch;
phase prompts, answers, routes, artifacts, and validation results are dynamic
context. A genuine tool/backend/configuration change must call
`runner.invalidate_tool_bundle_cache(reason=...)`, which starts a new
diagnostic epoch. Cache metadata records eligibility and fingerprints only; it
does not claim that a provider cache was hit.

Visual and interaction rejection tools accept an explicit phase target. An
unknown or incompatible target returns a structured error and leaves the phase
active. A valid target stales the target phase's and downstream artifact kinds,
records the source/target/reason, and consumes the bounded re-entry budget.
This preserves unaffected route/asset research while forcing dependent work to
be revalidated.

The research-fidelity contract is implemented in
[PRD-178](../../prds/prd-178-reconstruct-site-ui-fidelity-research.md). It
requires the opening research phases to account for every in-scope
route/surface, viewport, visual state, interaction trace, responsive rule, and
asset before implementation begins, with a tool-controlled completeness gate.
The runtime includes both `responsive_research` and `research_gate`. A run
cannot enter `bootstrap` until the gate tool approves a complete baseline or
the user explicitly accepts named unavailable cells.

#### Shared runtime guarantees and important boundaries

The workflows differ in purpose, not in the workflow-engine guarantees. They
are registered built-ins, use typed workflow context, persist phase artifacts
through the checkpoint hooks, and advance only through their transition tools.
Agent prose alone cannot silently move a run to the next phase. They are all
manual-only entries (`mode_bindings = []`), so select one explicitly rather
than expecting an ordinary turn to start it.

Their shared guarantees do not make their contexts interchangeable. A resumed
`copy_website` run restores its study/design/parity artifacts; a resumed
`site_imitate` run restores its component plan and per-component verification;
and a resumed `reconstruct_site` run restores its route, architecture,
implementation, validation, and infrastructure state. Browser/page handles
are live process resources and are not serialized into a checkpoint; a resumed
browser phase must reopen an approved target and recreate its live handles.

The optional browser integration is also not the selection criterion by itself:
`copy_website` explicitly makes Playwright-based study part of its phase
contract, while `site_imitate` and `reconstruct_site` are defined by their
analysis/reconstruction scope and may receive browser tools from the session
when configured. Choose based on the required outcome and evidence, not only
on whether browser tools happen to be available.

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
  or `on_reject` names are found only during execution. Workflows authored
  through `create_workflow` are the exception: its validate phase resolves every
  edge before the run can finish.
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
- Browser resume loses a live page: this is intentional. Inspect checkpoint
  browser metadata and reopen an approved URL explicitly; live page objects
  are never deserialized into a process. Browser handles are opaque and scoped
  to the session, and reusing a mutating call's `operation_id` returns its
  cached receipt without repeating the action.
- `/workflow` does nothing: ensure it is in the canonical built-in command
  registry and intercepted before generic slash dispatch.
- `create_workflow` is unknown: restart the session after upgrading and verify
  that the built-in workflow registry contains it. The command selects the
  authoring workflow; the following ordinary input is the intent.
- Custom runner is ignored: implement `build_runner()`, not the historical
  `runner_factory()` hook, and restart the session after changing the file.

The known workflow correctness findings are retained in
`docs/reference/workflow-review.md` and prioritized in PRD-138 P1.1.
