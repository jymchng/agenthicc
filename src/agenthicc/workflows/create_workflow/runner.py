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
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.base import ToolLike
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
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
    "  5. resume(context): re-enter the same dispatch path\n"
    "  6. phase tool factories whose @tool() closures set an asyncio.Event — the state "
    "method checks the event after the turn and NEVER parses the agent's prose\n"
    "  7. build_runner() on the plugin returning that runner\n"
    "  8. checkpoint_context_to_payload(context) and "
    "checkpoint_context_from_payload(payload, memory=None) on the plugin. The context "
    "must carry run_id, current state, phase_iteration, completed artefacts, and all "
    "workflow-specific resume data; omit asyncio.Events, locks, clients, and session "
    "memory from the payload, then attach the supplied memory object during restore. "
    "The payload must contain only bounded JSON-compatible values.\n"
    "  9. run() and resume(context) must use config.session_memory when it is supplied "
    "and must never create a second conversation for a resumed run. Use "
    "config.workflow_handle to attach context and publish the current phase.\n"
    "Subclass CodePlanRunner for the session wiring and its public run_phase(intent=, "
    "text=, system_prompt=, mode=, max_turns=, shared_memory=, tools=) helper; use the "
    "injected session memory for every call (create a local fallback only when no session "
    "memory was supplied). Never "
    "call super().run() — that would execute code_plan's own phases.\n"
    "Call describe_runner_pattern() for the full checklist and "
    "show_example_workflow() for a complete working runner to adapt. Omit the runner ONLY "
    "when every single phase is one unconditional agent turn with no retry and no branch."
)

_AUTHORING_GUIDE: str = (
    "A custom workflow is a single Python file at "
    f"{_WORKFLOW_DIR}/<name>.py that defines a WorkflowPlugin subclass:\n"
    "  - name: lower_snake_case identifier, unique and not a builtin\n"
    "  - description: one line shown in the workflow picker\n"
    "  - mode_bindings: list of mode names that auto-select it ([] = manual only)\n"
    "  - phases: ordered list of PhaseSpec objects wired with next / on_reject — this is "
    "the declarative metadata the registry and the TUI phase counter read\n"
    "  - build_runner(): returns the workflow's own runner (see below)\n"
    "  - checkpoint codecs: required for a custom runner/context so the generated "
    "workflow can pause and resume after Esc or a process restart\n"
    "  - build_params(): typed WorkflowParams when [workflows.<name>] config is needed\n\n"
    + _RUNNER_GUIDE
    + "\nUse describe_phasespec(), list_tool_capabilities(), list_agent_roles(), "
    "describe_cloakbrowser_tools(), describe_playwright_tools(), "
    "describe_runner_pattern() and show_example_workflow() to read the real API instead of "
    "guessing at it. inspect_agenthicc_source() and search_agenthicc_docs() read the "
    "installed agenthicc source and documentation directly."
)

