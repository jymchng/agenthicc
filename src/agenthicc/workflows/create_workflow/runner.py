"""CreateWorkflowRunner — explicit state-machine runner for create_workflow.

``create_workflow`` is the meta-workflow downstream users invoke to author their
own custom workflows.  Its shape is deliberately the same as ``code_plan``:

    state = CreateWorkflowState.DESIGN
    while not state.is_terminal:
        match state:
            case DESIGN:    state = await self._design(ctx)
            case GENERATE:  state = await self._generate(ctx)
            case VALIDATE:  state = await self._validate(ctx)
            case SUMMARIZE: state = await self._summarize(ctx)

* the **outer loop** above is the only place phase state evolves;
* each phase method is an **inner loop** that runs agent turns until that
  phase's transition tool fires;
* a phase advances **only** because a tool set its ``asyncio.Event`` — the
  agent's prose is never parsed for a transition signal;
* :class:`~agenthicc.workflows.create_workflow.state.CreateWorkflowContext`
  captures the artefact each phase produced.

The one place the runner adds judgement of its own is VALIDATE: the generated
file is imported and checked deterministically (see
:mod:`agenthicc.workflows.create_workflow.validation`) *before* the agent votes,
and an ``approve_workflow`` call is re-routed back to GENERATE when that check
failed.  A broken workflow can therefore never be accepted, however confident
the agent is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.base import ToolLike
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    PhaseBoundaryError,
    checkpoint_phase_boundary,
    publish_phase_annotation,
    reconcile_phase_cursor,
)
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.create_workflow.draft import DraftManifest
    from agenthicc.workflows.create_workflow.catalog import AuthoringSnapshot
    from agenthicc.workflows.create_workflow.validation import ValidationReport
    from agenthicc.workflows.plugin import WorkflowRun

log = logging.getLogger(__name__)

# Gap (lauren-ai): tools are @_tool()-decorated callables with no public ABC.
_ToolList = list[ToolLike]

#: Ordered phase names, also the payload of ``WorkflowRunStarted``.
_PHASE_NAMES: tuple[str, ...] = ("design", "generate", "validate", "summarize")

#: Zero-based status-bar position of each phase.
_PHASE_INDEX: dict[str, int] = {name: index for index, name in enumerate(_PHASE_NAMES)}

#: Class-attribute name holding each phase's static model override.
_PHASE_MODEL_ATTR: dict[str, str] = {
    "design": "design_model",
    "generate": "generate_model",
    "validate": "validate_model",
    "summarize": "summary_model",
}

#: Conventional directory for project-local workflow plugins.
_WORKFLOW_DIR: str = ".agenthicc/workflows"


def _evidence_fingerprint(value: object) -> str:
    """Return a deterministic identity for bounded JSON-safe evidence."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Stable authoring policy for the meta-workflow itself.  The design, approved
# artifacts, generated paths, validation reports, rejection feedback, and
# retry state are all dynamic phase context and must never be interpolated into
# this string.  ``_run_turn`` passes it through the same structured composer
# used by every other workflow.
CACHE_CONTRACT: str = """
[CREATE_WORKFLOW CACHE CONTRACT]
Keep this workflow's authoring, safety, capability, checkpoint, and transition
policies unchanged across DESIGN, GENERATE, VALIDATE, and SUMMARIZE. The user
request, current phase, approved design, generated artifacts, validation
reports, questions, answers, rejection feedback, and retry details are dynamic
context. Do not put changing values into this stable contract, prepend
messages to shared history, rewrite old conversation entries, or place rolling
summaries here.

Ask the user a focused clarifying question through the existing ask_user tool
whenever required information is missing, ambiguous, or could materially change
the generated workflow. Wait for the answer instead of guessing. The question
policy is stable; each actual question and answer remains dynamic. Prompt
caching never replaces capability filtering, approval, workspace policy, or
tool authorization. Use the parent session's conversation_id and injected
session memory for every phase, retry, and resume; never create a second
conversation or replace the session memory. Provider TTL expiry, connection
changes, stable-contract changes, and history compaction may intentionally
invalidate reuse.
""".strip()

# ── system prompts ────────────────────────────────────────────────────────────

_RUNNER_GUIDE: str = (
    "WRITE THE WORKFLOW'S OWN RUNNER INSIDE THE SAME FILE. This is the default and "
    "strongly preferred shape — it is how code_plan and create_workflow themselves are "
    "built, and it is the only shape that can express retries, branches, loops, "
    "accumulated context, or phase-local transition tools.\n"
    "A custom runner contains:\n"
    "  1. a typed State(Enum) with every non-terminal and terminal state, plus an "
    "is_terminal property\n"
    "  2. a typed @dataclass context carrying the intent, each phase's output, and the "
    "failure reason\n"
    "  3. one bounded async method per non-terminal state, returning the next state\n"
    "  4. run(intent): build the context, then drive "
    "'while not state.is_terminal' + 'match state'\n"
    "  5. resume(context): re-enter the same dispatch path; never implement a "
    "checkpoint resume as `return await self.run(context.intent)` or any fresh-run restart\n"
    "  6. phase tool factories whose @tool() closures set an asyncio.Event, and whose "
    "transition callables are also marked @tool_control — the state method checks the "
    "event after the turn and NEVER parses the agent's prose. Import `tool` from "
    "lauren_ai._tools`, import the bare `tool_control` decorator from "
    "agenthicc.tools.capabilities`, and write `@tool_control` (never "
    "`@tool_control()`) above `@tool()`\n"
    "  7. every phase system prompt MUST explicitly name the phase-transition tool(s) "
    "available in that phase, explain which tool advances or branches the state, and "
    "tell the phase agent that prose such as 'done' never advances the workflow; only "
    "a successful transition-tool call does\n"
    "  8. build_runner() on the plugin returning that runner\n"
    "  9. checkpoint_context_to_payload(context) and "
    "checkpoint_context_from_payload(payload, memory=None) on the plugin. The context "
    "must carry run_id, current state, phase_iteration, completed artefacts, and all "
    "workflow-specific resume data; omit asyncio.Events, locks, clients, and session "
    "memory from the payload, then attach the supplied memory object during restore. "
    "The payload must contain only bounded JSON-compatible values.\n"
    "  10. run() and resume(context) must use config.session_memory when it is supplied, "
    "use config.conversation_id unchanged for every phase and resume, and must never "
    "create a second conversation for a resumed run. Use "
    "config.workflow_handle to attach context and publish the current phase.\n"
    "  10a. Use the shared phase lifecycle contract: import PhaseAnnotation, "
    "publish_phase_annotation(), and checkpoint_phase_boundary() from "
    "agenthicc.workflows.phase_lifecycle. Build the annotation from the exact "
    "PhaseSpec plan (phase name/index/total, run ID, intent, effective model, "
    "iteration/attempt, and plan version), and publish it before every phase turn, "
    "including retries and resume. After a transition tool succeeds, commit the "
    "next typed state and output, call checkpoint_phase_boundary(), and only then "
    "enter/publish the next phase. A PhaseBoundaryError must propagate; never "
    "silently continue after checkpoint failure. Use describe_phase_lifecycle() "
    "and show_phase_lifecycle_template() for the exact call shape.\n"
    "  11. inherit config.workspace_scope and config.workspace_access unchanged. Never "
    "construct a second WorkspaceScope, bypass the policy with a raw filesystem call, "
    "or treat a custom runner as permission to access a parent directory. Use the "
    "standard CodePlanRunner.run_phase() path so every phase, retry, and subagent "
    "turn receives the same live Safe/Plan/Yolo policy; custom path-aware tools must "
    "use current_workspace_access() or an explicitly authorized adapter.\n"
    "Subclass CodePlanRunner for the session wiring and its public run_phase(intent=, "
    "text=, system_prompt=, mode=, max_turns=, shared_memory=, tools=) helper; use the "
    "injected session memory for every call (create a local fallback only when no session "
    "memory was supplied). Never "
    "call super().run() — that would execute code_plan's own phases.\n"
    "  12. Add a module-level CACHE_CONTRACT stable system prompt to the generated "
    "workflow. It MUST tell the workflow agent to ask the user clarifying questions "
    "through the existing ask_user tool whenever required information is missing, "
    "ambiguous, or materially changes the result, and to wait for the answer instead "
    "of guessing. Pass it as stable_system_prompt=CACHE_CONTRACT to run_phase(); keep "
    "phase instructions, artifacts, questions, and answers in the dynamic phase text.\n"
    "  13. Keep stable tools separate from phase-local transition/write tools and use "
    "the deterministic tool ordering inherited from run_phase(). Never insert messages "
    "into the beginning of shared memory or put a rolling summary into CACHE_CONTRACT.\n"
    "  14. The framework creates a durable run identity before setup and attaches a "
    "bootstrap context before build_params()/build_runner(). Attach the typed context "
    "to config.workflow_handle before the first provider or tool call. Do not swallow "
    "exceptions or mark a failed run complete; let the framework's failure finalizer "
    "persist an error-paused checkpoint or a diagnostic-only fallback.\n"
    "  15. A recoverable error must preserve the current state, phase iteration, "
    "artefacts, and run_id. resume(context) must use the supplied context and the "
    "same session memory, and must be safe to repeat after another error.\n"
    "  16. If a workflow truly requires a deferred session resource, declare its "
    "readiness phase names in the plugin's required_startup_phases tuple and let "
    "the framework await them. Keep optional integrations out of that tuple; use "
    "a real fallback so optional failure does not block local work. The runner may "
    "also call config.wait_for_startup() for a phase-local dependency, but it must "
    "never construct a second MCP/browser/provider resource.\n"
    "  17. Resume reconciliation must happen before any phase prompt, provider call, "
    "or recovery question: verified checkpoint/receipt/journal state outranks a "
    "transcript summary, and a stale INIT cursor must advance to the earliest verified "
    "incomplete phase.\n"
    "Call describe_runner_pattern(), describe_phase_lifecycle(), "
    "show_phase_lifecycle_template(), and describe_transition_tool_pattern() for the "
    "full checklist and canonical decorator/import pattern, then "
    "show_example_workflow() for a complete working runner to adapt. Omit the runner ONLY "
    "when every single phase is one unconditional agent turn with no retry and no branch."
)

