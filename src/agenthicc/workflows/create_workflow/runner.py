"""Explicit state-machine runner for the built-in ``create_workflow``.

The runner follows the architecture described for ``code_plan`` while keeping
workflow authoring's concerns local:

* the outer loop evolves :class:`CreateWorkflowState`;
* each state has one phase method;
* each phase method has a bounded inner loop of agent turns;
* only a phase-local handoff tool can set the transition event; and
* :class:`CreateWorkflowContext` captures the structured artifact from every
  successful handoff.

Generated Python is written by the agent through the normal workspace-guarded
``write_file`` tool.  This runner never imports or executes that source.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.base import ToolLike
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.phase_tools import (
    make_design_tools,
    make_execute_tools,
    make_interpret_tools,
    make_summarize_tools,
)
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.plugin import PhaseRunRecord

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

_PHASE_INDEX = {"interpret": 0, "design": 1, "execute": 2, "summarize": 3}
_PHASE_ROLES = {
    "interpret": "planner",
    "design": "planner",
    "execute": "executor",
    "summarize": "auto",
}
_MAX_ATTEMPTS = 20
_MAX_PHASE_TURNS = 100

_DANGEROUS_CAPABILITIES = frozenset(
    {
        ToolCapability.WRITE,
        ToolCapability.GIT_WRITE,
        ToolCapability.EXECUTE,
        ToolCapability.NETWORK,
    }
)
_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "read_lines",
        "list_directory",
        "search_files",
        "grep_file",
        "grep_files",
        "file_exists",
        "get_file_info",
        "checksum_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_blame",
        "git_grep",
    }
)
_NEVER_AUTHORING_TOOLS = frozenset(
    {
        "shell",
        "run_bash",
        "run_command",
        "run_python",
        "run_python_expr",
        "run_tests",
        "spawn_subagents",
        "wait_terminal",
        "wait_terminal_ready",
        "stop_terminal",
    }
)

_PhaseToolsFactory = Callable[[asyncio.Event, dict[str, object]], list[ToolLike]]


class CreateWorkflowRunner(BaseWorkflowRunner):
    """Drive interpretation, design, direct file creation, and summary."""

    workflow_name = "create_workflow"
    total_phases = 4

    def __init__(self, config: WorkflowConfig, mode_manager: ModeManager | None = None) -> None:
        self._cfg = config
        self._mode_manager = mode_manager
        transport_config = getattr(
            getattr(config.agent_runner, "_transport", None), "_config", None
        )
        self._model_id = (
            getattr(transport_config, "model", None) or config.cfg.execution.effective_model()
        )
        self._run_id = ""
        self._project_root = Path.cwd().resolve()
        self._shared_memory: ShortTermMemory | None = None

    async def run(self, intent: str) -> CreateWorkflowContext:
        """Start a new authoring run for a non-empty user intent."""

        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("create_workflow requires a non-empty user intent")
        from lauren_ai._memory import ShortTermMemory

        self._project_root = Path.cwd().resolve()
        self._run_id = uuid.uuid4().hex
        self._shared_memory = ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        context = CreateWorkflowContext(
            intent=intent.strip(),
            run_id=self._run_id,
            shared_memory=self._shared_memory,
        )
        await self._start_run(context)
        return await self._drive(context)

    async def resume(self, context: object) -> CreateWorkflowContext:
        """Resume a non-terminal typed context from its current state."""

        if not isinstance(context, CreateWorkflowContext):
            raise TypeError("create_workflow resume requires a CreateWorkflowContext")
        if context.state.is_terminal:
            return context
        from lauren_ai._memory import ShortTermMemory

        self._project_root = Path.cwd().resolve()
        self._run_id = context.run_id
        self._shared_memory = ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        context.shared_memory = self._shared_memory
        await self._start_run(context, resuming=True)
        return await self._drive(context)

    async def _start_run(self, context: CreateWorkflowContext, *, resuming: bool = False) -> None:
        """Publish initial TUI state and the durable run-start event."""

        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import WorkflowRun

        current_phase = None if context.state.is_terminal else context.state.name.lower()
        history = [self._phase_record(artifact) for artifact in context.phase_artifacts.values()]
        workflow_run = WorkflowRun(
            run_id=context.run_id,
            workflow_name=self.workflow_name,
            intent=context.intent,
            current_phase=current_phase,
            total_phases=self.total_phases,
            phase_history=history,
            status="running" if not context.state.is_terminal else self._status(context.state),
        )
        self._cfg.app_state.workflow_run.set(workflow_run)
        if not resuming:
            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunStarted",
                    {
                        "run_id": context.run_id,
                        "workflow_name": self.workflow_name,
                        "intent": context.intent,
                        "phase_names": list(_PHASE_INDEX),
                    },
                )
            )

    async def _drive(self, context: CreateWorkflowContext) -> CreateWorkflowContext:
        """Run the outer state loop until COMPLETE or FAILED."""

        completed_event = False
        try:
            while not context.state.is_terminal:
                phase_name = context.state.name.lower()
                previous = context.state
                self._set_phase(context, phase_name)
                await self._emit_phase_started(context, phase_name)

                match context.state:
                    case CreateWorkflowState.INTERPRET:
                        context.state = await self._interpret(
                            context, max_agent_turns=self._phase_turn_limit("interpret")
                        )
                    case CreateWorkflowState.DESIGN:
                        context.state = await self._design(
                            context, max_agent_turns=self._phase_turn_limit("design")
                        )
                    case CreateWorkflowState.EXECUTE:
                        context.state = await self._execute(
                            context, max_agent_turns=self._phase_turn_limit("execute")
                        )
                    case CreateWorkflowState.SUMMARIZE:
                        context.state = await self._summarize(
                            context, max_agent_turns=self._phase_turn_limit("summarize")
                        )

                await self._emit_phase_completed(context, phase_name, previous)
                log.info("create_workflow: %s -> %s", previous.name, context.state.name)

            status = self._status(context.state)
            self._set_run_status(context, status)
            await self._emit_run_completed(context, status)
            completed_event = True
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._set_run_status(context, "failed")
            raise
        except Exception as exc:  # noqa: BLE001
            context.fail_reason = f"{type(exc).__name__}: {exc}"
            context.state = CreateWorkflowState.FAILED
            log.exception("create_workflow failed")
            self._append_error(context.fail_reason)
            self._set_run_status(context, "failed")
        finally:
            if not completed_event and context.state is CreateWorkflowState.FAILED:
                try:
                    await self._emit_run_completed(context, "failed")
                except Exception:  # noqa: BLE001
                    log.exception("could not emit create_workflow completion event")
        return context

    async def _interpret(
        self,
        context: CreateWorkflowContext,
        *,
        max_agent_turns: int,
    ) -> CreateWorkflowState:
        """Normalize the use case and choose the generated workflow name."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="interpret",
            text=context.intent,
            system_prompt=self._prompt("interpret"),
            active_agent="planner",
            max_agent_turns=max_agent_turns,
            tools_factory=make_interpret_tools,
            excluded_capabilities=_DANGEROUS_CAPABILITIES,
        )
        if result is None:
            return self._fail(context, f"Interpret phase exhausted: {error}")
        workflow_name = result.get("workflow_name")
        summary = result.get("summary")
        if not isinstance(workflow_name, str) or not isinstance(summary, str):
            return self._fail(context, "Interpret phase returned incomplete handoff data.")
        context.workflow_name = workflow_name
        context.interpreted_intent = summary
        context.add_artifact(
            "interpret",
            summary,
            data={"workflow_name": workflow_name},
            attempts=attempts,
        )
        return CreateWorkflowState.DESIGN

    async def _design(
        self,
        context: CreateWorkflowContext,
        *,
        max_agent_turns: int,
    ) -> CreateWorkflowState:
        """Create the complete implementation design without writing files."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="design",
            text=(
                f"Design a custom workflow for this normalized use case:\n{context.interpreted_intent}"
            ),
            system_prompt=self._prompt("design"),
            active_agent="planner",
            max_agent_turns=max_agent_turns,
            tools_factory=make_design_tools,
            excluded_capabilities=_DANGEROUS_CAPABILITIES,
        )
        if result is None:
            return self._fail(context, f"Design phase exhausted: {error}")
        design = result.get("design")
        if not isinstance(design, str) or not design.strip():
            return self._fail(context, "Design phase returned an empty design.")
        context.design = design
        context.add_artifact("design", design, attempts=attempts)
        return CreateWorkflowState.EXECUTE

    async def _execute(
        self,
        context: CreateWorkflowContext,
        *,
        max_agent_turns: int,
    ) -> CreateWorkflowState:
        """Write the generated source and require an exact-path handoff."""

        if not context.workflow_name or not context.design:
            return self._fail(
                context, "Execute phase requires interpretation and design artifacts."
            )
        expected_root = self._project_root / ".agenthicc" / "workflows"
        result, attempts, error = await self._drive_phase(
            context,
            phase_name="execute",
            text=(
                "Write the complete custom workflow source now.\n\n"
                f"Normalized use case:\n{context.interpreted_intent}\n\n"
                f"Design:\n{context.design}"
            ),
            system_prompt=self._prompt("execute"),
            active_agent="executor",
            max_agent_turns=max_agent_turns,
            tools_factory=lambda event, data: make_execute_tools(
                event,
                data,
                expected_root=expected_root,
                expected_name=context.workflow_name,
            ),
            excluded_capabilities=frozenset(),
            mode="Auto",
        )
        if result is None:
            return self._fail(context, f"Execute phase exhausted: {error}")
        summary = result.get("summary")
        description = result.get("artifact_description")
        artifact_path = result.get("artifact_path")
        artifact_name = result.get("artifact_name")
        if not isinstance(summary, str) or not summary:
            return self._fail(context, "Execute phase returned incomplete artifact data.")
        if not isinstance(description, str) or not description:
            return self._fail(context, "Execute phase returned incomplete artifact data.")
        if not isinstance(artifact_path, str) or not artifact_path:
            return self._fail(context, "Execute phase returned incomplete artifact data.")
        if not isinstance(artifact_name, str) or not artifact_name:
            return self._fail(context, "Execute phase returned incomplete artifact data.")
        context.execute_summary = summary
        context.artifact_description = description
        context.artifact_path = artifact_path
        context.add_artifact(
            "execute",
            summary,
            data={
                "artifact_name": artifact_name,
                "artifact_description": description,
                "artifact_path": artifact_path,
            },
            attempts=attempts,
        )
        return CreateWorkflowState.SUMMARIZE

    async def _summarize(
        self,
        context: CreateWorkflowContext,
        *,
        max_agent_turns: int,
    ) -> CreateWorkflowState:
        """Collect the final truthful summary through an explicit tool handoff."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="summarize",
            text=(
                f"Summarize the completed custom workflow {context.workflow_name!r} at "
                f"{context.artifact_path}.\n{context.artifact_description}"
            ),
            system_prompt=self._prompt("summarize"),
            active_agent="auto",
            max_agent_turns=max_agent_turns,
            tools_factory=make_summarize_tools,
            excluded_capabilities=_DANGEROUS_CAPABILITIES,
        )
        if result is None:
            return self._fail(context, f"Summarize phase exhausted: {error}")
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return self._fail(context, "Summarize phase returned an empty summary.")
        context.final_summary = summary
        context.add_artifact("summarize", summary, attempts=attempts)
        return CreateWorkflowState.COMPLETE

    async def _drive_phase(
        self,
        context: CreateWorkflowContext,
        *,
        phase_name: str,
        text: str,
        system_prompt: str,
        active_agent: str,
        max_agent_turns: int,
        tools_factory: _PhaseToolsFactory,
        excluded_capabilities: frozenset[ToolCapability],
        mode: str | None = None,
    ) -> tuple[dict[str, object] | None, int, str]:
        """Run one bounded inner handoff loop.

        A successful event is the sole transition signal.  The returned data
        is copied from the handoff closure only after the event is observed.
        """

        last_error = "phase handoff was not called"
        for attempt in range(1, self._attempt_limit() + 1):
            context.phase_attempts[phase_name] = attempt
            event = asyncio.Event()
            data: dict[str, object] = {}
            tools = self._phase_tools()
            tools.extend(tools_factory(event, data))
            turn_text = (
                text
                if attempt == 1
                else self._phase_retry_prompt(
                    phase_name=phase_name,
                    original_text=text,
                    system_prompt=system_prompt,
                    last_error=last_error,
                )
            )
            try:
                await self._run_turn(
                    turn_text,
                    tools=tools,
                    mode=mode,
                    system_prompt=system_prompt + "\n\n" + context.as_system_block(),
                    max_turns=max_agent_turns,
                    context=context,
                    phase_name=phase_name,
                    active_agent=active_agent,
                    excluded_capabilities=excluded_capabilities,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("create_workflow %s attempt %d failed: %s", phase_name, attempt, exc)
                continue
            if event.is_set():
                return dict(data), attempt, ""
            candidate = data.get("last_error")
            last_error = candidate if isinstance(candidate, str) and candidate else last_error
            log.warning(
                "create_workflow %s attempt %d/%d did not hand off: %s",
                phase_name,
                attempt,
                self._attempt_limit(),
                last_error,
            )
        return None, self._attempt_limit(), last_error

    async def _run_turn(
        self,
        text: str,
        *,
        tools: list[ToolLike],
        mode: str | None,
        system_prompt: str,
        max_turns: int,
        context: CreateWorkflowContext,
        phase_name: str,
        active_agent: str,
        excluded_capabilities: frozenset[ToolCapability],
    ) -> None:
        """Execute one agent turn with the phase's mode and tool boundary."""

        from agenthicc.background.terminals import (
            reset_current_terminal_wait_policy,
            set_current_terminal_wait_policy,
        )
        from agenthicc.runners.agent_turn import _run_agent_turn

        original_mode = self._cfg.app_state.active_mode()
        if mode is not None and self._mode_manager is not None:
            if self._mode_manager.set_by_name(mode) is None:
                log.warning("create_workflow: mode %r is unavailable", mode)

        base_exec = self._cfg.cfg.execution
        model = self._phase_model(phase_name)
        exec_cfg = (
            dataclasses.replace(base_exec, model=model)
            if model and dataclasses.is_dataclass(base_exec)
            else base_exec
        )
        policy_token = set_current_terminal_wait_policy(
            self._cfg.terminal_wait_policies.get(phase_name, "foreground")
        )
        try:
            await _run_agent_turn(
                text,
                runner=self._cfg.agent_runner,
                processor=self._cfg.processor,
                session_memory=context.shared_memory,
                max_agent_turns=max(1, max_turns),
                conv_store=self._cfg.conv_store,
                app_state=self._cfg.app_state,
                exec_cfg=exec_cfg,
                skills=self._cfg.skills,
                skill_permissions=self._cfg.cfg.agents.skill_permissions_for(active_agent),
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=tools,
                mcp_registry=self._cfg.mcp_registry,
                active_agent=active_agent,
                completed_turns=self._cfg.completed_turns,
                approval_svc=self._cfg.approval_svc,
                output_collector=[],
                command_outcomes=context.command_outcomes,
                system_prompt_suffix=system_prompt,
                excluded_capabilities=excluded_capabilities,
                allowed_tool_names=self._allowed_tool_names(tools, phase_name),
                memory_router=self._cfg.memory_router,
                semantic_index=self._cfg.semantic_index,
            )
        finally:
            reset_current_terminal_wait_policy(policy_token)
            if mode is not None and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(original_mode)

    def _phase_tools(self) -> list[ToolLike]:
        """Return project, MCP, memory, and bounded inspection tools."""

        from agenthicc.workflows.memory_tools import make_memory_tools

        tools = list(self._cfg.all_plugin_tools())
        if self._cfg.mcp_registry is not None:
            tools.extend(self._cfg.mcp_registry.all_tools())
        tools.extend(make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index))
        tools.extend(make_inspection_tools())
        return tools

    def _allowed_tool_names(self, tools: list[ToolLike], phase_name: str) -> frozenset[str]:
        """Return the exact phase-local tool surface.

        Generation deliberately exposes ``write_file`` only in EXECUTE.  Shell
        and arbitrary execution tools remain unavailable even there because
        authoring needs one deterministic, auditable artifact write.
        """

        names = set(_READ_ONLY_TOOLS)
        for candidate in tools:
            name = getattr(candidate, "__name__", getattr(candidate, "name", ""))
            if not name or name in _NEVER_AUTHORING_TOOLS:
                continue
            if get_tool_capabilities(candidate).intersection(_DANGEROUS_CAPABILITIES):
                continue
            names.add(name)
        if phase_name == "execute":
            names.add("write_file")
        return frozenset(names)

    def _attempt_limit(self) -> int:
        configured = getattr(
            self._cfg.cfg.execution, "authoring_max_generation_attempts", _MAX_ATTEMPTS
        )
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = _MAX_ATTEMPTS
        return max(1, min(_MAX_ATTEMPTS, value))

    def _phase_turn_limit(self, phase_name: str) -> int:
        configured = getattr(self._cfg.cfg.execution, "authoring_max_phase_turns", 20)
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 20
        from agenthicc.workflows.create_workflow.definition import CreateWorkflow

        phase = CreateWorkflow.get_phase(phase_name)
        definition_limit = phase.max_turns if phase is not None and phase.max_turns > 0 else 20
        return max(1, min(_MAX_PHASE_TURNS, value, definition_limit))

    def _phase_model(self, phase_name: str) -> str:
        if self._cfg.params is not None:
            model = self._cfg.params.model_for_phase(phase_name, "")
            if model:
                return model
        return ""

    def _prompt(self, phase_name: str) -> str:
        from agenthicc.workflows.create_workflow.definition import CreateWorkflow

        phase = CreateWorkflow.get_phase(phase_name)
        return phase.system_prompt_override if phase is not None else ""

    def _set_phase(self, context: CreateWorkflowContext, phase_name: str) -> None:
        update = getattr(self._cfg.app_state, "update_workflow_phase", None)
        if callable(update):
            update(
                workflow_name=self.workflow_name,
                phase_name=phase_name,
                phase_index=_PHASE_INDEX[phase_name],
                total_phases=self.total_phases,
                run_id=context.run_id,
                intent=context.intent,
                model_id=self._phase_model(phase_name) or self._model_id,
            )

    async def _emit_phase_started(self, context: CreateWorkflowContext, phase_name: str) -> None:
        from agenthicc.kernel import Event

        await self._cfg.processor.emit(
            Event.create(
                "WorkflowPhaseStarted",
                {
                    "run_id": context.run_id,
                    "workflow_name": self.workflow_name,
                    "phase_name": phase_name,
                },
            )
        )

    async def _emit_phase_completed(
        self,
        context: CreateWorkflowContext,
        phase_name: str,
        previous: CreateWorkflowState,
    ) -> None:
        from agenthicc.kernel import Event

        artifact = context.phase_artifacts.get(phase_name)
        payload: dict[str, object] = {
            "run_id": context.run_id,
            "workflow_name": self.workflow_name,
            "phase_name": phase_name,
            "state": previous.name,
            "next_state": context.state.name,
            "artifact": dataclasses.asdict(artifact) if artifact is not None else {},
        }
        await self._cfg.processor.emit(Event.create("WorkflowPhaseCompleted", payload))
        current = self._cfg.app_state.workflow_run()
        if current is not None and artifact is not None:
            self._cfg.app_state.workflow_run.set(
                dataclasses.replace(
                    current,
                    phase_history=current.phase_history + [self._phase_record(artifact)],
                )
            )

    async def _emit_run_completed(self, context: CreateWorkflowContext, status: str) -> None:
        from agenthicc.kernel import Event

        current = self._cfg.app_state.workflow_run()
        phases_run = (
            len(current.phase_history) if current is not None else len(context.phase_artifacts)
        )
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunCompleted",
                {
                    "run_id": context.run_id,
                    "workflow_name": self.workflow_name,
                    "phases_run": phases_run,
                    "status": status,
                    "error": context.fail_reason,
                },
            )
        )

    def _set_run_status(self, context: CreateWorkflowContext, status: str) -> None:
        current = self._cfg.app_state.workflow_run()
        if current is not None:
            self._cfg.app_state.workflow_run.set(
                dataclasses.replace(current, status=status, current_phase=None)
            )
        if status == "failed" and context.fail_reason:
            self._append_error(context.fail_reason)

    def _append_error(self, message: str) -> None:
        try:
            self._cfg.conv_store.append_event("error", {"message": message})
        except Exception:  # noqa: BLE001
            log.debug("could not append create_workflow error to conversation", exc_info=True)

    @staticmethod
    def _phase_record(artifact: PhaseArtifact) -> PhaseRunRecord:
        return PhaseRunRecord(
            phase_name=artifact.phase_name,
            role=_PHASE_ROLES.get(artifact.phase_name, "auto"),
            approved=True,
            output_summary=artifact.summary[:200],
            iteration=artifact.attempts,
            duration_s=0.0,
        )

    @staticmethod
    def _phase_retry_prompt(
        *,
        phase_name: str,
        original_text: str,
        system_prompt: str,
        last_error: str,
    ) -> str:
        """Build a self-contained retry instruction after a missing handoff."""

        handoffs = {
            "interpret": "complete_interpret_phase(summary, workflow_name)",
            "design": "complete_design_phase(design)",
            "execute": "complete_execute_phase(summary, artifact_name, artifact_description)",
            "summarize": "complete_summarize_phase(summary)",
        }
        handoff = handoffs.get(phase_name, "the phase handoff tool")
        return (
            f"RETRY REQUIRED: {phase_name.upper()} phase did not complete.\n"
            f"Reason: {last_error}\n\n"
            f"Original phase task:\n{original_text}\n\n"
            f"Phase instructions:\n{system_prompt}\n\n"
            f"You must complete the work and call {handoff}. Do not stop with prose; "
            "only that handoff tool can advance the phase."
        )

    def _fail(self, context: CreateWorkflowContext, reason: str) -> CreateWorkflowState:
        context.fail_reason = reason
        return CreateWorkflowState.FAILED

    @staticmethod
    def _status(state: CreateWorkflowState) -> str:
        return "complete" if state is CreateWorkflowState.COMPLETE else "failed"


__all__ = ["CreateWorkflowRunner"]