_DESIGN_PROMPT: str = (
    "You are in the DESIGN phase of create_workflow. The user wants a NEW custom "
    "workflow. Your job in this phase is to design it — do not write any files yet.\n\n"
    "Work out, concretely: the workflow's lower_snake_case name; what it is for; the "
    "ordered phases; each phase's objective, agent_type, mode, and max_turns; and the "
    "transition graph (which phase follows which, and where a rejection loops back to). "
    "Explore only what you actually need — inspect the authoring API with the inspection "
    "tools, and read existing workflow files only if the request depends on them.\n\n"
    "Your design MUST also state the workflow's own runner: the state enum members, the "
    "context dataclass fields, the checkpoint payload fields and memory reattachment rule, "
    "one method per state, and the transition tool that ends each phase. Call "
    "describe_runner_pattern() and follow it. Only if every phase is a single "
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
    "system prompt. Write the complete workflow file to disk now with the write tools — "
    "do NOT re-design and do NOT ask for approval again.\n\n"
    "The file must import cleanly on its own: include the module docstring, "
    "'from __future__ import annotations', the imports it uses, and the full "
    "WorkflowPlugin subclass with every PhaseSpec from the approved design. Every "
    "next / on_reject value must name a phase that exists in the same workflow.\n\n"
    "Write the runner the design specified into the SAME file — the state enum, the "
    "context dataclass, one bounded async method per state, the "
    "'while not state.is_terminal' + 'match state' driver in run(), resume(), the phase "
    "tool factories, checkpoint_context_to_payload(), "
    "checkpoint_context_from_payload(payload, memory=None), and build_runner() returning "
    "it. Do not leave the runner or checkpoint codecs as a comment, "
    "a stub, or a TODO: write the working implementation. Use "
    "show_example_workflow() for a complete working runner to adapt, and "
    "inspect_agenthicc_source('agenthicc.workflows.code_plan.runner:CodePlanRunner') when "
    "you need the real signatures.\n\n"
    "WRITE THE FILE IN CHUNKS. A workflow with its own runner is a few hundred lines, and "
    "one tool call carrying the whole file can exceed the response limit — if that happens "
    "the call is discarded and nothing reaches disk. So:\n"
    "  1. write_file(path, content) with the first chunk — the module docstring, the "
    "imports, and the state enum;\n"
    "  2. append_file(path, content) for each following chunk — the context dataclass, the "
    "tool factories, the runner, then the plugin class;\n"
    "  3. keep each chunk to roughly 60-80 lines, and split only between top-level "
    "definitions so no chunk cuts a statement in half;\n"
    "  4. read_file(path) at the end to confirm the whole file is on disk and complete.\n"
    "Never re-write the file from the start after appending — that discards earlier chunks.\n\n"
    "The generated runner must use the WorkflowConfig.session_memory supplied by the "
    "session, including on resume; never instantiate a fresh ShortTermMemory when an "
    "injected session memory exists. The restore hook must attach its memory argument "
    "to the context.\n\n"
    "When the complete file is on disk, call mark_generation_complete(summary, path) "
    "with the exact path you wrote to. Do not call it before the file is written."
)
_GENERATE_REMINDER: str = (
    "You have not yet called mark_generation_complete(summary, path). The approved design "
    "and target path are in your system prompt.\n\n"
    "If a previous write produced no result, the response was too long and was discarded — "
    "write less per call. Use read_file(path) to see how much of the file already exists, "
    "then continue with append_file(path, content) in chunks of roughly 60-80 lines from "
    "where it stops. Do not start the file over. Call mark_generation_complete(summary, path) "
    "once the whole file is on disk."
)