_AUTHORING_GUIDE: str = (
    "A custom workflow is a directory at "
    f"{_WORKFLOW_DIR}/<name>/ with runner.py as its entry point. It may contain "
    "__init__.py and workflow-specific tools/helpers in sibling files such as tools.py. "
    "runner.py defines the WorkflowPlugin subclass:\n"
    "  - name: lower_snake_case identifier, unique and not a builtin\n"
    "  - description: one line shown in the workflow picker\n"
    "  - mode_bindings: list of mode names that auto-select it ([] = manual only)\n"
    "  - optional integration declarations: use required_integrations, optional_integrations, "
    "and integration_fallbacks (a mapping keyed by integration name) when the workflow uses "
    "CloakBrowser, Playwright, MCP, or another optional service; a required unavailable "
    "integration must have a working fallback or validation will reject the package\n"
    "  - readiness dependencies: declare only truly required deferred session phases in "
    "required_startup_phases (for example ('mcp',) or ('browser',)); optional resources "
    "must remain on a real fallback path and must not block unrelated local turns\n"
    "  - phases: ordered list of PhaseSpec objects wired with next / on_reject — this is "
    "the declarative metadata the registry and the TUI phase counter read\n"
    "  - build_runner(): returns the workflow's own runner (see below)\n"
    "  - checkpoint codecs: required for a custom runner/context so the generated "
    "workflow can pause and resume after Esc or a process restart\n"
    "  - phase prompts: each prompt must list its available transition tool(s), mark "
    "those callables with @tool_control, and state that only a successful call to one "
    "of them changes phase\n"
    "  - workspace policy: inherit WorkflowConfig.workspace_scope and "
    "WorkflowConfig.workspace_access unchanged; custom path-aware tools must use the "
    "same policy and must never create a second scope or raw unrestricted sandbox\n"
    "  - build_params(): typed WorkflowParams when [workflows.<name>] config is needed\n\n"
    + _RUNNER_GUIDE
    + "\nUse describe_authoring_session() for the effective live session catalog and "
    "explain_authoring_tool_access(tool_name) for a decision trace. Use "
    "describe_phasespec(), list_tool_capabilities(), list_agent_roles(), "
    "describe_cloakbrowser_tools(), describe_playwright_tools(), "
    "describe_runner_pattern(), describe_phase_lifecycle(), "
    "show_phase_lifecycle_template(), describe_transition_tool_pattern(), "
    "describe_prompt_cache_contract(), show_workflow_template(), "
    "validate_workflow_cache_contract(), and show_example_workflow() to read the real API instead of "
    "guessing at it. list_agenthicc_docs(), read_agenthicc_doc(), search_agenthicc_docs(), "
    "inspect_agenthicc_source(), and search_agenthicc_source() read the installed "
    "agenthicc source and documentation directly. These five exploratory tools are "
    "read-only; use them when the live source, schema, or documentation is needed "
    "instead of guessing."
)

_DESIGN_PROMPT: str = (
    "You are in the DESIGN phase of create_workflow. The user wants a NEW custom "
    "workflow. Your job in this phase is to design it — do not write any files yet.\n\n"
    "Work out, concretely: the workflow's lower_snake_case name; what it is for; the "
    "ordered phases; each phase's objective, agent_type, mode, and max_turns; and the "
    "transition graph (which phase follows which, and where a rejection loops back to). "
    "Explore only what you actually need — inspect the authoring API with the inspection "
    "tools, and read existing workflow files only if the request depends on them.\n\n"
    "The complete read-only self-inspection surface is "
    "list_agenthicc_docs(), read_agenthicc_doc(), search_agenthicc_docs(), "
    "inspect_agenthicc_source(), and search_agenthicc_source(). The effective-session "
    "catalog is authoritative for this run; the five source/documentation tools are "
    "exploratory and must be used only when their detail is needed.\n\n"
    "Your design MUST also state the workflow's own runner: the state enum members, the "
    "context dataclass fields, the checkpoint payload fields and memory reattachment rule, "
    "one method per state, and the transition tool that ends each phase. For every phase "
    "name the exact transition tool(s) available to its agent and state that only a "
    "successful call changes phase; prose such as 'done' never advances the workflow. Call "
    "describe_runner_pattern() and follow it. Inspect describe_prompt_cache_contract() "
    "and show_workflow_template() before writing the runner; use "
    "validate_workflow_cache_contract(path) after generation. Only if every phase is a single "
    "unconditional turn may you propose a declarative graph with no runner — and then say "
    "so explicitly and justify it.\n\n"
    "Present the design with request_design_approval(design, workflow_name). If it is not "
    "approved, revise and present it again. Once approved, call "
    "finalize_design(design, workflow_name).\n\n"
    "If the request is NOT about creating a new workflow — a question, an explanation "
    "request, or a task an existing workflow already covers — call "
    "exit_create_workflow(suggestion) immediately instead of designing anything.\n\n"
    + _AUTHORING_GUIDE
)
_DESIGN_REMINDER: str = (
    "You have not yet finalized the design. The user's request is in your system prompt. "
    "Return to it, complete or revise the workflow design, present it with "
    "request_design_approval(design, workflow_name), and once approved call "
    "finalize_design(design, workflow_name)."
)

