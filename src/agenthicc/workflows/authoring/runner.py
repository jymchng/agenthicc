"""Explicit state-machine runner for the ``create_workflow`` workflow.

The structure intentionally follows ``code_plan``:

* an outer ``while not state.is_terminal`` loop owns routing;
* each phase is a Python method returning the next enum state;
* each phase has a bounded retry loop around agent turns;
* phase transitions are signalled only by phase-local tools; and
* one typed context carries the structured artifacts between phases.

The runner never generates, parses, validates, stages, or publishes the
workflow source.  During EXECUTE the agent calls the canonical ``write_file``
tool directly into ``.agenthicc/workflows``.
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
from agenthicc.workflows.authoring.inspection_tools import make_inspection_tools
from agenthicc.workflows.authoring.phase_tools import (
    make_design_tools,
    make_execute_tools,
    make_interpret_tools,
    make_summarize_tools,
)
from agenthicc.workflows.authoring.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.plugin import PhaseRunRecord

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

_MAX_PHASE_ATTEMPTS = 20
_PHASE_INDEX = {"interpret": 0, "design": 1, "execute": 2, "summarize": 3}
_READ_ONLY_BUILTINS = frozenset(
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
_EXECUTION_TOOL_NAMES = frozenset(
    {
        "shell",
        "run_bash",
        "run_command",
        "run_python",
        "run_python_expr",
        "run_tests",
        "spawn_subagents",
    }
)


class CreateWorkflowRunner(BaseWorkflowRunner):
    """Drive interpretation, design, direct execution, and summary phases."""

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
        """Start an authoring run and return its populated typed context."""

        from lauren_ai._memory import ShortTermMemory

        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("create_workflow requires a non-empty user intent")
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
        """Resume a supplied context from its current non-terminal state."""

        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, CreateWorkflowContext):
            raise TypeError("create_workflow resume requires a CreateWorkflowContext")
        if context.state.is_terminal:
            return context
        self._project_root = Path.cwd().resolve()
        self._run_id = context.run_id
        self._shared_memory = ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        context.shared_memory = self._shared_memory
        await self._start_run(context, resuming=True)
        return await self._drive(context)

    async def _start_run(self, context: CreateWorkflowContext, *, resuming: bool = False) -> None:
        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import WorkflowRun

        phase_name = context.state.name.lower()
        history = [self._phase_record(artifact) for artifact in context.phase_artifacts.values()]
        workflow_run = WorkflowRun(
            run_id=context.run_id,
            workflow_name=self.workflow_name,
            intent=context.intent,
            current_phase=None if context.state.is_terminal else phase_name,
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
        from agenthicc.kernel import Event

        workflow_run = self._cfg.app_state.workflow_run()
        try:
            while not context.state.is_terminal:
                phase_name = context.state.name.lower()
                self._set_phase(context, phase_name)
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
                previous = context.state
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
                await self._complete_phase_event(context, phase_name, previous)
                log.info("create_workflow: %s -> %s", previous.name, context.state.name)

            status = self._status(context.state)
            workflow_run = self._cfg.app_state.workflow_run()
            if workflow_run is not None:
                workflow_run = dataclasses.replace(workflow_run, status=status, current_phase=None)
                self._cfg.app_state.workflow_run.set(workflow_run)
            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunCompleted",
                    {
                        "run_id": context.run_id,
                        "workflow_name": self.workflow_name,
                        "phases_run": len(context.phase_artifacts),
                        "status": status,
                        "error": context.fail_reason,
                    },
                )
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            current_run = self._cfg.app_state.workflow_run() or workflow_run
            if current_run is not None:
                self._cfg.app_state.workflow_run.set(
                    dataclasses.replace(current_run, status="failed", current_phase=None)
                )
            raise
        except Exception as exc:  # noqa: BLE001
            context.fail_reason = f"{type(exc).__name__}: {exc}"
            context.state = CreateWorkflowState.FAILED
            log.exception("create_workflow failed")
            current_run = self._cfg.app_state.workflow_run() or workflow_run
            if current_run is not None:
                self._cfg.app_state.workflow_run.set(
                    dataclasses.replace(current_run, status="failed", current_phase=None)
                )
            self._cfg.conv_store.append_event("error", {"message": context.fail_reason})
        return context

    async def _interpret(
        self, context: CreateWorkflowContext, *, max_agent_turns: int
    ) -> CreateWorkflowState:
        """Normalize user intent and choose the generated workflow name."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="interpret",
            text=context.intent,
            system_prompt=self._prompt("interpret"),
            active_agent="planner",
            max_agent_turns=max_agent_turns,
            tools_factory=make_interpret_tools,
            excluded_capabilities=frozenset(
                {
                    ToolCapability.WRITE,
                    ToolCapability.GIT_WRITE,
                    ToolCapability.EXECUTE,
                    ToolCapability.NETWORK,
                }
            ),
        )
        if result is None:
            return self._fail(context, f"Interpret phase exhausted: {error}")
        context.workflow_name = str(result["workflow_name"])
        context.interpreted_intent = str(result["summary"])
        context.add_artifact(
            "interpret",
            context.interpreted_intent,
            data={"workflow_name": context.workflow_name},
            attempts=attempts,
        )
        return CreateWorkflowState.DESIGN

    async def _design(
        self, context: CreateWorkflowContext, *, max_agent_turns: int
    ) -> CreateWorkflowState:
        """Produce the complete implementation design without filesystem writes."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="design",
            text=f"Design this workflow from the normalized intent.\n\n{context.interpreted_intent}",
            system_prompt=self._prompt("design"),
            active_agent="planner",
            max_agent_turns=max_agent_turns,
            tools_factory=make_design_tools,
            excluded_capabilities=frozenset(
                {
                    ToolCapability.WRITE,
                    ToolCapability.GIT_WRITE,
                    ToolCapability.EXECUTE,
                    ToolCapability.NETWORK,
                }
            ),
        )
        if result is None:
            return self._fail(context, f"Design phase exhausted: {error}")
        context.design = str(result["design"])
        context.add_artifact("design", context.design, attempts=attempts)
        return CreateWorkflowState.EXECUTE

    async def _execute(
        self, context: CreateWorkflowContext, *, max_agent_turns: int
    ) -> CreateWorkflowState:
        """Have the agent write the source and hand off after exact-path checking."""

        expected_root = self._project_root / ".agenthicc" / "workflows"
        result, attempts, error = await self._drive_phase(
            context,
            phase_name="execute",
            text=(
                "Implement the workflow directly from this design. Use write_file to write the complete source.\n\n"
                f"{context.design}"
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
            excluded_capabilities=frozenset(
                {ToolCapability.GIT_WRITE, ToolCapability.EXECUTE, ToolCapability.NETWORK}
            ),
            mode="Auto",
        )
        if result is None:
            return self._fail(context, f"Execute phase exhausted: {error}")
        context.artifact_path = str(result["artifact_path"])
        context.artifact_description = str(result["artifact_description"])
        context.execute_summary = str(result["summary"])
        context.add_artifact(
            "execute",
            context.execute_summary,
            data={
                "artifact_path": context.artifact_path,
                "artifact_name": context.workflow_name,
                "artifact_description": context.artifact_description,
                "agent_owned_write": True,
            },
            attempts=attempts,
        )
        return CreateWorkflowState.SUMMARIZE

    async def _summarize(
        self, context: CreateWorkflowContext, *, max_agent_turns: int
    ) -> CreateWorkflowState:
        """Capture the final agent summary and terminate the run."""

        result, attempts, error = await self._drive_phase(
            context,
            phase_name="summarize",
            text=(
                f"Summarize the completed authoring run.\n\n"
                f"Workflow: {context.workflow_name}\nArtifact: {context.artifact_path}\n"
                f"Execution evidence: {context.execute_summary}"
            ),
            system_prompt=self._prompt("summarize"),
            active_agent="auto",
            max_agent_turns=max_agent_turns,
            tools_factory=make_summarize_tools,
            excluded_capabilities=frozenset(
                {
                    ToolCapability.WRITE,
                    ToolCapability.GIT_WRITE,
                    ToolCapability.EXECUTE,
                    ToolCapability.NETWORK,
                }
            ),
        )
        if result is None:
            return self._fail(context, f"Summarize phase exhausted: {error}")
        context.final_summary = str(result["summary"])
        context.add_artifact("summarize", context.final_summary, attempts=attempts)
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
        tools_factory: Callable[..., list[ToolLike]],
        excluded_capabilities: frozenset[ToolCapability],
        mode: str | None = None,
    ) -> tuple[dict[str, object] | None, int, str]:
        """Run an inner handoff loop until its phase tool signals completion."""

        last_error = ""
        for attempt in range(1, self._attempt_limit() + 1):
            event = asyncio.Event()
            data: dict[str, object] = {}
            tools = self._phase_tools()
            tools.extend(make_inspection_tools())
            tools.extend(tools_factory(event, data))
            prompt_text = (
                text
                if attempt == 1
                else (
                    f"Continue the {phase_name} phase. The previous handoff was not accepted: {last_error or 'no phase handoff was called.'} "
                    "Correct the issue and call the phase handoff tool. Do not stop at prose."
                )
            )
            try:
                await self._run_turn(
                    prompt_text,
                    tools=tools,
                    phase_name=phase_name,
                    active_agent=active_agent,
                    system_prompt=system_prompt + "\n\n" + context.as_system_block(),
                    max_turns=max_agent_turns,
                    excluded_capabilities=excluded_capabilities,
                    mode=mode,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001
                return None, attempt, f"{type(exc).__name__}: {exc}"
            if event.is_set():
                return data, attempt, ""
            last_error = str(data.get("last_error") or "phase handoff was not called")
            self._emit_retry(phase_name, attempt, last_error)
        return None, self._attempt_limit(), last_error or "phase handoff was not called"

    async def _run_turn(
        self,
        text: str,
        *,
        tools: list[ToolLike],
        phase_name: str,
        active_agent: str,
        system_prompt: str,
        max_turns: int,
        excluded_capabilities: frozenset[ToolCapability],
        mode: str | None,
    ) -> None:
        """Run one bounded agent turn with phase-local tool and mode policy."""

        from agenthicc.runners.agent_turn import _run_agent_turn
        from agenthicc.background.terminals import (
            reset_current_terminal_wait_policy,
            set_current_terminal_wait_policy,
        )

        original_mode = self._cfg.app_state.active_mode()
        if mode is not None and self._mode_manager is not None:
            self._mode_manager.set_by_name(mode)
        base_exec = self._cfg.cfg.execution
        phase_model = self._phase_model(phase_name)
        exec_cfg = (
            dataclasses.replace(base_exec, model=phase_model)
            if phase_model and dataclasses.is_dataclass(base_exec)
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
                session_memory=self._shared_memory,
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
        """Return project/MCP and safe memory tools for this authoring run."""

        from agenthicc.workflows.memory_tools import make_memory_tools

        tools = list(self._cfg.all_plugin_tools())
        if self._cfg.mcp_registry is not None:
            tools.extend(self._cfg.mcp_registry.all_tools())
        tools.extend(
            tool
            for tool in make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index)
            if getattr(tool, "__name__", "") != "publish_artifact"
        )
        return tools

    def _allowed_tool_names(self, tools: list[ToolLike], phase_name: str) -> frozenset[str]:
        """Build an exact phase tool surface, keeping shell out of authoring."""

        names = set(_READ_ONLY_BUILTINS)
        for tool in tools:
            name = getattr(tool, "__name__", getattr(tool, "name", ""))
            if name in _EXECUTION_TOOL_NAMES:
                continue
            caps = get_tool_capabilities(tool)
            if not caps.intersection(
                {
                    ToolCapability.WRITE,
                    ToolCapability.GIT_WRITE,
                    ToolCapability.EXECUTE,
                    ToolCapability.NETWORK,
                }
            ):
                names.add(name)
        if phase_name == "execute":
            names.add("write_file")
        return frozenset(name for name in names if name)

    def _attempt_limit(self) -> int:
        execution = getattr(getattr(getattr(self, "_cfg", None), "cfg", None), "execution", None)
        configured = getattr(execution, "authoring_max_generation_attempts", 20)
        return max(1, min(_MAX_PHASE_ATTEMPTS, int(configured)))

    def _phase_turn_limit(self, phase_name: str) -> int:
        execution = getattr(getattr(getattr(self, "_cfg", None), "cfg", None), "execution", None)
        configured = getattr(execution, "authoring_max_phase_turns", 20)
        definition_limit = 20
        try:
            from agenthicc.workflows.authoring.definition import CreateWorkflow

            phase = CreateWorkflow.get_phase(phase_name)
            if phase is not None and phase.max_turns > 0:
                definition_limit = phase.max_turns
        except (ImportError, TypeError, ValueError):
            pass
        return max(1, min(definition_limit, int(configured)))

    def _phase_model(self, phase_name: str) -> str:
        if self._cfg.params is not None:
            model = self._cfg.params.model_for_phase(phase_name, "")
            if model:
                return model
        return ""

    def _prompt(self, phase_name: str) -> str:
        from agenthicc.workflows.authoring.definition import CreateWorkflow

        phase = CreateWorkflow.get_phase(phase_name)
        return phase.system_prompt_override if phase is not None else ""

    def _set_phase(self, context: CreateWorkflowContext, phase_name: str) -> None:
        self._cfg.app_state.update_workflow_phase(
            workflow_name=self.workflow_name,
            phase_name=phase_name,
            phase_index=_PHASE_INDEX[phase_name],
            total_phases=self.total_phases,
            run_id=context.run_id,
            intent=context.intent,
            model_id=self._phase_model(phase_name) or self._model_id,
        )

    async def _complete_phase_event(
        self,
        context: CreateWorkflowContext,
        phase_name: str,
        previous: CreateWorkflowState,
    ) -> None:
        from agenthicc.kernel import Event

        await self._cfg.processor.emit(
            Event.create(
                "WorkflowPhaseCompleted",
                {
                    "run_id": context.run_id,
                    "workflow_name": self.workflow_name,
                    "phase_name": phase_name,
                    "state": previous.name,
                    "next_state": context.state.name,
                    "artifact": dataclasses.asdict(context.phase_artifacts[phase_name])
                    if phase_name in context.phase_artifacts
                    else {},
                },
            )
        )
        workflow_run = self._cfg.app_state.workflow_run()
        if workflow_run is not None and phase_name in context.phase_artifacts:
            record = self._phase_record(context.phase_artifacts[phase_name])
            self._cfg.app_state.workflow_run.set(
                dataclasses.replace(
                    workflow_run, phase_history=workflow_run.phase_history + [record]
                )
            )

    @staticmethod
    def _phase_record(artifact: PhaseArtifact) -> PhaseRunRecord:
        return PhaseRunRecord(
            phase_name=artifact.phase_name,
            role="auto",
            approved=True,
            output_summary=artifact.summary[:200],
            iteration=artifact.attempts,
            duration_s=0.0,
        )

    def _fail(self, context: CreateWorkflowContext, reason: str) -> CreateWorkflowState:
        context.fail_reason = reason
        return CreateWorkflowState.FAILED

    @staticmethod
    def _status(state: CreateWorkflowState) -> str:
        return "complete" if state is CreateWorkflowState.COMPLETE else "failed"

    @staticmethod
    def _emit_retry(phase_name: str, attempt: int, error: str) -> None:
        log.warning(
            "create_workflow phase %s attempt %d/%d did not complete: %s",
            phase_name,
            attempt,
            _MAX_PHASE_ATTEMPTS,
            error,
        )


__all__ = ["CreateWorkflowRunner"]