_VALIDATE_PROMPT: str = (
    "You are in the VALIDATION phase of create_workflow. The generated file has already "
    "been imported and checked automatically; the report is in your system prompt.\n\n"
    "Read the report first. If it lists ANY error, call reject_workflow(reason) naming the "
    "concrete fix — the generation phase will run again. If the report passes, read the "
    "generated file and confirm its phases and prompts match the approved design, then "
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
            from agenthicc.workflows.checkpoint import CheckpointValidationError  # noqa: PLC0415

            try:
                handle.save_checkpoint(reason="started")
            except CheckpointValidationError:
                handle.checkpoint_supported = False

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
                if handle is not None:
                    handle.mark_terminal("failed", error="cancelled")
            self._cfg.app_state.workflow_run.set(wf_run)
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
            elif wf_run.status == "failed":
                handle.mark_terminal("failed", error=ctx.fail_reason)
                if handle.checkpoint_supported:
                    handle.save_checkpoint(reason="failed")
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
            # Generic contexts predate typed checkpoints and have no durable
            # state. Keep their documented restart semantics; typed
            # CreateWorkflowContext resumes from its exact outer-loop state.
            return await self.run(context.intent)
        else:
            raise TypeError("workflow resume requires a WorkflowContext")

        self._run_id = ctx.run_id
        if handle is not None:
            handle.attach_context(ctx)
        # Re-enter the same outer dispatch loop.  The typed state is the durable
        # continuation point; a legacy context has no state and therefore starts
        # at DESIGN for backwards compatibility.
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
                if handle is not None:
                    handle.mark_terminal("failed", error="cancelled")
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        if handle is not None:
            if final_status in {"complete", "exited"}:
                handle.mark_terminal("complete")
            else:
                handle.mark_terminal("failed", error=ctx.fail_reason)
            if handle.checkpoint_supported:
                handle.save_checkpoint(reason=final_status)
        return ctx

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
            tools.extend(make_inspection_tools())
            tools.extend(
                make_design_tools(
                    self._cfg.approval_svc,
                    design_event,
                    design_data,
                    exit_event=exit_event,
                )
            )
            tools.extend(make_questions_tool(self._cfg.approval_svc))

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

        self._set_phase("generate", _PHASE_INDEX["generate"], ctx)
        ctx.command_outcomes.clear()

        target_path: str = self._target_path(ctx.workflow_name)
        system_prompt: str = (
            _GENERATE_PROMPT
            + f"\n\n[USER REQUEST]\n{ctx.intent}"
            + f"\n\n[APPROVED DESIGN]\n{ctx.design}"
            + f"\n\n[WORKFLOW NAME]\n{ctx.workflow_name}"
            + f"\n\n[TARGET PATH]\n{target_path}"
        )
        if ctx.rejection_reason:
            system_prompt += (
                f"\n\n[VALIDATION REJECTED THE PREVIOUS ATTEMPT]\n{ctx.rejection_reason}"
            )
        if ctx.validation_report:
            system_prompt += f"\n\n{ctx.validation_report}"

        first_text: str = (
            f"Write the approved {ctx.workflow_name} workflow to {target_path}."
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

            tools: _ToolList = list(self._base_tools()) + list(
                make_generation_tools(generate_event, generate_data)
            )
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
                ctx.generated_path = path if isinstance(path, str) else ""
                ctx.generation_summary = summary if isinstance(summary, str) else ""
                ctx.add_artifact(
                    PhaseArtifact(
                        phase="generate",
                        kind="workflow_file",
                        content=ctx.generation_summary,
                        metadata={
                            "path": ctx.generated_path,
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
        """Check the generated file, then loop until the agent votes.

        Returns SUMMARIZE (approved and the deterministic check passed), GENERATE
        (rejected, or approved against a failing check), or FAILED.
        """
        from agenthicc.workflows.create_workflow.phase_tools import (  # noqa: PLC0415
            make_validation_tools,
        )
        from agenthicc.workflows.create_workflow.validation import (  # noqa: PLC0415
            validate_workflow_file,
        )

        self._set_phase("validate", _PHASE_INDEX["validate"], ctx)
        ctx.command_outcomes.clear()

        report: ValidationReport = validate_workflow_file(
            ctx.generated_path,
            expected_name=ctx.workflow_name,
            root=self._workspace_root(),
        )
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
        self._set_phase("summarize", _PHASE_INDEX["summarize"], ctx)
        ctx.command_outcomes.clear()

        text: str = (
            f"Request: {ctx.intent}\n\n"
            f"Workflow created: {ctx.workflow_name or '(unnamed)'}\n"
            f"File: {ctx.generated_path or '(unknown)'}\n"
            f"What was generated: {ctx.generation_summary or '(see conversation)'}\n"
            f"Validation verdict: {ctx.validation_summary or 'approved'}"
        )
        try:
            await self._run_turn(
                text,
                tools=self._base_tools(),
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
                    "path": ctx.generated_path,
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
        return Path.cwd()

    def _target_path(self, workflow_name: str) -> str:
        """Return the conventional project-local path for *workflow_name*."""
        return f"{_WORKFLOW_DIR}/{workflow_name or 'my_workflow'}.py"

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

    def _set_phase(self, phase_name: str, phase_index: int, ctx: CreateWorkflowContext) -> None:
        """Update all workflow TUI state for the current phase in one call."""
        ctx.state = CreateWorkflowState[phase_name.upper()]
        ctx.phase_iteration += 1
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(ctx)
            handle.update_phase(phase_name, phase_index, ctx.phase_iteration)
        self._cfg.app_state.update_workflow_phase(
            workflow_name=self.workflow_name,
            phase_name=phase_name,
            phase_index=phase_index,
            total_phases=self.total_phases,
            run_id=ctx.run_id,
            intent=ctx.intent,
            model_id=self._phase_model(phase_name) or self._model_id,
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
                output_collector=[],
                command_outcomes=ctx.command_outcomes,
                system_prompt_suffix=system_prompt,
                memory_router=self._cfg.memory_router,
                semantic_index=self._cfg.semantic_index,
                next_queued_message=self._cfg.next_queued_message,
                usage_ledger=self._cfg.usage_ledger,
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
        return filtered + make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index)