_GENERATE_PROMPT: str = (
    "You are in the GENERATION phase of create_workflow. The approved design is in your "
    "system prompt. Write the complete workflow directory to the run-owned draft path in "
    "your system prompt with the write tools — "
    "do NOT re-design and do NOT ask for approval again.\n\n"
    "The package must import cleanly through its runner.py entry point: include the module docstring, "
    "'from __future__ import annotations', the imports it uses, and the full "
    "WorkflowPlugin subclass with every PhaseSpec from the approved design. Every "
    "next / on_reject value must name a phase that exists in the same workflow.\n\n"
    "Every phase's system_prompt_override must name its exact transition tool(s) and "
    "say that only a successful transition-tool call changes phase; prose never advances "
    "the workflow. Mark custom transition callables with the bare @tool_control imported "
    "from agenthicc.tools.capabilities (never @tool_control()) so the runtime can "
    "advertise them automatically. For a custom state-machine runner, this field is "
    "metadata only unless the phase method explicitly reads it; pass the authoritative "
    "phase prompt through run_phase(system_prompt=...). The generic inherited runner "
    "consumes system_prompt_override automatically.\n\n"
    "Add a module-level CACHE_CONTRACT string to runner.py and pass it as the "
    "stable_system_prompt argument on every CodePlanRunner.run_phase() call. It must "
    "include this policy verbatim in substance: ask the user a focused question through "
    "the existing ask_user tool whenever a missing or ambiguous requirement could change "
    "the result; wait for the answer and do not guess. The current question and answer "
    "are dynamic context, not part of CACHE_CONTRACT. Use the parent session's "
    "WorkflowConfig.conversation_id and injected session memory unchanged across every "
    "phase, retry, and resume; never create a second conversation.\n\n"
    "If the workflow depends on an optional integration, declare it on the plugin with "
    "required_integrations, optional_integrations, and/or integration_fallbacks. Use "
    "exact names such as 'cloakbrowser', 'playwright', 'mcp', or 'mcp:<server>'. Do not "
    "claim an unavailable integration is usable; a declared fallback must be a real "
    "degraded path that works without the missing service. If a required integration "
    "maps to a deferred session phase, also declare that exact phase in "
    "required_startup_phases; use config.wait_for_startup() for a phase-local gate, "
    "and never construct a second client, browser, or MCP manager.\n\n"
    "The generated runner must inherit WorkflowConfig.workspace_scope and "
    "WorkflowConfig.workspace_access unchanged. Every phase must call the public "
    "CodePlanRunner.run_phase() helper so the same live Safe/Plan/Yolo policy reaches "
    "all turns and subagents. Custom filesystem/Git/mention/command tools must use "
    "current_workspace_access() or an authorized built-in adapter; never construct a "
    "second scope, add an implicit parent root, parse around the policy, or use raw "
    "filesystem I/O for a convenience check.\n\n"
    "Implement the runtime lifecycle with the shared "
    "agenthicc.workflows.phase_lifecycle helpers. Centralize a publish_phase(context, "
    "phase_spec) operation that constructs a validated PhaseAnnotation and calls "
    "publish_phase_annotation() before every first turn, retry, and resume. Centralize "
    "checkpoint_boundary(context, completed_phase, next_state, outcome) and call "
    "checkpoint_phase_boundary() after the transition tool commits state/output and "
    "before the outer loop can publish or invoke the next phase. Include terminal "
    "boundaries. Propagate PhaseBoundaryError to the framework finalizer; do not "
    "swallow it. Inspect describe_phase_lifecycle() and "
    "show_phase_lifecycle_template() before coding.\n\n"
    "Write the runner the design specified into runner.py — the state enum, the "
    "context dataclass, one bounded async method per state, the "
    "'while not state.is_terminal' + 'match state' driver in run(), resume(), the phase "
    "tool factories, checkpoint_context_to_payload(), "
    "checkpoint_context_from_payload(payload, memory=None), and build_runner() returning "
    "it. Do not leave the runner or checkpoint codecs as a comment, "
    "a stub, or a TODO: write the working implementation. Use "
    "show_example_workflow() for a complete working runner to adapt, and "
    "inspect_agenthicc_source('agenthicc.workflows.code_plan.runner:CodePlanRunner') when "
    "you need the real signatures.\n\n"
    "WRITE THE PACKAGE IN CHUNKS. A workflow with its own runner is a few hundred lines, and "
    "one tool call carrying the whole file can exceed the response limit — if that happens "
    "the call is discarded and nothing reaches disk. So:\n"
    "  1. make_directory(path) for the exact run-owned draft directory; never write directly "
    "to the published .agenthicc/workflows directory;\n"
    "  2. write_file(path/runner.py, content) with the first chunk — the module docstring, the "
    "imports, and the state enum;\n"
    "  3. append_file(path/runner.py, content) for each following chunk — the context dataclass, the "
    "tool factories, the runner, then the plugin class;\n"
    "  4. write_file(path/tools.py, content) (and other sibling modules) for workflow-specific "
    "tools; keep imports relative to the package;\n"
    "  5. keep each chunk to roughly 60-80 lines, and split only between top-level "
    "definitions so no chunk cuts a statement in half;\n"
    "  6. read_file(path/runner.py) and the sibling files at the end to confirm the package "
    "is on disk and complete.\n"
    "Never re-write runner.py from the start after appending — that discards earlier chunks.\n\n"
    "The generated runner must use the WorkflowConfig.session_memory supplied by the "
    "session, including on resume; use WorkflowConfig.conversation_id unchanged for "
    "every phase and retry; never instantiate a fresh ShortTermMemory or a second "
    "conversation when an injected session object exists. The restore hook must attach "
    "its memory argument to the context.\n\n"
    "When the complete directory is on disk, call mark_generation_complete(summary, path) "
    "with the exact path you wrote to. Do not call it before the file is written."
)
_GENERATE_REMINDER: str = (
    "You have not yet called mark_generation_complete(summary, path). The approved design "
    "and target path are in your system prompt.\n\n"
    "If a previous write produced no result, the response was too long and was discarded — "
    "write less per call. Use read_file(path/runner.py) to see how much of the runner exists, "
    "then continue with append_file(path/runner.py, content) in chunks of roughly 60-80 lines from "
    "where it stops. Do not start the file over. Confirm every sibling tool module is also "
    "present. Call mark_generation_complete(summary, path) once the whole directory is on disk."
)

_VALIDATE_PROMPT: str = (
    "You are in the VALIDATION phase of create_workflow. The generated package has already "
    "been imported and checked automatically; the report is in your system prompt.\n\n"
    "Read the report first. If it lists ANY error, call reject_workflow(reason) naming the "
    "concrete fix — the generation phase will run again. If the report passes, read the "
    "generated package and confirm its phases and prompts match the approved design, then "
    "call approve_workflow(summary).\n\n"
    "You MUST call exactly one of these two tools."
)
_VALIDATE_REMINDER: str = (
    "You have not yet called approve_workflow() or reject_workflow(). The validation report "
    "and the approved design are in your system prompt. Decide now: reject_workflow(reason) "
    "if the report shows any error or the file does not match the design, otherwise "
    "approve_workflow(summary)."
)

_SUMMARIZE_PROMPT: str = (
    "You are in the SUMMARY phase of create_workflow. Write a short summary for the user: "
    "the name of the workflow that was created, the file it lives in, its phases, and how "
    "to run it — '/workflows reload' to pick it up in this session, then "
    "'/workflow <name>' to select it. Do not write any more files."
)


class CreateWorkflowRunner(BaseWorkflowRunner):
    """State-machine runner for the create_workflow meta-workflow.

    Parameters
    ----------
    config:
        WorkflowConfig holding all session-scoped singletons.
    mode_manager:
        ModeManager for per-phase mode overrides (None = headless).
    """

    #: Workflow name written to ``app_state.workflow_run``.
    workflow_name: str = "create_workflow"
    #: Total number of phases shown in the "N/M" status-bar counter.
    total_phases: int = 4

    # Per-phase model overrides.  Empty string = use global execution.model.
    # Override as class attributes in subclasses, or via TOML:
    #   [workflows.create_workflow]
    #   generate_model = "claude-opus-5"
    design_model: str = ""
    generate_model: str = ""
    validate_model: str = ""
    summary_model: str = ""

    def __init__(
        self,
        config: WorkflowConfig,
        mode_manager: ModeManager | None = None,
    ) -> None:
        self._cfg: WorkflowConfig = config
        self._mode_manager: ModeManager | None = mode_manager
        self._run_id: str = ""
        # Snapshots are immutable and are keyed by live tool/configuration
        # fingerprints.  Keeping this small cache on the runner avoids
        # repeating schema extraction for every retry while still naturally
        # invalidating when modes, MCP catalogs, browser selection, or tools
        # change.
        from agenthicc.workflows.create_workflow.catalog import (  # noqa: PLC0415
            AuthoringSnapshotCache,
        )

        self._authoring_snapshot_cache = AuthoringSnapshotCache()

        # Gap (lauren-ai): no public model_id accessor on AgentRunnerBase.
        _transport_cfg = getattr(getattr(config.agent_runner, "_transport", None), "_config", None)
        self._model_id: str = (
            getattr(_transport_cfg, "model", None) or config.cfg.execution.effective_model()
        )

        # Authoring budgets (previously inert config keys).  Both are clamped to
        # at least 1 so a hostile TOML value cannot skip a phase entirely.
        execution = config.cfg.execution
        self._max_attempts: int = max(1, int(execution.authoring_max_generation_attempts))
        self._max_phase_turns: int = max(1, int(execution.authoring_max_phase_turns))
        #: Bound on VALIDATE → GENERATE repair round-trips.
        self._max_repair_cycles: int = self._max_attempts

    # ── public entry points ───────────────────────────────────────────────────

    async def run(self, intent: str) -> CreateWorkflowContext:
        """Drive the full design → generate → validate → summarize state machine."""
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415

        from agenthicc.kernel import Event  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        session_memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        handle = self._cfg.workflow_handle
        run_id: str = handle.run_id if handle is not None else uuid.uuid4().hex
        self._run_id = run_id

        ctx: CreateWorkflowContext = CreateWorkflowContext(
            intent=intent,
            run_id=run_id,
            shared_memory=session_memory,
        )
        if handle is not None:
            handle.attach_context(ctx)
            # Record DESIGN before the first authoring turn. The phase helper
            # will increment the iteration when the phase actually begins.
            handle.update_phase("design", 0, 0)

        wf_run: WorkflowRun = WorkflowRun(
            run_id=run_id,
            workflow_name=self.workflow_name,
            intent=intent,
            current_phase="design",
            total_phases=self.total_phases,
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunStarted",
                {
                    "run_id": run_id,
                    "workflow_name": self.workflow_name,
                    "intent": intent,
                    "phase_names": list(_PHASE_NAMES),
                },
            )
        )

        state: CreateWorkflowState = CreateWorkflowState.DESIGN

        try:
            while not state.is_terminal:
                ctx.state = state
                phase_name: str = state.name.lower()
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=phase_name,
                    current_phase_index=_PHASE_INDEX.get(phase_name, 0),
                )
                self._cfg.app_state.workflow_run.set(wf_run)

                await self._cfg.processor.emit(
                    Event.create(
                        "WorkflowPhaseStarted",
                        {
                            "run_id": run_id,
                            "phase_name": phase_name,
                            "workflow_name": self.workflow_name,
                        },
                    )
                )

                state = await self._dispatch(state, ctx)
                ctx.state = state
                # A phase method returns only after its transition tool has
                # fired (or its bounded failure path has been selected).  The
                # boundary must be durable before the outer loop can enter a
                # further phase turn, including terminal outcomes.
                self._checkpoint_boundary(ctx, phase_name, state)

                next_label: str | None = state.name.lower() if not state.is_terminal else None
                artifact = ctx.artifacts.get(phase_name)
                await self._cfg.processor.emit(
                    Event.create(
                        "WorkflowPhaseCompleted",
                        {
                            "run_id": run_id,
                            "phase_name": phase_name,
                            "role": "auto",
                            "full_text": artifact.content if artifact is not None else "",
                            "approved": None,
                            "structured": {},
                            "edge_label": next_label,
                            "metadata": {
                                "command_outcomes": list(ctx.command_outcomes),
                                "artifact_kind": artifact.kind if artifact is not None else "",
                            },
                        },
                    )
                )
                self._cfg.app_state.workflow_run.set(wf_run)
                log.info("create_workflow: %s → %s", phase_name, state.name)

            final_status: str = self._final_status(state)
            wf_run = dataclasses.replace(wf_run, status=final_status, current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)

            if state == CreateWorkflowState.FAILED and ctx.fail_reason:
                self._cfg.conv_store.append_event(
                    "error", {"message": f"create_workflow failed: {ctx.fail_reason}"}
                )

            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunCompleted",
                    {
                        "run_id": run_id,
                        "workflow_name": self.workflow_name,
                        "phases_run": len(wf_run.phase_history),
                        "status": final_status,
                    },
                )
            )

        except (asyncio.CancelledError, KeyboardInterrupt):
            if handle is not None and handle.is_pause_requested():
                wf_run = dataclasses.replace(wf_run, status="paused")
                handle.attach_context(ctx)
            else:
                wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        except PhaseBoundaryError:
            # A UI projection is not durable progress.  Let the session-owned
            # failure finalizer classify and persist the checkpoint failure.
            raise
        except Exception as exc:
            log.error("CreateWorkflowRunner error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            self._cfg.conv_store.append_event("error", {"message": str(exc)})

        if handle is not None:
            if wf_run.status in {"complete", "exited"}:
                handle.mark_terminal("complete")
                if handle.checkpoint_supported:
                    handle.save_checkpoint(reason=wf_run.status)
        return ctx

    async def resume(self, context: object) -> CreateWorkflowContext:
        """Resume a typed checkpoint context or a legacy generic context."""
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowContext  # noqa: PLC0415

        handle = self._cfg.workflow_handle
        session_memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        if isinstance(context, CreateWorkflowContext):
            ctx = context
            ctx.shared_memory = session_memory
            state = ctx.state
        elif isinstance(context, WorkflowContext):
            # A generic context contains phase outputs but not the state-machine
            # cursor that makes recovery safe. Restarting from DESIGN here would
            # duplicate writes and silently discard the durable continuation.
            # Old callers must explicitly start a new run instead.
            raise TypeError(
                "create_workflow resume requires CreateWorkflowContext; "
                "the legacy generic context has no recoverable phase state"
            )
        else:
            raise TypeError("workflow resume requires a WorkflowContext")

        self._run_id = ctx.run_id
        self._reconcile_resume_cursor(ctx)
        state = ctx.state
        if handle is not None:
            handle.attach_context(ctx)
        # Re-enter the same outer dispatch loop. The typed state is the durable
        # continuation point; no resume path silently restarts at DESIGN.
        from agenthicc.kernel import Event  # noqa: PLC0415
        from agenthicc.workflows.plugin import WorkflowRun  # noqa: PLC0415

        wf_run = WorkflowRun(
            run_id=ctx.run_id,
            workflow_name=self.workflow_name,
            intent=ctx.intent,
            current_phase=state.name.lower() if not state.is_terminal else None,
            total_phases=self.total_phases,
        )
        self._cfg.app_state.workflow_run.set(wf_run)
        try:
            while not state.is_terminal:
                ctx.state = state
                phase_name = state.name.lower()
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=phase_name,
                    current_phase_index=_PHASE_INDEX.get(phase_name, 0),
                )
                self._cfg.app_state.workflow_run.set(wf_run)
                await self._cfg.processor.emit(
                    Event.create(
                        "WorkflowPhaseStarted",
                        {
                            "run_id": ctx.run_id,
                            "phase_name": phase_name,
                            "workflow_name": self.workflow_name,
                        },
                    )
                )
                state = await self._dispatch(state, ctx)
                ctx.state = state
                self._checkpoint_boundary(ctx, phase_name, state)

            final_status = self._final_status(state)
            wf_run = dataclasses.replace(wf_run, status=final_status, current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunCompleted",
                    {
                        "run_id": ctx.run_id,
                        "workflow_name": self.workflow_name,
                        "phases_run": len(wf_run.phase_history),
                        "status": final_status,
                    },
                )
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            if handle is not None and handle.is_pause_requested():
                wf_run = dataclasses.replace(wf_run, status="paused")
                handle.attach_context(ctx)
            else:
                wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        if handle is not None:
            if final_status in {"complete", "exited"}:
                handle.mark_terminal("complete")
            if final_status in {"complete", "exited"} and handle.checkpoint_supported:
                handle.save_checkpoint(reason=final_status)
        return ctx

    def _reconcile_resume_cursor(self, ctx: CreateWorkflowContext) -> None:
        """Resolve the saved state before the first resumed phase prompt.

        ``create_workflow`` has no external artifact manifest, so its typed
        context is the durable execution evidence.  The same pure resolver
        used by reconstruct_site still matters here: it prevents an older
        checkpoint cursor from being allowed to replay a phase when the
        context already contains a contiguous completed prefix.  A validation
        rejection is an intentional re-entry and therefore preserves its
        repair cursor rather than treating the successful validation attempt
        as a linear prefix.
        """
        current = ctx.state.name.lower()
        if ctx.state.is_terminal:
            ctx.resume_resolution_source = "checkpoint_cursor"
            return
        boundary = ctx.last_boundary
        preserve_current = (
            isinstance(boundary, dict)
            and str(boundary.get("next_phase", "")).strip().lower() == current
            and str(boundary.get("outcome", "")).strip().lower() in {"rejected", "retry"}
        )
        journal_phases: tuple[str, ...] = ()
        handle = self._cfg.workflow_handle
        if handle is not None:
            try:
                conversation = object.__getattribute__(handle, "conversation")
                journal = object.__getattribute__(conversation, "journal")
                fold_boundaries = object.__getattribute__(journal, "fold_workflow_phase_boundaries")
                records = fold_boundaries(ctx.run_id, self.workflow_name)
                journal_phases = tuple(
                    str(item["completed_phase"])
                    for item in records
                    if isinstance(item.get("completed_phase"), str)
                )
            except (AttributeError, TypeError):
                # Lightweight/headless adapters may not expose the optional
                # journal index. The typed checkpoint remains authoritative.
                journal_phases = ()
        resolution = reconcile_phase_cursor(
            _PHASE_NAMES,
            current,
            completed_phases=ctx.completed_phases,
            journal_phases=journal_phases,
            terminal_phase="complete",
            preserve_current=preserve_current,
        )
        ctx.resume_resolution_source = resolution.source
        ctx.resume_resolution_reason = resolution.diagnostic[:512]
        ctx.resume_reconciled = resolution.reconciled
        for phase_name in resolution.completed_phases:
            if phase_name not in ctx.completed_phases:
                ctx.completed_phases.append(phase_name)
        if resolution.phase_name == "complete":
            resolved = CreateWorkflowState.COMPLETE
        else:
            resolved = CreateWorkflowState[resolution.phase_name.upper()]
        if resolved is ctx.state:
            return
        ctx.state = resolved
        ctx.last_boundary = {
            **ctx.last_boundary,
            "next_state": resolved.name,
            "next_phase": None if resolved.is_terminal else resolved.name.lower(),
            "outcome": "resume_reconciled",
        }
        if self._cfg.workflow_handle is not None:
            self._cfg.workflow_handle.attach_context(ctx)
            self._cfg.workflow_handle.update_phase(
                None if resolved.is_terminal else resolution.phase_name,
                resolution.phase_index,
                ctx.phase_iteration,
                persist=False,
            )
            self._cfg.workflow_handle.save_checkpoint(reason="resume_reconciled")

    # ── outer-loop dispatch ───────────────────────────────────────────────────

    async def _dispatch(
        self,
        state: CreateWorkflowState,
        ctx: CreateWorkflowContext,
    ) -> CreateWorkflowState:
        """Run the phase method for *state* and return the next state."""
        match state:
            case CreateWorkflowState.DESIGN:
                return await self._design(ctx)
            case CreateWorkflowState.GENERATE:
                return await self._generate(ctx)
            case CreateWorkflowState.VALIDATE:
                return await self._validate(ctx)
            case CreateWorkflowState.SUMMARIZE:
                return await self._summarize(ctx)
            case _:
                return state

    @staticmethod
    def _final_status(state: CreateWorkflowState) -> str:
        """Map a terminal state onto the ``WorkflowRun.status`` string."""
        if state == CreateWorkflowState.COMPLETE:
            return "complete"
        if state == CreateWorkflowState.EXITED:
            return "exited"
        return "failed"

    # ── phase methods ─────────────────────────────────────────────────────────

    async def _design(self, ctx: CreateWorkflowContext) -> CreateWorkflowState:
        """Loop until finalize_design() or exit_create_workflow() fires.

        Returns GENERATE, EXITED, or FAILED.
        """
        from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415
        from agenthicc.workflows.create_workflow.inspection_tools import (  # noqa: PLC0415
            make_inspection_tools,
        )
        from agenthicc.workflows.create_workflow.phase_tools import (  # noqa: PLC0415
            make_design_tools,
        )

        self._set_phase("design", _PHASE_INDEX["design"], ctx)
        ctx.command_outcomes.clear()

        system_prompt: str = _DESIGN_PROMPT + f"\n\n[USER REQUEST]\n{ctx.intent}"
        exit_event: asyncio.Event = asyncio.Event()

        for attempt in range(1, self._max_attempts + 1):
            design_event: asyncio.Event = asyncio.Event()
            design_data: dict[str, object] = {}

            tools: _ToolList = list(self._base_tools())
            # Build the snapshot from the complete phase surface.  The two
            # snapshot-query tools themselves are appended afterward because
            # their closures need the immutable snapshot they report; the
            # turn-level snapshot is rebuilt by _run_turn with those tools.
            tools.extend(make_inspection_tools())
            tools.extend(
                make_design_tools(
                    self._cfg.approval_svc,
                    design_event,
                    design_data,
                    exit_event=exit_event,
                )
            )
            tools.extend(make_questions_tool(self._cfg.approval_svc, ctx.question_metadata))
            authoring_snapshot = self._authoring_snapshot(
                phase_name="design",
                phase_role="auto",
                tools=tools,
            )
            tools.extend(make_inspection_tools(snapshot=authoring_snapshot)[-2:])
            text: str = ctx.intent if attempt == 1 else _DESIGN_REMINDER

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode=None,
                    system_prompt=system_prompt,
                    max_turns=self._max_phase_turns,
                    ctx=ctx,
                    phase_name="design",
                    model_override=self._phase_model("design"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_design permanent error on attempt %d: %s", attempt, exc)
                return CreateWorkflowState.FAILED

            # Exit takes priority — check before design finalization.
            if exit_event.is_set():
                suggestion = design_data.get("suggestion", "")
                ctx.suggestion = suggestion if isinstance(suggestion, str) else ""
                ctx.add_artifact(
                    PhaseArtifact(
                        phase="design",
                        kind="exit",
                        content=ctx.suggestion,
                        metadata={"attempts": attempt},
                    )
                )
                return CreateWorkflowState.EXITED

            if design_event.is_set() and "design" in design_data:
                design = design_data.get("design", "")
                name = design_data.get("workflow_name", "")
                ctx.design = design if isinstance(design, str) else ""
                ctx.workflow_name = name if isinstance(name, str) else ""
                ctx.add_artifact(
                    PhaseArtifact(
                        phase="design",
                        kind="design",
                        content=ctx.design,
                        metadata={"workflow_name": ctx.workflow_name, "attempts": attempt},
                    )
                )
                return CreateWorkflowState.GENERATE

        ctx.fail_reason = (
            f"Design phase exhausted {self._max_attempts} attempts without calling "
            "finalize_design()."
        )
        return CreateWorkflowState.FAILED

    async def _generate(self, ctx: CreateWorkflowContext) -> CreateWorkflowState:
        """Loop until mark_generation_complete() fires; return VALIDATE or FAILED."""
        from agenthicc.workflows.create_workflow.phase_tools import (  # noqa: PLC0415
            make_generation_tools,
        )
        from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415
        from agenthicc.workflows.create_workflow.draft import (  # noqa: PLC0415
            DraftError,
            reset_draft,
            scan_draft,
            stage_legacy_package,
        )

        self._set_phase("generate", _PHASE_INDEX["generate"], ctx)
        ctx.command_outcomes.clear()

        target_path: str = self._draft_path(ctx.workflow_name, ctx.run_id)
        if ctx.rejection_reason:
            # A validation rejection starts a new generation attempt, but the
            # attempt remains part of this run.  Clear only the exact run-owned
            # draft so stale helper modules cannot leak into the next manifest.
            try:
                reset_draft(
                    Path(target_path),
                    root=self._workspace_root(),
                    run_id=ctx.run_id or self._run_id or "pending",
                    workflow_name=ctx.workflow_name,
                )
            except (DraftError, OSError, ValueError) as exc:
                ctx.fail_reason = (
                    f"Cannot reset rejected workflow draft: {type(exc).__name__}: {exc}"
                )
                return CreateWorkflowState.FAILED
        system_prompt: str = (
            _GENERATE_PROMPT
            + f"\n\n[USER REQUEST]\n{ctx.intent}"
            + f"\n\n[APPROVED DESIGN]\n{ctx.design}"
            + f"\n\n[WORKFLOW NAME]\n{ctx.workflow_name}"
            + f"\n\n[TARGET PATH]\n{target_path}"
            + f"\n\n[PUBLISHED PATH — DO NOT WRITE HERE]\n{self._target_path(ctx.workflow_name)}"
        )
        if ctx.rejection_reason:
            system_prompt += (
                f"\n\n[VALIDATION REJECTED THE PREVIOUS ATTEMPT]\n{ctx.rejection_reason}"
            )
        if ctx.validation_report:
            system_prompt += f"\n\n{ctx.validation_report}"

        first_text: str = (
            f"Write the approved {ctx.workflow_name} workflow to {target_path}. "
            f"The published destination is {self._target_path(ctx.workflow_name)}; "
            "do not write there directly."
            if not ctx.rejection_reason
            else (
                f"Fix and rewrite {ctx.generated_path or target_path}. "
                f"What must change: {ctx.rejection_reason}"
            )
        )

        for attempt in range(1, self._max_attempts + 1):
            ctx.command_outcomes.clear()
            generate_event: asyncio.Event = asyncio.Event()
            generate_data: dict[str, object] = {}

            from agenthicc.workflows.create_workflow.inspection_tools import (  # noqa: PLC0415
                make_inspection_tools,
            )

            tools: _ToolList = list(self._base_tools())
            tools.extend(make_inspection_tools())
            tools.extend(make_generation_tools(generate_event, generate_data))
            tools.extend(make_questions_tool(self._cfg.approval_svc, ctx.question_metadata))
            authoring_snapshot = self._authoring_snapshot(
                phase_name="generate",
                phase_role="auto",
                tools=tools,
            )
            tools.extend(make_inspection_tools(snapshot=authoring_snapshot)[-2:])
            text: str = first_text if attempt == 1 else _GENERATE_REMINDER

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode="Yolo",
                    system_prompt=system_prompt,
                    max_turns=self._max_phase_turns,
                    ctx=ctx,
                    phase_name="generate",
                    model_override=self._phase_model("generate"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_generate permanent error on attempt %d: %s", attempt, exc)
                return CreateWorkflowState.FAILED

            if generate_event.is_set():
                path = generate_data.get("path", "")
                summary = generate_data.get("summary", "")
                reported_path = path if isinstance(path, str) else ""
                ctx.generation_summary = summary if isinstance(summary, str) else ""
                manifest = None
                draft_path = self._draft_path(ctx.workflow_name, ctx.run_id)
                try:
                    root = self._workspace_root()
                    run_id = ctx.run_id or self._run_id or "pending"
                    reported = Path(reported_path).expanduser()
                    if not reported.is_absolute():
                        reported = root / reported
                    if reported.exists() and reported.resolve() != Path(draft_path).resolve():
                        # Keep compatibility with older callers that reported
                        # a direct published path, while making the draft the
                        # only path that later phases can consume.
                        stage_legacy_package(
                            reported,
                            destination=Path(draft_path),
                            root=root,
                        )
                    if Path(draft_path).is_dir():
                        manifest = scan_draft(
                            Path(draft_path),
                            root=root,
                            run_id=run_id,
                            workflow_name=ctx.workflow_name,
                        )
                except (DraftError, OSError, ValueError) as exc:
                    # Leave the reported path intact so VALIDATE can show the
                    # normal workspace/missing-file diagnostic and let the
                    # bounded repair loop decide what happens next.
                    log.warning("_generate draft staging deferred to validation: %s", exc)

                ctx.generated_path = str(draft_path) if manifest is not None else reported_path
                if manifest is not None:
                    ctx.draft_manifest = manifest.to_dict()
                    ctx.draft_fingerprint = manifest.fingerprint
                ctx.add_artifact(
                    PhaseArtifact(
                        phase="generate",
                        kind="workflow_file",
                        content=ctx.generation_summary,
                        metadata={
                            "path": reported_path or ctx.generated_path,
                            "draft_path": ctx.generated_path,
                            "reported_path": reported_path,
                            "draft_fingerprint": ctx.draft_fingerprint,
                            "file_count": len(manifest.files) if manifest is not None else 0,
                            "workflow_name": ctx.workflow_name,
                            "attempts": attempt,
                            "repair_cycle": ctx.repair_cycles,
                        },
                    )
                )
                return CreateWorkflowState.VALIDATE

        ctx.fail_reason = (
            f"Generation phase exhausted {self._max_attempts} attempts without calling "
            "mark_generation_complete()."
        )
        return CreateWorkflowState.FAILED

    async def _validate(self, ctx: CreateWorkflowContext) -> CreateWorkflowState:
        """Check the generated package, then loop until the agent votes.

        Returns SUMMARIZE (approved and the deterministic check passed), GENERATE
        (rejected, or approved against a failing check), or FAILED.
        """
        from agenthicc.workflows.create_workflow.phase_tools import (  # noqa: PLC0415
            make_validation_tools,
        )
        from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415
        from agenthicc.workflows.create_workflow.validation import (  # noqa: PLC0415
            validate_workflow_file,
        )
        from agenthicc.workflows.create_workflow.draft import (  # noqa: PLC0415
            DraftError,
            build_draft_path,
            publish_draft,
            scan_draft,
            stage_legacy_package,
        )
        from agenthicc.workflows.create_workflow.smoke import (  # noqa: PLC0415
            run_generated_workflow_smoke,
        )

        self._set_phase("validate", _PHASE_INDEX["validate"], ctx)
        ctx.command_outcomes.clear()

        # Keep publication provenance complete even for embedders that replace
        # _run_turn with a deterministic test/headless adapter.  The normal
        # turn path records a richer snapshot immediately before the provider
        # call; this fallback still records the live, redacted session contract.
        if not ctx.authoring_snapshot:
            snapshot = self._authoring_snapshot(
                phase_name="validate",
                phase_role="auto",
                tools=self._base_tools(),
            )
            ctx.authoring_snapshot = snapshot.checkpoint_reference()
            ctx.selected_tools = [entry.name for entry in snapshot.tools if entry.available]
            ctx.dependency_summary = {
                "browser": dict(snapshot.browser),
                "mcp": [dict(item) for item in snapshot.mcp],
                "unavailable_optional": [dict(item) for item in snapshot.unavailable],
            }

        manifest: DraftManifest | None = None
        try:
            root = self._workspace_root()
            run_id = ctx.run_id or self._run_id or "pending"
            expected_draft = build_draft_path(root, run_id, ctx.workflow_name)
            candidate: Path | None = None
            if ctx.generated_path.strip():
                candidate = Path(ctx.generated_path).expanduser()
                if not candidate.is_absolute():
                    candidate = root / candidate
            if (
                candidate is not None
                and candidate.exists()
                and candidate.resolve() != expected_draft.resolve()
            ):
                stage_legacy_package(candidate, destination=expected_draft, root=root)
            if expected_draft.is_dir():
                manifest = scan_draft(
                    expected_draft,
                    root=root,
                    run_id=run_id,
                    workflow_name=ctx.workflow_name,
                )
                ctx.generated_path = str(expected_draft)
                ctx.draft_manifest = manifest.to_dict()
                ctx.draft_fingerprint = manifest.fingerprint
            report: ValidationReport = validate_workflow_file(
                ctx.generated_path,
                expected_name=ctx.workflow_name,
                root=root,
                strict_cache_contract=True,
                available_integrations=ctx.dependency_summary,
            )
        except (DraftError, OSError, ValueError) as exc:
            report = validate_workflow_file(
                ctx.generated_path,
                expected_name=ctx.workflow_name,
                root=self._workspace_root(),
                strict_cache_contract=True,
                available_integrations=ctx.dependency_summary,
            )
            report = dataclasses.replace(
                report,
                ok=False,
                errors=(*report.errors, f"Draft manifest rejected: {type(exc).__name__}: {exc}"),
                categories={**report.categories, "manifest": "fail", "result": "fail"},
            )

        smoke = run_generated_workflow_smoke(
            ctx.generated_path,
            expected_name=ctx.workflow_name,
            root=self._workspace_root(),
        )
        if not smoke.ok:
            report = dataclasses.replace(
                report,
                ok=False,
                errors=(*report.errors, *smoke.errors),
                categories={**report.categories, "smoke": "fail", "result": "fail"},
            )
        else:
            report = dataclasses.replace(
                report,
                categories={**report.categories, "smoke": "pass"},
            )
        ctx.validation_evidence = {
            "categories": dict(report.categories),
            "evidence": dict(report.evidence),
            "errors": list(report.errors),
            "warnings": list(report.warnings),
            "smoke": smoke.to_dict(),
            "draft_fingerprint": ctx.draft_fingerprint,
        }
        ctx.validation_evidence["evidence_id"] = _evidence_fingerprint(ctx.validation_evidence)
        ctx.validation_report = report.render()
        ctx.add_artifact(
            PhaseArtifact(
                phase="validate",
                kind="validation_report",
                content=ctx.validation_report,
                metadata={
                    "ok": report.ok,
                    "path": report.path,
                    "errors": list(report.errors),
                    "warnings": list(report.warnings),
                    "plugin_names": list(report.plugin_names),
                    "phase_names": list(report.phase_names),
                    "categories": dict(report.categories),
                    "evidence": dict(report.evidence),
                    "smoke": smoke.to_dict(),
                },
            )
        )

        system_prompt: str = (
            _VALIDATE_PROMPT
            + f"\n\n[USER REQUEST]\n{ctx.intent}"
            + f"\n\n[APPROVED DESIGN]\n{ctx.design}"
            + f"\n\n{ctx.validation_report}"
        )

        for attempt in range(1, self._max_attempts + 1):
            validate_event: asyncio.Event = asyncio.Event()
            validate_data: dict[str, object] = {}

            tools: _ToolList = list(self._base_tools()) + list(
                make_validation_tools(validate_event, validate_data)
            )
            tools.extend(make_questions_tool(self._cfg.approval_svc, ctx.question_metadata))
            text: str = (
                (
                    f"The generated workflow is at {report.path or ctx.generated_path}. "
                    f"Deterministic validation says: {'PASS' if report.ok else 'FAIL'}. "
                    "Decide with approve_workflow() or reject_workflow()."
                )
                if attempt == 1
                else _VALIDATE_REMINDER
            )

            try:
                await self._run_turn(
                    text,
                    tools=tools,
                    mode=None,
                    system_prompt=system_prompt,
                    max_turns=self._max_phase_turns,
                    ctx=ctx,
                    phase_name="validate",
                    model_override=self._phase_model("validate"),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                ctx.fail_reason = f"{type(exc).__name__}: {exc}"
                log.error("_validate permanent error on attempt %d: %s", attempt, exc)
                return CreateWorkflowState.FAILED

            if not validate_event.is_set():
                continue

            action_value = validate_data.get("action", "reject")
            action: str = action_value if isinstance(action_value, str) else "reject"

            if action == "approve" and report.ok:
                summary = validate_data.get("summary", "")
                ctx.validation_summary = summary if isinstance(summary, str) else ""
                ctx.rejection_reason = ""
                if manifest is None:
                    ctx.fail_reason = "Validated workflow has no publishable draft manifest."
                    ctx.publication = {
                        "status": "failed",
                        "error": ctx.fail_reason,
                        "draft_path": ctx.generated_path,
                        "catalog_snapshot": dict(ctx.authoring_snapshot),
                        "validation_evidence_id": str(
                            ctx.validation_evidence.get("evidence_id", "")
                        ),
                    }
                    return CreateWorkflowState.FAILED
                try:
                    publication = publish_draft(
                        manifest,
                        root=self._workspace_root(),
                        published_name=ctx.workflow_name,
                    )
                except (DraftError, OSError, ValueError) as exc:
                    ctx.fail_reason = (
                        f"Validated workflow could not be published: {type(exc).__name__}: {exc}"
                    )
                    ctx.publication = {
                        "status": "failed",
                        "error": ctx.fail_reason,
                        "draft_path": ctx.generated_path,
                        "catalog_snapshot": dict(ctx.authoring_snapshot),
                        "validation_evidence_id": str(
                            ctx.validation_evidence.get("evidence_id", "")
                        ),
                    }
                    return CreateWorkflowState.FAILED
                publication_data = publication.to_dict()
                raw_categories = ctx.validation_evidence.get("categories", {})
                validation_categories = (
                    dict(raw_categories) if isinstance(raw_categories, dict) else {}
                )
                publication_data.update(
                    {
                        "catalog_snapshot": dict(ctx.authoring_snapshot),
                        "catalog_snapshot_id": str(ctx.authoring_snapshot.get("snapshot_id", "")),
                        "catalog_version": str(ctx.authoring_snapshot.get("catalog_version", "")),
                        "validation_evidence_id": str(
                            ctx.validation_evidence.get("evidence_id", "")
                        ),
                        "validation_categories": validation_categories,
                    }
                )
                ctx.publication = publication_data
                validation_artifact = ctx.artifacts.get("validate")
                if validation_artifact is not None:
                    validation_artifact.metadata["publication"] = dict(publication_data)
                # The context points at the canonical package that the normal
                # loader discovers. Legacy reported paths remain in the
                # generation artifact and publication evidence.
                ctx.generated_path = publication.published_path
                return CreateWorkflowState.SUMMARIZE

            if action == "approve":
                # Ground truth wins: the agent approved a file that does not load.
                reason = (
                    "Deterministic validation failed, so the approval was overridden. "
                    + " ".join(report.errors)
                )
                log.warning("create_workflow: approval overridden by failing validation")
            else:
                raw_reason = validate_data.get("reason", "")
                reason = raw_reason if isinstance(raw_reason, str) else ""
                if not report.ok:
                    reason = f"{reason} {' '.join(report.errors)}".strip()

            return self._route_repair(ctx, reason)

        ctx.fail_reason = (
            f"Validation phase exhausted {self._max_attempts} attempts without calling "
            "approve_workflow() or reject_workflow()."
        )
        return CreateWorkflowState.FAILED

    async def _summarize(self, ctx: CreateWorkflowContext) -> CreateWorkflowState:
        """Single turn; always returns COMPLETE."""
        from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415

        self._set_phase("summarize", _PHASE_INDEX["summarize"], ctx)
        ctx.command_outcomes.clear()

        text: str = (
            f"Request: {ctx.intent}\n\n"
            f"Workflow created: {ctx.workflow_name or '(unnamed)'}\n"
            f"Directory: {ctx.publication.get('published_path', ctx.generated_path) or '(unknown)'}\n"
            f"What was generated: {ctx.generation_summary or '(see conversation)'}\n"
            f"Validation verdict: {ctx.validation_summary or 'approved'}"
        )
        try:
            tools = list(self._base_tools())
            tools.extend(make_questions_tool(self._cfg.approval_svc, ctx.question_metadata))
            await self._run_turn(
                text,
                tools=tools,
                mode=None,
                system_prompt=_SUMMARIZE_PROMPT + f"\n\n[USER REQUEST]\n{ctx.intent}",
                max_turns=min(4, self._max_phase_turns),
                ctx=ctx,
                phase_name="summarize",
                model_override=self._phase_model("summarize"),
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("_summarize error: %s", exc)

        ctx.add_artifact(
            PhaseArtifact(
                phase="summarize",
                kind="summary",
                content=ctx.validation_summary or ctx.generation_summary,
                metadata={
                    "workflow_name": ctx.workflow_name,
                    "path": ctx.publication.get("published_path", ctx.generated_path),
                    "publication": dict(ctx.publication),
                    "repair_cycles": ctx.repair_cycles,
                },
            )
        )
        return CreateWorkflowState.COMPLETE

    # ── phase helpers ─────────────────────────────────────────────────────────

    def _route_repair(self, ctx: CreateWorkflowContext, reason: str) -> CreateWorkflowState:
        """Send the run back to GENERATE, or FAIL when the repair budget is spent."""
        ctx.repair_cycles += 1
        ctx.rejection_reason = reason.strip()
        if ctx.repair_cycles > self._max_repair_cycles:
            ctx.fail_reason = (
                f"Validation rejected the generated workflow {ctx.repair_cycles} times "
                f"(limit {self._max_repair_cycles}). Last reason: {ctx.rejection_reason}"
            )
            return CreateWorkflowState.FAILED
        return CreateWorkflowState.GENERATE

    def _workspace_root(self) -> Path:
        """Return the directory generated workflow files must live inside."""
        scope = self._cfg.workspace_scope
        return scope.primary_root if scope is not None else Path.cwd()

    def _target_path(self, workflow_name: str) -> str:
        """Return the conventional project-local path for *workflow_name*."""
        return f"{_WORKFLOW_DIR}/{workflow_name or 'my_workflow'}"

    def _draft_path(self, workflow_name: str, run_id: str) -> str:
        """Return the isolated run-owned generation path for a workflow."""
        from agenthicc.workflows.create_workflow.draft import build_draft_path  # noqa: PLC0415

        return str(
            build_draft_path(
                self._workspace_root(),
                run_id or self._run_id or "pending",
                workflow_name or "my_workflow",
            )
        )

    def _phase_model(self, phase_name: str) -> str:
        """Return the model override for *phase_name*, or '' for the global default.

        Priority: ``WorkflowParams.model_for_phase()`` (TOML/CLI) → the static
        class attribute → empty string.
        """
        if self._cfg.params is not None:
            configured = self._cfg.params.model_for_phase(phase_name, "")
            if configured:
                return configured
        attr = _PHASE_MODEL_ATTR.get(phase_name, "")
        if not attr:
            return ""
        value = getattr(self, attr, "")
        return value if isinstance(value, str) else ""

    def _authoring_snapshot(
        self,
        *,
        phase_name: str,
        phase_role: str,
        phase_capabilities: Iterable[object] | None = None,
        tools: _ToolList,
    ) -> AuthoringSnapshot:
        """Return the cached effective catalog for one authoring turn."""
        return self._authoring_snapshot_cache.get_or_build(
            self._cfg,
            phase_name=phase_name,
            phase_role=phase_role,
            phase_capabilities=phase_capabilities,
            tools=tools,
        )

    def _set_phase(self, phase_name: str, phase_index: int, ctx: CreateWorkflowContext) -> None:
        """Update all workflow TUI state for the current phase in one call."""
        ctx.state = CreateWorkflowState[phase_name.upper()]
        ctx.phase_iteration += 1
        ctx.phase_attempts[phase_name] = ctx.phase_attempts.get(phase_name, 0) + 1
        publish_phase_annotation(
            self._cfg,
            PhaseAnnotation(
                workflow_name=self.workflow_name,
                phase_name=phase_name,
                phase_index=phase_index,
                total_phases=self.total_phases,
                run_id=ctx.run_id,
                intent=ctx.intent,
                model_id=self._phase_model(phase_name) or self._model_id,
                phase_iteration=ctx.phase_iteration,
                phase_attempt=ctx.phase_attempts[phase_name],
                plan_version=ctx.plan_version,
            ),
            ctx,
        )

    def _checkpoint_boundary(
        self,
        ctx: CreateWorkflowContext,
        completed_phase: str,
        next_state: CreateWorkflowState,
    ) -> None:
        """Commit one phase result before the outer loop advances."""
        next_phase = None if next_state.is_terminal else next_state.name.lower()
        next_index = (
            _PHASE_INDEX.get(next_phase, _PHASE_INDEX.get(completed_phase, 0))
            if next_phase is not None
            else _PHASE_INDEX.get(completed_phase, 0)
        )
        outcome = "failed" if next_state is CreateWorkflowState.FAILED else "completed"
        if next_state.is_terminal and next_state is not CreateWorkflowState.FAILED:
            outcome = "terminal"
        elif ctx.rejection_reason and next_state is CreateWorkflowState.GENERATE:
            outcome = "rejected"
        existing = ctx.last_boundary
        boundary_key = "|".join(
            (
                completed_phase[:128],
                next_phase[:128] if next_phase is not None else "",
                str(next_index),
                str(ctx.phase_iteration),
                outcome,
            )
        )
        if existing.get("boundary_key") == boundary_key and existing.get("durable") is True:
            return
        ctx.last_boundary = {
            "completed_phase": completed_phase,
            "next_state": next_state.name,
            "next_phase": next_phase,
            "outcome": outcome,
            "phase_iteration": ctx.phase_iteration,
            "boundary_key": boundary_key,
            "durable": False,
        }
        if completed_phase not in ctx.completed_phases:
            ctx.completed_phases.append(completed_phase)
        ctx.phase_history.append(dict(ctx.last_boundary))
        checkpoint_phase_boundary(
            self._cfg,
            ctx,
            completed_phase=completed_phase,
            next_phase=next_phase,
            phase_index=next_index,
            phase_iteration=ctx.phase_iteration,
            outcome=outcome,
        )

    # ── turn helpers ──────────────────────────────────────────────────────────

    async def _run_turn(
        self,
        text: str,
        *,
        tools: _ToolList,
        mode: str | None,
        system_prompt: str,
        max_turns: int,
        ctx: CreateWorkflowContext,
        phase_name: str = "",
        model_override: str = "",
    ) -> None:
        """Run one agent turn, optionally switching mode for its duration.

        When *model_override* is non-empty a modified copy of ``exec_cfg`` is built
        with ``model=model_override`` so the per-phase model is picked up.
        """
        from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415
        from agenthicc.workflows.plugin import (  # noqa: PLC0415
            _WORKFLOW_USER_QUESTION_REMINDER,
            phase_transition_instruction,
        )
        from agenthicc.runners.prompt_contract import (  # noqa: PLC0415
            build_workflow_prompt_contract,
            tool_name,
        )
        from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities  # noqa: PLC0415

        original_mode = self._cfg.app_state.active_mode()
        if mode is not None and self._mode_manager is not None:
            # A phase override is executable configuration: never fall back to
            # the caller's mode when the declaration is unknown or internal.
            override_name = self._mode_manager.resolve_name(mode)
            self._mode_manager.set_by_name(override_name)

        _base_exec = self._cfg.cfg.execution
        exec_cfg = (
            dataclasses.replace(_base_exec, model=model_override)
            if model_override and dataclasses.is_dataclass(_base_exec)
            else _base_exec
        )

        if self._cfg.approval_svc is not None and ctx.shared_memory is not None:
            ctx.shared_memory.ensure_valid()

        from agenthicc.background.terminals import (  # noqa: PLC0415
            reset_current_terminal_wait_policy,
            set_current_terminal_wait_policy,
        )

        policy = self._cfg.terminal_wait_policies.get(phase_name, "foreground")
        policy_token = set_current_terminal_wait_policy(policy)
        try:
            stable_tools: _ToolList = []
            phase_tools: _ToolList = []
            for tool in tools:
                is_transition = tool_name(
                    tool
                ) != "ask_user" and ToolCapability.CONTROL in get_tool_capabilities(tool)
                (phase_tools if is_transition else stable_tools).append(tool)
            from agenthicc.workflows.create_workflow.definition import CreateWorkflow

            phase_spec = next(
                (spec for spec in CreateWorkflow.phases if spec.name == phase_name),
                None,
            )
            phase_role = phase_spec.agent_type if phase_spec is not None else "auto"
            phase_capabilities = (
                phase_spec.resolved_allowed_caps if phase_spec is not None else None
            )

            snapshot = self._authoring_snapshot(
                phase_name=phase_name,
                phase_role=phase_role,
                phase_capabilities=phase_capabilities,
                tools=tools,
            )
            ctx.authoring_snapshot = snapshot.checkpoint_reference()
            ctx.selected_tools = [entry.name for entry in snapshot.tools if entry.available]
            ctx.dependency_summary = {
                "browser": dict(snapshot.browser),
                "mcp": [dict(item) for item in snapshot.mcp],
                "unavailable_optional": [dict(item) for item in snapshot.unavailable],
            }
            dynamic_prompt = (
                f"{system_prompt}\n\n"
                f"{snapshot.render()}\n\n"
                f"{_WORKFLOW_USER_QUESTION_REMINDER}\n\n"
                f"{phase_transition_instruction(tools, phase_name=phase_name)}"
            )
            prompt_contract = build_workflow_prompt_contract(
                workflow_name="create_workflow",
                stable_system_prefix=CACHE_CONTRACT,
                phase_prompt=dynamic_prompt,
                stable_tools=stable_tools,
                phase_tools=phase_tools,
                execution=exec_cfg,
            )
            diagnostic = prompt_contract.diagnostics()
            diagnostic["regions"] = [
                "stable_system_prefix",
                "dynamic_context",
                "stable_tools",
                "phase_tools",
            ]
            diagnostic["authoring_snapshot_id"] = snapshot.snapshot_id
            diagnostic["authoring_catalog_version"] = snapshot.catalog_version
            ctx.cache_diagnostic = diagnostic
            record_contract = getattr(self._cfg.workflow_handle, "record_prompt_contract", None)
            if callable(record_contract):
                record_contract(prompt_contract)
            await _run_agent_turn(
                text,
                runner=self._cfg.agent_runner,
                processor=self._cfg.processor,
                session_memory=ctx.shared_memory,
                conversation_id=self._cfg.conversation_id,
                max_agent_turns=max_turns,
                conv_store=self._cfg.conv_store,
                app_state=self._cfg.app_state,
                exec_cfg=exec_cfg,
                skills=self._cfg.skills,
                skill_permissions=self._cfg.cfg.agents.skill_permissions_for("auto"),
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=tools,
                mcp_registry=self._cfg.mcp_registry,
                active_agent="auto",
                completed_turns=self._cfg.completed_turns,
                approval_svc=self._cfg.approval_svc,
                workspace_access=self._cfg.workspace_access,
                output_collector=[],
                command_outcomes=ctx.command_outcomes,
                system_prompt_suffix=dynamic_prompt,
                prompt_contract=prompt_contract,
                memory_router=self._cfg.memory_router,
                semantic_index=self._cfg.semantic_index,
                next_queued_message=self._cfg.next_queued_message,
                usage_ledger=self._cfg.usage_ledger,
                browser_manager=self._cfg.browser_manager,
            )
        finally:
            reset_current_terminal_wait_policy(policy_token)
            if mode is not None and self._mode_manager is not None:
                self._mode_manager.restore(original_mode)

    def _base_tools(self) -> _ToolList:
        """Return capability-filtered project tools for the current mode."""
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415
        from agenthicc.tools.cloakbrowser import is_browser_tool  # noqa: PLC0415
        from agenthicc.workflows.memory_tools import make_memory_tools  # noqa: PLC0415

        mode_blocked = self._cfg.app_state.active_mode().blocked_capabilities
        all_tools: _ToolList = list(self._cfg.all_plugin_tools())
        if self._cfg.mcp_registry is not None:
            try:
                all_tools = all_tools + list(self._cfg.mcp_registry.all_tools())
            except Exception:  # noqa: BLE001
                pass

        filtered: _ToolList = [
            tool
            for tool in all_tools
            if not is_browser_tool(tool) and not (get_tool_capabilities(tool) & mode_blocked)
        ]
        # Memory tools carry no capability restrictions — always available.
        filtered = filtered + make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index)
        return filtered
