"""Shared runner for workflow, tool, and command authoring (PRD-147)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.workflows.authoring.artifact import (
    AuthoringArtifact,
    AuthoringResumeContext,
    AuthoringResult,
    ValidationFinding,
    ValidationReport,
    WorkflowCandidate,
    parse_authoring_response,
    source_sha256,
    validate_command_candidate,
    validate_tool_candidate,
)
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.authoring.phase_tools import (
    authoring_transition_tool_name,
    make_authoring_review_tools,
    make_authoring_transition_tools,
)
from agenthicc.workflows.authoring.state import (
    AuthoringContext,
    AuthoringState,
    state_for_phase,
)

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowRun
    from agenthicc.workflows.plugin import PhaseSpec
    from agenthicc.tools.base import ToolLike

log = logging.getLogger(__name__)

_PHASES = ("interpret", "design", "stage", "review", "publish", "summarize")
_DEFAULT_MAX_GENERATION_ATTEMPTS = 20
_MAX_GENERATION_ATTEMPTS = 20
_DEFAULT_MAX_PHASE_TURNS = 20
_MAX_PHASE_TURNS = 100
_MAX_PHASE_ATTEMPTS = 20
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class _PhaseOutput(list[str]):
    """Collect assistant text only until a phase transition succeeds."""

    def __init__(self, transition_event: asyncio.Event) -> None:
        super().__init__()
        self._transition_event = transition_event

    def append(self, value: str) -> None:
        if not self._transition_event.is_set():
            super().append(value)


class CreateWorkflowRunner(BaseWorkflowRunner):
    """Drive one authoring workflow to completion.

    The lifecycle is shared by the three built-in authoring workflows. Concrete
    subclasses only select the artifact contract, destination, and prompt.
    ``create_workflow`` lets the execute agent write its workflow with the
    canonical filesystem tool; extension subclasses retain their parser,
    validator, staging, and approval lifecycle.
    """

    workflow_name = "create_workflow"
    artifact_kind = "workflow"
    destination_dir = "workflows"
    artifact_label = "workflow"
    activation = "workflows-reload"

    def __init__(
        self,
        config: WorkflowConfig,
        mode_manager: ModeManager | None = None,
        phase_specs: tuple[PhaseSpec, ...] | list[PhaseSpec] | None = None,
    ) -> None:
        self._cfg = config
        self._mode_manager = mode_manager
        self._run_id = ""
        self._shared_memory: ShortTermMemory | None = None
        self._workflow_run: WorkflowRun | None = None
        self._project_root = Path.cwd().resolve()
        self._summary_emitted = False
        self._phase_specs = {spec.name: spec for spec in (phase_specs or ())}
        self._design_metadata: dict[str, object] = {}
        self._state = AuthoringState.INTERPRET

    def _phase_names(self) -> tuple[str, ...]:
        """Return the phase topology owned by this concrete authoring definition."""

        if self._phase_specs:
            return tuple(self._phase_specs)
        if self.artifact_kind == "workflow":
            return ("interpret", "design", "execute", "summarize")
        return _PHASES

    def _result(
        self,
        *,
        run_id: str,
        status: str,
        artifact: AuthoringArtifact | None = None,
        approval: str = "not-requested",
        activation: str | None = None,
        error: str | None = None,
        attempts: int = 0,
    ) -> AuthoringResult:
        """Build a result carrying this runner's workflow and artifact kind."""

        result = AuthoringResult(
            workflow=self.workflow_name,
            artifact_kind=self.artifact_kind,
            run_id=run_id,
            status=status,
            artifact=artifact,
            approval=approval,
            activation=activation,
            error=error,
            attempts=attempts,
        )
        return dataclasses.replace(result, summary=self._summary(result))

    async def run(self, intent: str) -> AuthoringResult:
        """Run the complete authoring lifecycle for *intent* as a state machine."""

        if not intent.strip():
            raise ValueError(f"{self.artifact_label.title()} authoring intent must not be empty")
        from lauren_ai._memory import ShortTermMemory
        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import WorkflowRun

        self._run_id = uuid.uuid4().hex
        self._summary_emitted = False
        self._design_metadata = {}
        self._state = AuthoringState.INTERPRET
        self._shared_memory = ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        self._workflow_run = WorkflowRun(
            run_id=self._run_id,
            workflow_name=self.workflow_name,
            intent=intent,
            current_phase="interpret",
            total_phases=len(self._phase_names()),
        )
        self._cfg.app_state.workflow_run.set(self._workflow_run)
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunStarted",
                {
                    "run_id": self._run_id,
                    "workflow_name": self.workflow_name,
                    "intent": intent,
                    "phase_names": list(self._phase_names()),
                },
            )
        )
        ctx = AuthoringContext(
            intent=intent, run_id=self._run_id, shared_memory=self._shared_memory
        )
        state = AuthoringState.INTERPRET
        try:
            while not state.is_terminal:
                phase_name = state.phase_name
                if phase_name is None:
                    raise RuntimeError(f"non-terminal authoring state has no phase: {state}")
                ctx.clear_phase_output()
                await self._start_phase(phase_name)

                match state:
                    case AuthoringState.INTERPRET:
                        state = await self._interpret(
                            ctx, max_agent_turns=self._phase_max_turns("interpret")
                        )
                    case AuthoringState.DESIGN:
                        state = await self._design(
                            ctx, max_agent_turns=self._phase_max_turns("design")
                        )
                    case AuthoringState.EXECUTE:
                        state = await self._execute(
                            ctx, max_agent_turns=self._phase_max_turns("execute")
                        )
                    case AuthoringState.STAGE:
                        state = await self._stage(
                            ctx, max_agent_turns=self._phase_max_turns("stage")
                        )
                    case AuthoringState.REVIEW:
                        state = await self._review(
                            ctx, max_agent_turns=self._phase_max_turns("review")
                        )
                    case AuthoringState.PUBLISH:
                        state = await self._publish(
                            ctx, max_agent_turns=self._phase_max_turns("publish")
                        )
                    case AuthoringState.SUMMARIZE:
                        state = await self._summarize(
                            ctx, max_agent_turns=self._phase_max_turns("summarize")
                        )

                next_label = state.name.lower() if not state.is_terminal else None
                await self._complete_phase(
                    phase_name,
                    ctx.phase_text or f"{phase_name.title()} phase completed.",
                    approved=ctx.phase_approved,
                    structured={
                        **ctx.phase_structured,
                        "state": state_for_phase(phase_name).name,
                        "next_state": next_label,
                    },
                )
                self._state = state

            if ctx.result is None:
                self._set_failure(ctx, "Authoring state machine ended without a result.")
            result = ctx.result
            if result is None:
                raise RuntimeError("authoring result was not created")
            final_status = "complete" if state == AuthoringState.COMPLETE else "failed"
            await self._finish_run(result, status=final_status)
            return result
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self._finish_run(
                self._result(
                    run_id=self._run_id,
                    status="cancelled",
                    artifact=ctx.artifact,
                    error="Workflow authoring was cancelled.",
                    attempts=ctx.attempts,
                ),
                status="failed",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("%s authoring failed", self.workflow_name)
            result = self._result(
                run_id=self._run_id,
                status="failed",
                artifact=ctx.artifact,
                error=f"{type(exc).__name__}: {exc}",
                attempts=ctx.attempts,
            )
            await self._finish_run(result, status="failed")
            return result

    async def _run_tool_gated_phase(
        self,
        ctx: AuthoringContext,
        *,
        phase_name: str,
        text: str,
        system_prompt: str,
        active_agent: str,
        tool_builder: Callable[[asyncio.Event, dict[str, object]], list[ToolLike]],
        max_agent_turns: int | None = None,
        carry_data: dict[str, object] | None = None,
        excluded_capabilities: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, object] | None, int, str | None]:
        """Run one code-plan-style phase until its transition tool succeeds.

        A phase transition is not inferred from assistant text.  Each attempt
        receives a fresh event/data pair, and a rejected transition remains in
        the agent conversation as a structured error so the next turn can fix
        the exact prerequisite instead of blindly repeating the handoff.
        """

        limit = self._phase_attempt_limit(phase_name)
        transition_tool_name = authoring_transition_tool_name(phase_name)
        last_error = ""
        for attempt in range(1, limit + 1):
            transition_event = asyncio.Event()
            transition_data: dict[str, object] = dict(carry_data or {})
            try:
                await self._run_authoring_turn(
                    text
                    if attempt == 1
                    else (
                        f"Continue the {phase_name} phase. The previous transition was not "
                        f"accepted: {last_error} Fix the reported issue, then invoke the "
                        f"{transition_tool_name}() tool again. Do not stop at a prose response."
                    ),
                    phase_name=phase_name,
                    tools=tool_builder(transition_event, transition_data),
                    active_agent=active_agent,
                    system_prompt=system_prompt,
                    max_agent_turns=max_agent_turns,
                    shared_memory=ctx.shared_memory,
                    excluded_capabilities=excluded_capabilities,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001
                return None, attempt, f"{type(exc).__name__}: {exc}"

            if carry_data is not None:
                for key in ("write_receipt", "artifact_name", "artifact_description"):
                    if key in transition_data:
                        carry_data[key] = transition_data[key]

            if transition_event.is_set():
                return transition_data, attempt, None

            last_error = str(
                transition_data.get("last_error")
                or f"the {phase_name} transition tool was not called successfully"
            )
            self._emit_phase_retry(phase_name, attempt, limit, last_error)

        return (
            None,
            limit,
            (
                f"{phase_name.title()} phase exhausted {limit} attempts. Last transition feedback: "
                f"{last_error or 'the transition tool was not invoked.'}"
            ),
        )

    def _phase_agent(self, phase_name: str, default: str) -> str:
        """Resolve the role declared by a phase, with a safe fallback."""

        spec = self._phase_specs.get(phase_name)
        return spec.agent_type if spec is not None and spec.agent_type else default

    def _phase_read_tools(self) -> list[ToolLike]:
        """Return authoring tools without the workflow write tool."""

        return [
            tool
            for tool in self._phase_tools()
            if getattr(tool, "__name__", getattr(tool, "name", "")) != "write_file"
        ]

    def _phase_execute_tools(self) -> list[ToolLike]:
        """Return execute tools with the canonical filesystem writer exposed.

        The design phase removes every ``write_file`` entry so it cannot mutate
        the workspace.  Execute must add the real filesystem tool back, rather
        than a runner-owned proxy: the model sees the same schema and
        capability metadata as a normal agent turn, and the side effect remains
        entirely owned by the agent tool call.  The execute transition verifies
        the exact declared path when a provider does not preserve the tool
        receipt in the following turn.
        """

        from agenthicc.tools.fs.agent_tools import write_file

        return [*self._phase_read_tools(), write_file]

    def _execute_transition_error(
        self, transition_data: dict[str, object]
    ) -> tuple[str, str] | None:
        """Require a successful write or an existing exact workflow file.

        The file may already exist when the provider returns after the tool
        side effect but the tool receipt is not preserved in the following
        transition call.  Existence is checked only at the exact declared path;
        the runner does not read or validate the source.
        """

        raw_name = transition_data.get("artifact_name")
        if not isinstance(raw_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", raw_name):
            return (
                "artifact_name must be a valid lowercase workflow name",
                "provide the stable filename stem used by the successful write_file path",
            )
        description = transition_data.get("artifact_description")
        if not isinstance(description, str) or not description.strip():
            return (
                "artifact_description is missing",
                "provide a concise description of the workflow in complete_execute_phase()",
            )
        expected_root = (self._project_root / ".agenthicc" / self.destination_dir).resolve()
        expected_path = expected_root / f"{raw_name}.py"

        receipt = transition_data.get("write_receipt")
        if isinstance(receipt, dict) and receipt.get("ok") is True:
            raw_path = receipt.get("path")
            if isinstance(raw_path, str):
                try:
                    resolved_path = Path(raw_path).resolve()
                except OSError:
                    resolved_path = None
                if resolved_path == expected_path and expected_root in resolved_path.parents:
                    return None

        try:
            exists = expected_path.is_file()
        except OSError:
            exists = False
        if exists:
            transition_data["write_receipt"] = {
                "ok": True,
                "path": str(expected_path),
                "verified_by": "exact_path_exists",
            }
            return None

        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            return (
                "the execute agent has not completed a successful write_file call and the "
                "declared workflow file does not exist",
                "call write_file with the complete source, or ensure the exact declared "
                "workflow path exists, then retry complete_execute_phase()",
            )
        return (
            "the write_file path does not match the declared workflow artifact and the "
            "declared file does not exist",
            f"write exactly to .agenthicc/{self.destination_dir}/{raw_name}.py",
        )

    async def _interpret(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Interpret intent, continuing until the agent calls its handoff tool."""

        if not self._phase_specs:
            ctx.interpreted_intent = ctx.intent
            ctx.set_phase_output(
                ctx.intent,
                approved=True,
                structured={"intent": ctx.intent, "interpretation": ctx.intent},
            )
            return AuthoringState.DESIGN

        from agenthicc.workflows.authoring.inspection_tools import (
            make_authoring_inspection_tools,
        )

        data, attempts, error = await self._run_tool_gated_phase(
            ctx,
            phase_name="interpret",
            text=ctx.intent,
            system_prompt=(
                "You are in the interpretation phase of an authoring state machine. "
                "Inspect only the current contracts needed for this artifact, normalize "
                f"the user's intent, and call {authoring_transition_tool_name('interpret')}(summary) when the "
                "design handoff is precise. Do not generate source yet."
                + self._phase_prompt("interpret")
            ),
            active_agent="planner",
            max_agent_turns=max_agent_turns,
            tool_builder=lambda event, values: [
                *self._phase_tools(),
                *make_authoring_inspection_tools(),
                *make_authoring_transition_tools("interpret", event, values),
            ],
        )
        if data is None:
            reason = error or "Interpretation did not produce a tool-gated handoff."
            ctx.attempts = attempts
            self._set_failure(ctx, reason)
            ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
            return AuthoringState.SUMMARIZE

        summary = data.get("summary")
        ctx.interpreted_intent = summary if isinstance(summary, str) else ctx.intent
        ctx.set_phase_output(
            ctx.interpreted_intent,
            approved=True,
            structured={
                "intent": ctx.intent,
                "interpretation": ctx.interpreted_intent,
                "attempts": attempts,
            },
        )
        return AuthoringState.DESIGN

    async def _design(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Produce the workflow implementation specification.

        ``create_workflow`` deliberately keeps design read-only.  The execute
        phase owns the agent-written source file and its handoff.
        """

        if self.artifact_kind == "workflow":
            from agenthicc.workflows.authoring.inspection_tools import (
                make_authoring_inspection_tools,
            )
            from agenthicc.tools.capabilities import ToolCapability

            data, attempts, error = await self._run_tool_gated_phase(
                ctx,
                phase_name="design",
                text=(
                    "Create the complete implementation specification for this workflow. "
                    "Do not generate source or modify files."
                ),
                system_prompt=(
                    "You are the read-only design agent in the create_workflow authoring "
                    "state machine. Produce the complete implementation specification, "
                    "then call complete_design_phase(summary). Never call write_file, "
                    "batch_write, shell, or another mutating tool." + self._phase_prompt("design")
                ),
                active_agent=self._phase_agent("design", "planner"),
                max_agent_turns=max_agent_turns,
                tool_builder=lambda event, values: [
                    *self._phase_read_tools(),
                    *make_authoring_inspection_tools(),
                    *make_authoring_transition_tools("design", event, values),
                ],
                excluded_capabilities=frozenset(
                    {
                        ToolCapability.WRITE,
                        ToolCapability.GIT_WRITE,
                        ToolCapability.EXECUTE,
                        ToolCapability.NETWORK,
                    }
                ),
            )
            if data is None:
                reason = error or "Design did not produce a tool-gated handoff."
                ctx.attempts = attempts
                self._set_failure(ctx, reason)
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return AuthoringState.SUMMARIZE

            summary = data.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                reason = "Design completed without an implementation specification."
                self._set_failure(ctx, reason)
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return AuthoringState.SUMMARIZE
            ctx.design_summary = summary.strip()
            ctx.attempts = attempts
            ctx.set_phase_output(
                ctx.design_summary,
                approved=True,
                structured={"design": ctx.design_summary, "attempts": attempts},
            )
            return AuthoringState.EXECUTE

        try:
            candidate, report, attempts, generation_text = await self._generate(
                ctx.interpreted_intent or ctx.intent,
                max_agent_turns=max_agent_turns,
                shared_memory=ctx.shared_memory,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_failure(ctx, f"{type(exc).__name__}: {exc}")
            ctx.set_phase_output(self._result_error(ctx, "Design failed."), approved=False)
            return AuthoringState.SUMMARIZE

        ctx.candidate = candidate
        ctx.report = report
        ctx.attempts = attempts
        ctx.generation_text = generation_text
        if self.artifact_kind == "workflow":
            if self._design_metadata.get("phase") != "design":
                reason = (
                    "The design agent did not complete its direct write handoff. "
                    "No assistant response was copied into .agenthicc/workflows."
                )
                self._set_failure(ctx, reason)
                ctx.set_phase_output(
                    self._result_error(ctx, "Design did not complete."),
                    approved=False,
                    structured={"attempts": attempts, "agent_owned_write": True},
                )
                return AuthoringState.SUMMARIZE
            ctx.artifact = self._agent_reported_workflow_artifact()
            ctx.result = self._result(
                run_id=self._run_id,
                status="complete",
                artifact=ctx.artifact,
                approval="not-requested",
                activation=self.activation,
                attempts=attempts,
            )
            ctx.set_phase_output(
                self._summary(ctx.result),
                approved=True,
                structured={
                    **ctx.result.to_dict(),
                    "agent_owned_write": True,
                    "runner_wrote_artifact": False,
                },
            )
            return AuthoringState.SUMMARIZE
        ctx.set_phase_output(
            generation_text or self._validation_text(report, attempts=attempts),
            approved=report.valid,
            structured={"attempts": attempts, "validation": report.to_dict()},
        )
        return AuthoringState.STAGE

    async def _execute(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Have the execute agent write and hand off the workflow source."""

        if self.artifact_kind != "workflow":
            raise RuntimeError("execute is only valid for create_workflow")

        from agenthicc.workflows.authoring.inspection_tools import (
            make_authoring_inspection_tools,
        )

        persistent_data: dict[str, object] = {}
        data, attempts, error = await self._run_tool_gated_phase(
            ctx,
            phase_name="execute",
            text=(
                "Implement the workflow from this design specification. Write the complete "
                "source with write_file, then complete the execute handoff.\n\n"
                f"DESIGN SPECIFICATION:\n{ctx.design_summary}"
            ),
            system_prompt=(
                "You are the write-capable execute agent in the create_workflow authoring "
                "state machine. Consume the design specification, generate the complete "
                "workflow source, call write_file exactly for the project workflow path, "
                "wait for success, then call complete_execute_phase(summary, "
                "artifact_name, artifact_description). Do not stop at prose."
                + self._phase_prompt("execute")
            ),
            active_agent=self._phase_agent("execute", "executor"),
            max_agent_turns=max_agent_turns,
            carry_data=persistent_data,
            tool_builder=lambda event, values: [
                *self._phase_execute_tools(),
                *make_authoring_inspection_tools(),
                *make_authoring_transition_tools(
                    "execute",
                    event,
                    values,
                    validator=self._execute_transition_error,
                ),
            ],
        )
        if data is None:
            reason = error or "Execute did not produce a successful write handoff."
            ctx.attempts = attempts
            self._set_failure(ctx, reason)
            ctx.set_phase_output(
                self._result_error(ctx, "Execute did not complete."),
                approved=False,
                structured={
                    "attempts": attempts,
                    "agent_owned_write": True,
                    "write_receipt": persistent_data.get("write_receipt"),
                },
            )
            return AuthoringState.SUMMARIZE

        self._design_metadata = dict(data)
        ctx.attempts = attempts
        ctx.artifact = self._agent_reported_workflow_artifact()
        ctx.result = self._result(
            run_id=self._run_id,
            status="complete",
            artifact=ctx.artifact,
            approval="not-requested",
            activation=self.activation,
            attempts=attempts,
        )
        ctx.set_phase_output(
            self._summary(ctx.result),
            approved=True,
            structured={
                **ctx.result.to_dict(),
                "agent_owned_write": True,
                "runner_wrote_artifact": False,
                "write_receipt": data.get("write_receipt"),
            },
        )
        return AuthoringState.SUMMARIZE

    async def _stage(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Stage valid source without making it discoverable."""

        if ctx.candidate is None or not ctx.report.valid:
            reason = (
                self._validation_text(ctx.report, attempts=ctx.attempts)
                if ctx.report.findings
                else (
                    f"No artifact staged because generation did not produce a valid "
                    f"{self.artifact_label}."
                )
            )
            self._set_failure(ctx, reason)
            ctx.set_phase_output(reason, approved=False)
            return AuthoringState.SUMMARIZE
        handoff_summary = ""
        if self._phase_specs:
            data, attempts, error = await self._run_tool_gated_phase(
                ctx,
                phase_name="stage",
                text=(
                    f"The {self.artifact_label} passed design validation. Confirm that it "
                    "is ready to be stored in the run-scoped staging area, then call "
                    f"{authoring_transition_tool_name('stage')}(summary). Do not publish or execute it."
                ),
                system_prompt=(
                    "You are in the staging phase. Confirm the candidate is ready for "
                    f"isolated run-scoped staging. Call {authoring_transition_tool_name('stage')}(summary) "
                    "only after checking the handoff requirements; do not write to the "
                    "discoverable extension directory, publish, or execute generated code."
                    + self._phase_prompt("stage")
                ),
                active_agent="auto",
                max_agent_turns=max_agent_turns,
                tool_builder=lambda event, values: [
                    *self._phase_tools(),
                    *make_authoring_transition_tools(
                        "stage",
                        event,
                        values,
                        validator=lambda _data: self._artifact_ready_transition_error(ctx),
                    ),
                ],
            )
            if data is None:
                reason = error or "Staging did not receive a valid tool-gated handoff."
                self._set_failure(ctx, reason)
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return AuthoringState.SUMMARIZE
            summary = data.get("summary")
            handoff_summary = summary if isinstance(summary, str) else ""
        try:
            ctx.artifact = await self._stage_artifact(ctx.candidate, ctx.report, ctx.intent)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_failure(ctx, f"{type(exc).__name__}: {exc}")
            ctx.set_phase_output(self._result_error(ctx, "Staging failed."), approved=False)
            return AuthoringState.SUMMARIZE
        ctx.set_phase_output(
            handoff_summary or f"Staged {ctx.artifact.staged_path}.",
            approved=True,
            structured=ctx.artifact.to_dict(),
        )
        return AuthoringState.REVIEW

    async def _review(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Request explicit publication approval and route through its result."""

        if ctx.artifact is None or ctx.candidate is None:
            self._set_failure(ctx, "Cannot review an unstaged authoring artifact.")
            ctx.set_phase_output(self._result_error(ctx, "Review unavailable."), approved=False)
            return AuthoringState.SUMMARIZE
        artifact = ctx.artifact
        candidate = ctx.candidate

        if self._phase_specs:
            data, attempts, error = await self._run_tool_gated_phase(
                ctx,
                phase_name="review",
                text=(
                    f"Review the staged {self.artifact_label} at {ctx.artifact.staged_path}. "
                    "Present its behavior and validation evidence, then call "
                    "request_publication_approval() exactly once."
                ),
                system_prompt=(
                    "You are the human-approval review phase. Inspect the staged artifact, "
                    "summarize its behavior and validation evidence, and call "
                    "request_publication_approval() to obtain the explicit publication "
                    "decision. Do not publish or modify the artifact."
                    + self._phase_prompt("review")
                ),
                active_agent="reviewer",
                max_agent_turns=max_agent_turns,
                tool_builder=lambda event, values: [
                    *self._phase_tools(),
                    *make_authoring_review_tools(
                        lambda: self._request_publication_approval(artifact, candidate),
                        event,
                        values,
                    ),
                ],
            )
            if data is None:
                reason = error or "Review did not receive a publication decision."
                self._set_failure(ctx, reason)
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return AuthoringState.SUMMARIZE
            approved = data.get("approved") is True
            if not approved:
                ctx.result = self._result(
                    run_id=self._run_id,
                    status="rejected",
                    artifact=dataclasses.replace(ctx.artifact, state="staged"),
                    approval="denied",
                    error="Publication approval was denied; the candidate remains staged.",
                    attempts=ctx.attempts,
                )
                ctx.set_phase_output(
                    ctx.result.error or "Publication approval was denied.",
                    approved=False,
                    structured={"attempts": attempts, "approved": False},
                )
                return AuthoringState.SUMMARIZE
            ctx.approval_granted = True
            ctx.set_phase_output(
                "Publication approved.",
                approved=True,
                structured={"attempts": attempts, "approved": True},
            )
            return AuthoringState.PUBLISH

        try:
            approved = await self._request_publication_approval(ctx.artifact, ctx.candidate)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_failure(ctx, f"{type(exc).__name__}: {exc}")
            ctx.set_phase_output(
                self._result_error(ctx, "Publication review failed."), approved=False
            )
            return AuthoringState.SUMMARIZE
        if not approved:
            ctx.result = self._result(
                run_id=self._run_id,
                status="rejected",
                artifact=dataclasses.replace(ctx.artifact, state="staged"),
                approval="denied",
                error="Publication approval was denied; the candidate remains staged.",
                attempts=ctx.attempts,
            )
            ctx.set_phase_output(
                self._result_error(ctx, "Publication approval was denied."), approved=False
            )
            return AuthoringState.SUMMARIZE
        ctx.approval_granted = True
        ctx.set_phase_output("Publication approved.", approved=True)
        return AuthoringState.PUBLISH

    async def _publish(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Atomically publish the already-approved artifact."""

        if ctx.artifact is None or ctx.candidate is None:
            self._set_failure(ctx, "Cannot publish an unstaged authoring artifact.")
            ctx.set_phase_output(
                self._result_error(ctx, "Publication unavailable."), approved=False
            )
            return AuthoringState.SUMMARIZE
        if self._phase_specs:
            data, attempts, error = await self._run_tool_gated_phase(
                ctx,
                phase_name="publish",
                text=(
                    f"The staged {self.artifact_label} passed review. Confirm that it is "
                    "approved and ready for atomic publication, then call "
                    f"{authoring_transition_tool_name('publish')}(summary)."
                ),
                system_prompt=(
                    "You are in the publication phase. Confirm the staged artifact is the "
                    "same validated artifact that received explicit approval, then call "
                    f"{authoring_transition_tool_name('publish')}(summary). Do not alter the source or claim "
                    "publication before the runner completes it." + self._phase_prompt("publish")
                ),
                active_agent="auto",
                max_agent_turns=max_agent_turns,
                tool_builder=lambda event, values: [
                    *self._phase_tools(),
                    *make_authoring_transition_tools(
                        "publish",
                        event,
                        values,
                        validator=lambda _data: self._publish_transition_error(ctx),
                    ),
                ],
            )
            if data is None:
                reason = error or "Publication did not receive a valid tool-gated handoff."
                self._set_failure(ctx, reason)
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return AuthoringState.SUMMARIZE
        try:
            published = self._publish_artifact(ctx.artifact, ctx.candidate)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_failure(ctx, f"{type(exc).__name__}: {exc}")
            ctx.set_phase_output(self._result_error(ctx, "Publication failed."), approved=False)
            return AuthoringState.SUMMARIZE
        ctx.artifact = published
        ctx.result = self._result(
            run_id=self._run_id,
            status="published",
            artifact=published,
            approval="approved",
            activation=self.activation,
            attempts=ctx.attempts,
        )
        ctx.set_phase_output(
            f"Published {published.published_path}; restart the session to discover it.",
            approved=True,
            structured=published.to_dict(),
        )
        return AuthoringState.SUMMARIZE

    async def _summarize(
        self, ctx: AuthoringContext, *, max_agent_turns: int | None = None
    ) -> AuthoringState:
        """Emit the terminal authoring summary and select the terminal state."""

        if ctx.result is None:
            self._set_failure(ctx, "Authoring summary was reached without a result.")
        if ctx.result is None:
            raise RuntimeError("authoring summary result was not created")
        if self._phase_specs:
            data, attempts, error = await self._run_tool_gated_phase(
                ctx,
                phase_name="summarize",
                text=(
                    f"Summarize the authoring result: {ctx.result.to_dict()}. Call "
                    f"{authoring_transition_tool_name('summarize')}(summary) with the exact status, artifact "
                    "location, validation/approval outcome, and required activation step."
                ),
                system_prompt=(
                    "You are in the summary phase. Report only the authoritative authoring "
                    "result supplied in the phase context. Call "
                    f"{authoring_transition_tool_name('summarize')}(summary) after including status, artifact "
                    "path, validation and approval outcome, and the next activation action. "
                    "Never claim publication or activation that the result does not show."
                    + self._phase_prompt("summarize")
                ),
                active_agent="auto",
                max_agent_turns=max_agent_turns,
                tool_builder=lambda event, values: [
                    *self._phase_tools(),
                    *make_authoring_transition_tools(
                        "summarize",
                        event,
                        values,
                        validator=lambda _data: self._summary_transition_error(ctx),
                    ),
                ],
            )
            if data is None:
                reason = error or "Summary did not receive a valid tool-gated handoff."
                ctx.set_phase_output(reason, approved=False, structured={"attempts": attempts})
                return self._terminal_state_for_result(ctx.result)
            summary = data.get("summary")
            ctx.set_phase_output(
                summary
                if isinstance(summary, str)
                else ctx.result.summary or self._summary(ctx.result),
                approved=ctx.result.status == "published",
                structured={**ctx.result.to_dict(), "attempts": attempts, "agent_summary": summary},
            )
            return self._terminal_state_for_result(ctx.result)
        ctx.set_phase_output(
            ctx.result.summary or self._summary(ctx.result),
            approved=ctx.result.status == "published",
            structured=ctx.result.to_dict(),
        )
        return self._terminal_state_for_result(ctx.result)

    @staticmethod
    def _terminal_state_for_result(result: AuthoringResult) -> AuthoringState:
        """Map an authoring result to the terminal state used by ``run``."""

        if result.status in {"complete", "published"}:
            return AuthoringState.COMPLETE
        if result.status == "rejected":
            return AuthoringState.REJECTED
        return AuthoringState.FAILED

    @staticmethod
    def _summary_transition_error(
        ctx: AuthoringContext,
    ) -> tuple[str, str] | None:
        if ctx.result is None:
            return (
                "the authoring result is missing",
                "finish the preceding phase and summarize only its structured result",
            )
        return None

    def _set_failure(self, ctx: AuthoringContext, error: str) -> None:
        """Create a structured failure that the summarize phase can report."""

        ctx.result = self._result(
            run_id=self._run_id,
            status="failed",
            artifact=ctx.artifact,
            error=error,
            attempts=ctx.attempts,
        )

    @staticmethod
    def _result_error(ctx: AuthoringContext, fallback: str) -> str:
        """Return a non-empty error for a phase completion payload."""

        return ctx.result.error if ctx.result is not None and ctx.result.error else fallback

    def _emit_phase_retry(
        self,
        phase_name: str,
        attempt: int,
        limit: int,
        reason: str,
    ) -> None:
        """Emit a visible retry notice without turning a missing tool call into an exception."""

        self._cfg.conv_store.append_event(
            "system",
            {
                "text": (
                    f"{phase_name.title()} phase attempt {attempt}/{limit} did not "
                    f"complete; retrying. {reason}"
                )
            },
        )

    async def resume(self, context: object) -> AuthoringResult:
        """Resume an extension-authoring run without duplicating side effects.

        ``create_workflow`` deliberately has no runner-owned staged artifact:
        its design agent writes the workflow directly with ``write_file``. A
        direct-write run therefore cannot be resumed by this runner.
        """

        from agenthicc.workflows.plugin import WorkflowContext, WorkflowRun

        if isinstance(context, AuthoringResumeContext):
            run_id = context.run_id
            intent = context.intent
        elif isinstance(context, WorkflowContext):
            run_id = context.run_id
            intent = context.intent
        else:
            raise TypeError(
                "create_workflow resume requires AuthoringResumeContext or WorkflowContext"
            )

        if self.artifact_kind == "workflow":
            self._run_id = run_id
            self._summary_emitted = False
            self._workflow_run = WorkflowRun(
                run_id=run_id,
                workflow_name=self.workflow_name,
                intent=intent,
                current_phase=None,
                total_phases=len(self._phase_names()),
            )
            self._cfg.app_state.workflow_run.set(self._workflow_run)
            result = self._result(
                run_id=run_id,
                status="failed",
                error=(
                    "create_workflow has no runner-owned staged artifact to resume. "
                    "Inspect the workflow written by the execute agent or run "
                    "/workflow create_workflow again."
                ),
            )
            await self._finish_run(result, status="failed")
            return result

        artifact: AuthoringArtifact | None = None
        try:
            candidate, _report, artifact, manifest_state, manifest_intent = (
                self._load_staged_artifact(run_id)
            )
            intent = intent or manifest_intent
            self._run_id = run_id
            self._summary_emitted = False
            self._workflow_run = WorkflowRun(
                run_id=run_id,
                workflow_name=self.workflow_name,
                intent=intent,
                current_phase="design" if self.artifact_kind == "workflow" else "review",
                total_phases=len(self._phase_names()),
            )
            self._cfg.app_state.workflow_run.set(self._workflow_run)
            from agenthicc.kernel import Event

            await self._cfg.processor.emit(
                Event.create(
                    "WorkflowRunStarted",
                    {
                        "run_id": run_id,
                        "workflow_name": self.workflow_name,
                        "intent": intent,
                        "phase_names": list(self._phase_names()),
                        "resumed": True,
                    },
                )
            )

            if manifest_state == "published":
                result = self._result(
                    run_id=run_id,
                    status="published",
                    artifact=artifact,
                    approval="already-approved",
                    activation=self.activation,
                )
                await self._finish_run(result, status="complete")
                return result

            await self._start_phase("review")
            approved = await self._request_publication_approval(artifact, candidate)
            if not approved:
                result = self._result(
                    run_id=run_id,
                    status="rejected",
                    artifact=dataclasses.replace(artifact, state="staged"),
                    approval="denied",
                    error="Publication approval was denied; the candidate remains staged.",
                )
                await self._complete_phase(
                    "review", result.error or "Publication denied.", approved=False
                )
                await self._complete_phase("summarize", result.error or "Publication denied.")
                await self._finish_run(result, status="failed")
                return result

            await self._complete_phase("review", "Publication approved.", approved=True)
            await self._start_phase("publish")
            published = self._publish_artifact(artifact, candidate)
            result = self._result(
                run_id=run_id,
                status="published",
                artifact=published,
                approval="approved",
                activation=self.activation,
            )
            await self._complete_phase(
                "publish",
                f"Published {published.published_path}; restart the session to discover it.",
                approved=True,
                structured=published.to_dict(),
            )
            await self._complete_phase("summarize", self._summary(result), approved=True)
            await self._finish_run(result, status="complete")
            return result
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self._finish_run(
                self._result(
                    run_id=run_id,
                    status="cancelled",
                    artifact=artifact,
                    error="Workflow authoring resume was cancelled.",
                ),
                status="failed",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            result = self._result(
                run_id=run_id,
                status="failed",
                artifact=artifact,
                error=f"{type(exc).__name__}: {exc}",
            )
            if self._workflow_run is None:
                from agenthicc.workflows.plugin import WorkflowRun

                self._run_id = run_id
                self._workflow_run = WorkflowRun(
                    run_id=run_id,
                    workflow_name=self.workflow_name,
                    intent=intent,
                    current_phase=None,
                    total_phases=len(self._phase_names()),
                )
                self._cfg.app_state.workflow_run.set(self._workflow_run)
            await self._finish_run(result, status="failed")
            return result

    def _parse_candidate(self, text: str) -> WorkflowCandidate:
        """Parse the model source response for this artifact kind."""

        if self.artifact_kind == "workflow":
            raise RuntimeError(
                "create_workflow is agent-owned: the runner does not parse assistant output"
            )
        return parse_authoring_response(text, self.artifact_kind)

    def _validate_candidate(self, candidate: WorkflowCandidate) -> ValidationReport:
        """Validate the model candidate using the selected export contract."""

        if self.artifact_kind == "workflow":
            raise RuntimeError(
                "create_workflow is agent-owned: the runner does not validate workflow source"
            )
        if self.artifact_kind == "tool":
            return validate_tool_candidate(candidate)
        return validate_command_candidate(candidate)

    @staticmethod
    def _unvalidated_report() -> ValidationReport:
        """Describe the intentionally skipped source checks for raw workflows."""

        return ValidationReport(
            (
                ValidationFinding(
                    "not-run",
                    "Source parsing and static validation were intentionally skipped.",
                    blocking=False,
                ),
            )
        )

    def _agent_reported_workflow_artifact(self) -> AuthoringArtifact | None:
        """Describe the path reported by the execute agent without inspecting it.

        The execute agent owns the actual ``write_file`` call.  This method only
        turns the agent-provided stable name into result metadata; it never
        reads, hashes, parses, validates, or writes the source file.
        """

        raw_name = self._design_metadata.get("artifact_name")
        if not isinstance(raw_name, str):
            return None
        name = raw_name.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
            return None
        path = self._project_root / ".agenthicc" / self.destination_dir / f"{name}.py"
        return AuthoringArtifact(
            name=name,
            state="agent-written",
            staged_path=str(path),
            published_path=None,
            sha256="",
            validation=self._unvalidated_report(),
            manifest_path=None,
        )

    def _generation_attempt_limit(self) -> int:
        """Return the bounded source-generation attempt limit from execution config."""

        configured = self._cfg.cfg.execution.authoring_max_generation_attempts
        if isinstance(configured, bool) or not isinstance(configured, int):
            configured = _DEFAULT_MAX_GENERATION_ATTEMPTS
        return max(1, min(configured, _MAX_GENERATION_ATTEMPTS))

    def _phase_max_turns(self, phase_name: str) -> int:
        """Resolve a phase's bounded multi-turn budget.

        A definition may specialize the default, while the execution setting
        remains the operator's global safety ceiling for every ``create_*``
        phase.  Both values are clamped to keep malformed TOML harmless.
        """

        configured = self._cfg.cfg.execution.authoring_max_phase_turns
        if isinstance(configured, bool) or not isinstance(configured, int):
            configured = _DEFAULT_MAX_PHASE_TURNS
        configured = max(1, min(configured, _MAX_PHASE_TURNS))
        spec = self._phase_specs.get(phase_name)
        if spec is None or spec.max_turns <= 0:
            return configured
        return max(1, min(spec.max_turns, configured, _MAX_PHASE_TURNS))

    def _phase_attempt_limit(self, phase_name: str) -> int:
        """Resolve the bounded number of phase-level continuation attempts."""

        spec = self._phase_specs.get(phase_name)
        if spec is not None and spec.max_iterations > 0:
            return max(1, min(spec.max_iterations, _MAX_PHASE_ATTEMPTS))
        return _MAX_PHASE_ATTEMPTS

    def _phase_prompt(self, phase_name: str) -> str:
        """Return the literal phase contract selected by the workflow definition."""

        try:
            phase_specs = self._phase_specs
        except AttributeError:
            # Prompt-only callers and older integrations may construct the
            # runner with ``object.__new__``; those calls have no definition
            # metadata but still need the stable generation contract.
            return ""
        spec = phase_specs.get(phase_name)
        if spec is None or not spec.system_prompt_override.strip():
            return ""
        next_phase = spec.next.upper() if spec.next else "TERMINAL"
        rejection_route = (
            f" If rejected, the runner routes to {spec.on_reject.upper()}."
            if spec.on_reject
            else ""
        )
        memory_boundary = (
            "do not use memory to replace the direct source capture contract"
            if self.artifact_kind == "workflow"
            else "do not rely on memory to bypass the explicit transition tool or validation gates"
        )
        return (
            f"\n\n[AUTHORING PHASE: {phase_name}]\n"
            f"{spec.system_prompt_override.strip()}\n"
            f"This phase's unique transition tool is {authoring_transition_tool_name(phase_name)}(summary).\n"
            "The phase may use multiple agent turns. Do not advance by merely "
            "writing a conversational answer; use the phase transition tool when "
            "the objective is complete.\n"
            "ULTIMATE PURPOSE REMINDER: create one new specialized agenthicc workflow "
            "from the user's intent. TRANSITION REMINDER: invoke the phase-local "
            f"{authoring_transition_tool_name(phase_name)} only after completing this phase; a successful handoff "
            f"moves the authoring run to {next_phase}.{rejection_route}\n"
            "MEMORY REMINDER: one shared session memory is carried across all authoring "
            "phases. Use memory_write/memory_read for important authoring decisions and "
            f"semantic_search when prior phase context is needed; {memory_boundary}."
        )

    def _generation_feedback(
        self,
        report: ValidationReport,
        *,
        parse_error: str | None = None,
        phase_transition_required: bool = False,
    ) -> str:
        """Build actionable correction instructions for the next agent attempt."""

        if phase_transition_required:
            return (
                "The design phase is tool-gated. Generate the complete raw Python "
                "source directly in your assistant response, with no analysis or "
                "envelope, then immediately call complete_design_phase(summary=...). "
                "The runner captures and validates that response; it cannot advance "
                "from analysis, an empty response, or a transition call without source."
            )
        if parse_error is not None:
            return (
                "The previous response was not a parseable source artifact. "
                f"Parser finding: {parse_error}\n"
                "It contained analysis, tool-call activity, or incomplete output "
                "instead of the requested source. Do not repeat repository inspection "
                "or describe what you will generate. Now return the complete source "
                "file directly, with no prose, XML, JSON, or Markdown fence."
            )
        findings = "\n".join(
            f"- [{item.code}] {item.message}" for item in report.findings if item.blocking
        )
        return (
            "The previous source artifact was returned but failed static validation. "
            "Preserve the user's intent and correct every blocking finding below:\n"
            f"{findings}\n"
            "Return the complete corrected source file directly, not a patch, plan, "
            "explanation, XML, JSON, or Markdown fence. Re-check the source contract "
            "before responding."
        )

    async def _run_authoring_turn(
        self,
        text: str,
        *,
        phase_name: str,
        tools: list[ToolLike],
        active_agent: str,
        system_prompt: str,
        output: list[str] | None = None,
        max_agent_turns: int | None = None,
        shared_memory: ShortTermMemory | None = None,
        excluded_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        """Run one bounded tool-capable turn for an authoring phase."""

        from agenthicc.runners.agent_turn import _run_agent_turn

        memory = shared_memory if shared_memory is not None else self._shared_memory
        if memory is None:
            raise RuntimeError("authoring shared memory is not initialized")
        if self._cfg.approval_svc is not None:
            memory.ensure_valid()
        await _run_agent_turn(
            text,
            runner=self._cfg.agent_runner,
            processor=self._cfg.processor,
            session_memory=memory,
            max_agent_turns=max(
                1,
                min(
                    max_agent_turns
                    if max_agent_turns is not None
                    else self._phase_max_turns(phase_name),
                    self._cfg.cfg.execution.max_agent_turns,
                ),
            ),
            conv_store=self._cfg.conv_store,
            app_state=self._cfg.app_state,
            exec_cfg=self._cfg.cfg.execution,
            skills=self._cfg.skills,
            skill_permissions=self._cfg.cfg.agents.skill_permissions_for(active_agent),
            mention_cache=self._cfg.mention_cache,
            project_plugin_tools=tools,
            mcp_registry=self._cfg.mcp_registry,
            active_agent=active_agent,
            completed_turns=self._cfg.completed_turns,
            approval_svc=self._cfg.approval_svc,
            output_collector=output if output is not None else [],
            system_prompt_suffix=system_prompt,
            excluded_capabilities=excluded_capabilities,
            memory_router=self._cfg.memory_router,
            semantic_index=self._cfg.semantic_index,
        )

    def _phase_tools(self) -> list[ToolLike]:
        """Return project tools plus shared-memory tools for every phase."""

        from agenthicc.workflows.memory_tools import make_memory_tools

        tools = [
            *self._cfg.all_plugin_tools(),
            *make_memory_tools(self._cfg.memory_router, self._cfg.semantic_index),
        ]
        if self.artifact_kind == "workflow":
            from agenthicc.tools.fs.agent_tools import write_file

            if write_file not in tools:
                tools.append(write_file)
        return tools

    def _generation_prompt(self, intent: str) -> str:
        """Return the direct source-generation contract for ``create_workflow``."""

        if self.artifact_kind == "workflow":
            return f"""You are the read-only DESIGN agent in the built-in
agenthicc ``create_workflow`` workflow.

Produce the complete implementation specification for one custom specialized
workflow from the user's intent. Do not generate Python source, call
``write_file``, call ``batch_write``, execute shell commands, or modify the
workspace. Inspect only the current contracts needed to make the specification
accurate.

Use the built-in ``inspect_agenthicc_documentation(path)`` and
``inspect_agenthicc_source(module, symbol)`` tools when current API details are
needed. Failed optional reads are recoverable and must not become a reason to
avoid the design handoff.

The specification must name the workflow, define its phase graph, and provide
complete self-contained prompts, tools, inputs, outputs, verification behavior,
safety boundaries, completion signals, handoffs, and activation notes for every
generated phase. Use only existing agenthicc APIs and configured integrations.

When the specification is complete, call ``complete_design_phase(summary)``.
That phase transition is the only accepted design handoff and moves the run to
the EXECUTE phase. The execute agent will generate and write the source; the
runner never copies assistant response text, parses, validates, stages, or
publishes the workflow.

USER INTENT:
{intent}
{self._phase_prompt("design")}
"""

        return f"""You are the implementation agent in the design phase of the built-in
agenthicc ``create_workflow`` workflow.

Your output is the complete Python source for one custom specialized workflow.
Do not return a plan, pseudocode, a runner skeleton, or advice for another
agent to finish. Generate the source directly from the user's intent.

There are two workflow layers:

1. ``create_workflow`` is the authoring workflow. It interprets the user's
   intent, asks you to generate the artifact, and gives you ownership of writing
   the completed workflow with the canonical ``write_file`` tool.
2. Your generated workflow is the specialized runtime workflow. Later runtime
   agents execute its phases. The generated runner should orchestrate phases;
   the generated phase prompts must contain the specialized behavior.

Inspect the repository before generating source. Read these current contracts
when available:

- ``agenthicc.workflows.plugin``
- ``agenthicc.workflows.default.runner``
- ``agenthicc.workflows.base_runner``
- ``agenthicc.workflows.code_plan.definition`` and ``phase_tools``
- ``docs/guides/workflows.md``
- ``docs/guides/custom-workflows-and-config.md``
- ``docs/guides/command-execution.md`` when commands or services are involved

Use the built-in read-only authoring tools before writing code:
``inspect_agenthicc_documentation(path)`` reads the installed documentation and
``inspect_agenthicc_source(module, symbol)`` uses Python introspection to show
the API surface, signatures, and current source. Prefer those tools over
guessing from memory; the installed package is authoritative. Inspect the
specific loader, PhaseSpec, runner, and tool contracts needed by this request.

Use only existing agenthicc APIs and preserve the repository's ownership,
capability, approval, workspace, network, and activation boundaries.

ARCHITECTURE CHOICE

Prefer a declarative workflow:

- define one ``WorkflowPlugin``;
- define a literal ``PhaseSpec`` list; and
- rely on the inherited ``WorkflowPlugin.build_runner()``.

Do not add ``run()``, ``resume()``, or ``build_runner()`` merely as boilerplate.

Use a custom runner only when the intent requires runtime orchestration that a
literal PhaseSpec graph cannot express, such as context transformation,
post-processing, or intentionally extending a specialized runner. A custom
runner may implement ``run()`` and ``resume()`` directly. It may delegate to
``super()`` only when it intentionally reuses parent lifecycle behavior, such
as the existing ``code_plan_docs`` composition. Never require or generate a
no-op wrapper solely to satisfy validation.

Do not claim that changing ``CodePlan.phases`` changes the specialized
``CodePlanRunner`` state machine. If the request truly extends ``code_plan``,
use the documented ``CodePlanRunner``/``run_phase()`` composition pattern and
wire it through ``build_runner()``.

PHASE INSTRUCTIONS

Every generated ``PhaseSpec`` must contain a non-empty literal
``system_prompt_override``. This is the runtime instruction that the later
agent follows. Write the implementation instructions into the generated Python
source; do not leave them only in this response. Cover all eight instruction
areas below for every phase, omitting an area only when it genuinely does not
apply and stating the applicable boundary explicitly.

Each phase prompt must be self-contained and explicitly state, as applicable:

1. the phase's exact objective;
2. the specialized workflow behavior and required output;
3. the exact files, APIs, tools, MCP services, or commands to use;
4. what inputs and outputs it receives from earlier phases;
5. the exact code or artifacts it must create or modify;
6. how it verifies success and handles failure;
7. which completion, approval, or review signal it must call; and
8. what structured information it must hand off to the next phase.

The generic runner supplies the runtime user's task and WorkflowContext. The
phase prompt must explicitly explain how the agent uses that task and which
prior phase outputs it consumes. Do not rely on the phase name, workflow
description, undocumented conventions, or a later phase to infer behavior.
Do not use vague instructions such as "continue the implementation", "handle
the task", or "do the appropriate work".

Use the smallest phase graph that fully implements the intent. Use explicit
roles, capabilities, transitions, bounded retries, mode overrides, command
lifecycle settings, readiness gates, and completion/approval tools when needed.
Do not invent tool names or MCP integrations; if a requested integration is
not configured, instruct the runtime agent to report that prerequisite clearly.

CONFIGURATION

When configurable behavior is requested, use a typed ``WorkflowParams``
dataclass, ``get_phase_models()``, and ``build_params(source)`` for the merged
``[workflows.<name>]`` table. A parameter has an effect only if the selected
runner consumes it. Provider, credentials, and ``base_url`` are session-wide;
do not invent per-phase provider switching.

If configuration is needed, put a clearly labeled copy-ready ``agenthicc.toml``
template in the generated module docstring or comments. This authoring run
writes only the Python workflow artifact through the canonical filesystem tool.
Never include API keys or tokens, claim that TOML was written, silently edit
configuration, install dependencies, or bypass explicit activation.

SAFETY AND SOURCE CONTRACT

- Generate exactly one complete Python workflow source file.
- Define exactly one top-level ``WorkflowPlugin`` subclass.
- Use a literal list or tuple of ``PhaseSpec`` calls.
- Keep phase names and transitions valid and bounded.
- Do not use eval, exec, compile, __import__, os.system, subprocess, ctypes,
  import-time side effects, or unsafe filesystem/process bypasses.
- Do not use shell commands or an unguarded filesystem API to write the workflow.
  Use the configured canonical ``write_file`` tool with a path inside
  ``.agenthicc/workflows``.
- Do not generate extra tools, commands, tests, or files.
- Do not expose secrets or claim a generated integration is configured when it
  is not.

Before returning, verify that every user requirement maps to a phase objective,
prompt, tool/API, expected output, success criterion, and handoff. Verify the
source, class-level workflow name and description, phase references, runner
choice, and activation notes.

Write the complete raw Python source file directly with the canonical filesystem
tool. In the normal authoring run, make exactly one complete ``write_file`` call
with:

``path=".agenthicc/workflows/<stable_workflow_name>.py"``
``content=<the complete Python source>``

The content argument must contain the entire source beginning with imports and
including the full ``WorkflowPlugin`` class, phase specifications, prompts, and
any justified custom runner. Do not wrap the code: it must not contain prose, a
plan, a patch, XML, JSON, Markdown fences, or another envelope. Wait for a
successful write result,
then immediately call
``complete_design_phase(summary, artifact_name, artifact_description)``. If the
write fails or is interrupted, retry the complete write before calling the
transition. The runner never copies assistant response text, publishes files,
parses source, statically validates source, or requests end-user approval.


USER INTENT:
{intent}
{self._phase_prompt("design")}
"""

    @staticmethod
    def _artifact_ready_transition_error(
        ctx: AuthoringContext,
    ) -> tuple[str, str] | None:
        """Return actionable feedback when a phase lacks its artifact handoff."""

        if ctx.candidate is None:
            return "no candidate exists", "return to design and generate the source"
        if not ctx.report.valid:
            return (
                "the candidate has blocking validation findings",
                "repair the design candidate before requesting staging",
            )
        return None

    def _validation_transition_error(self, ctx: AuthoringContext) -> tuple[str, str] | None:
        """Ensure validation hands off the same immutable staged artifact."""

        failure = self._artifact_ready_transition_error(ctx)
        if failure is not None:
            return failure
        if ctx.artifact is None:
            return "the staged artifact is missing", "return to staging and create the manifest"
        staged = Path(ctx.artifact.staged_path)
        if not staged.is_file():
            return (
                "the staged source file is missing",
                "do not publish; return to staging and recreate the run-scoped artifact",
            )
        try:
            current_digest = source_sha256(staged.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            return (
                f"the staged source could not be read: {exc}",
                "keep the artifact unchanged and retry validation after restoring the staged file",
            )
        if current_digest != ctx.artifact.sha256:
            return (
                "the staged source changed after design validation",
                "discard this handoff and regenerate or restage the unchanged candidate",
            )
        return None

    def _publish_transition_error(self, ctx: AuthoringContext) -> tuple[str, str] | None:
        """Ensure publication cannot advance without an explicit approval gate."""

        if not ctx.approval_granted:
            return (
                "explicit publication approval is missing",
                "complete the review phase and call request_publication_approval()",
            )
        if ctx.artifact is None or ctx.artifact.state != "staged":
            return (
                "the candidate is not in the staged state",
                "use the existing staged manifest and do not regenerate or publish manually",
            )
        return self._validation_transition_error(ctx)

    async def _generate(
        self,
        intent: str,
        *,
        max_agent_turns: int | None = None,
        shared_memory: ShortTermMemory | None = None,
    ) -> tuple[WorkflowCandidate | None, ValidationReport, int, str]:
        from agenthicc.workflows.authoring.inspection_tools import (
            make_authoring_inspection_tools,
        )
        from agenthicc.workflows.authoring.phase_tools import make_authoring_transition_tools

        feedback = ""
        last_report = ValidationReport()
        last_text = ""
        attempt_limit = self._generation_attempt_limit()
        for attempt in range(1, attempt_limit + 1):
            transition_event = asyncio.Event()
            output: list[str] = _PhaseOutput(transition_event)
            transition_data: dict[str, object] = {}
            prompt = self._generation_prompt(intent)
            if feedback:
                prompt += (
                    "\n\nRECOVERY ATTEMPT\n"
                    f"This is correction attempt {attempt} of {attempt_limit}.\n"
                    f"{feedback}\n"
                )
            await self._run_authoring_turn(
                prompt,
                phase_name="design",
                tools=[
                    *self._phase_tools(),
                    *make_authoring_inspection_tools(),
                    *make_authoring_transition_tools(
                        "design",
                        transition_event,
                        transition_data,
                    ),
                ],
                active_agent="planner",
                system_prompt=(
                    "You are the create_workflow design agent. Write the complete "
                    "workflow source with the canonical write_file tool inside "
                    ".agenthicc/workflows, then complete the design handoff. The "
                    "runner never copies assistant text into the project."
                    if self.artifact_kind == "workflow"
                    else (
                        "You are generating source for a staged user extension. "
                        f"Never write directly to .agenthicc/{self.destination_dir}."
                    )
                ),
                output=output,
                max_agent_turns=max_agent_turns,
                shared_memory=shared_memory,
            )
            last_text = (
                "".join(output) if self.artifact_kind == "workflow" else "".join(output).strip()
            )
            if self.artifact_kind == "workflow":
                if transition_event.is_set():
                    self._design_metadata = dict(transition_data)
                    return None, self._unvalidated_report(), attempt, last_text
                last_report = ValidationReport(
                    (
                        ValidationFinding(
                            "phase-transition",
                            "The design agent must write the complete workflow with "
                            "write_file and then call complete_design_phase().",
                        ),
                    )
                )
                feedback = (
                    "The design handoff was not received. Use write_file to write the "
                    "complete Python source to .agenthicc/workflows/<name>.py, wait "
                    "for a successful result, then call "
                    "complete_design_phase(summary, artifact_name, artifact_description). "
                    "The runner will not copy or publish assistant response text."
                )
                if attempt < attempt_limit:
                    self._emit_phase_retry("workflow design", attempt, attempt_limit, feedback)
                continue
            if self._phase_specs and not transition_event.is_set():
                try:
                    fallback_candidate = self._parse_candidate(last_text)
                except ValueError:
                    fallback_candidate = None
                if fallback_candidate is not None:
                    fallback_report = self._validate_candidate(fallback_candidate)
                    if fallback_report.valid:
                        self._cfg.conv_store.append_event(
                            "system",
                            {
                                "text": (
                                    "Design produced a complete statically valid source artifact, "
                                    "but omitted complete_design_phase(); advancing through the "
                                    "validated-source fallback."
                                )
                            },
                        )
                        return fallback_candidate, fallback_report, attempt, last_text
                parse_error = str(
                    transition_data.get("last_error")
                    or (
                        "the design agent did not call complete_design_phase(); "
                        "a phase cannot advance from free-form text"
                    )
                )
                last_report = ValidationReport(
                    (ValidationFinding("phase-transition", parse_error),)
                )
                feedback = self._generation_feedback(
                    last_report,
                    parse_error=parse_error,
                    phase_transition_required=True,
                )
                if attempt < attempt_limit:
                    self._cfg.conv_store.append_event(
                        "system",
                        {
                            "text": (
                                f"{self.artifact_label.title()} design phase attempt "
                                f"{attempt}/{attempt_limit} did not call its transition "
                                "tool; retrying. "
                                f"{feedback}"
                            )
                        },
                    )
                continue
            try:
                candidate = self._parse_candidate(last_text)
            except ValueError as exc:
                finding = ValidationFinding("response-parse", str(exc))
                last_report = ValidationReport((finding,))
                feedback = self._generation_feedback(
                    last_report,
                    parse_error=str(exc),
                    phase_transition_required=bool(self._phase_specs),
                )
                if attempt < attempt_limit:
                    self._cfg.conv_store.append_event(
                        "system",
                        {
                            "text": (
                                f"{self.artifact_label.title()} source generation attempt "
                                f"{attempt}/{attempt_limit} needs correction; retrying. "
                                f"{feedback}"
                            )
                        },
                    )
                continue
            last_report = self._validate_candidate(candidate)
            if last_report.valid:
                return candidate, last_report, attempt, last_text
            feedback = self._generation_feedback(last_report)
            if attempt < attempt_limit:
                self._cfg.conv_store.append_event(
                    "system",
                    {
                        "text": (
                            f"{self.artifact_label.title()} source generation attempt "
                            f"{attempt}/{attempt_limit} failed validation; retrying. "
                            f"{feedback}"
                        )
                    },
                )
        return None, last_report, attempt_limit, last_text

    def _load_staged_artifact(
        self, run_id: str
    ) -> tuple[WorkflowCandidate, ValidationReport, AuthoringArtifact, str, str]:
        """Load and revalidate one run-scoped staging manifest."""

        if self.artifact_kind == "workflow":
            raise ValueError(
                "create_workflow has no runner-owned staged artifact; its agent writes directly"
            )
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("authoring run id is invalid")
        root = WorkspaceView(self._project_root)
        stage_dir = root.resolve(Path(".agenthicc") / "authoring" / run_id)
        manifest_path = root.resolve(stage_dir / "manifest.json")
        try:
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"authoring manifest not found for run {run_id!r}") from exc
        if not isinstance(manifest_value, dict):
            raise ValueError("authoring manifest must be a JSON object")

        def required_string(key: str) -> str:
            value = manifest_value.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"authoring manifest field {key!r} must be a non-empty string")
            return value

        if required_string("workflow") != self.workflow_name:
            raise ValueError("authoring manifest belongs to a different workflow")
        if required_string("run_id") != run_id:
            raise ValueError("authoring manifest run id does not match the resume request")
        if self.artifact_kind != "workflow" and not re.fullmatch(
            r"[a-z][a-z0-9_]{1,63}", required_string("name")
        ):
            raise ValueError("authoring manifest contains an invalid artifact name")
        name = required_string("name")
        if required_string("artifact_kind") != self.artifact_kind:
            raise ValueError("authoring manifest belongs to a different artifact kind")
        description = manifest_value.get("description", "")
        if not isinstance(description, str):
            raise ValueError("authoring manifest description must be a string")
        expected_stage = root.resolve(
            Path(".agenthicc") / self.destination_dir / f"{name}.py"
            if self.artifact_kind == "workflow"
            else stage_dir / f"{name}.py"
        )
        staged_path = root.resolve(required_string("staged_path"))
        if staged_path != expected_stage:
            raise ValueError("authoring staged path does not match the run manifest")
        expected_destination = root.resolve(
            Path(".agenthicc") / self.destination_dir / f"{name}.py"
        )
        destination = root.resolve(required_string("destination"))
        if destination != expected_destination:
            raise ValueError("authoring destination does not match the run manifest")
        source = staged_path.read_text(encoding="utf-8")
        digest = required_string("sha256")
        if self.artifact_kind != "workflow" and source_sha256(source) != digest:
            raise ValueError(f"staged {self.artifact_label} changed after its last validation")
        if self.artifact_kind == "workflow":
            digest = source_sha256(source)
        candidate = WorkflowCandidate(name=name, code=source, description=description)
        report = (
            self._unvalidated_report()
            if self.artifact_kind == "workflow"
            else self._validate_candidate(candidate)
        )
        if self.artifact_kind != "workflow" and not report.valid:
            raise ValueError(self._validation_text(report))
        state = manifest_value.get("state", "staged")
        if not isinstance(state, str) or state not in {"staged", "published"}:
            raise ValueError("authoring manifest has an unsupported artifact state")
        if state == "published":
            published_path = manifest_value.get("published_path")
            if not isinstance(published_path, str) or root.resolve(published_path) != destination:
                raise ValueError("published authoring manifest has an invalid destination")
            if (
                not destination.exists()
                or source_sha256(destination.read_text(encoding="utf-8")) != digest
            ):
                raise ValueError(f"published {self.artifact_label} no longer matches its manifest")
        manifest_intent = manifest_value.get("intent", "")
        if not isinstance(manifest_intent, str):
            raise ValueError("authoring manifest intent must be a string")
        artifact = AuthoringArtifact(
            name=name,
            state=state,
            staged_path=str(staged_path),
            published_path=str(destination) if state == "published" else None,
            sha256=digest,
            validation=report,
            manifest_path=str(manifest_path),
        )
        return candidate, report, artifact, state, manifest_intent

    async def _start_phase(self, name: str) -> None:
        from agenthicc.kernel import Event

        if self._workflow_run is None:
            return
        self._state = state_for_phase(name)
        index = self._phase_names().index(name)
        self._workflow_run = dataclasses.replace(
            self._workflow_run,
            current_phase=name,
            current_phase_index=index,
        )
        self._cfg.app_state.workflow_run.set(self._workflow_run)
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowPhaseStarted",
                {
                    "run_id": self._run_id,
                    "phase_name": name,
                    "workflow_name": self.workflow_name,
                    "state": self._state.name,
                },
            )
        )

    async def _complete_phase(
        self,
        name: str,
        text: str,
        *,
        approved: bool | None = None,
        structured: dict[str, object] | None = None,
    ) -> None:
        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import PhaseRunRecord

        if self._workflow_run is None:
            return
        if self._workflow_run.current_phase != name:
            await self._start_phase(name)
        phase_spec = self._phase_specs.get(name)
        record = PhaseRunRecord(
            phase_name=name,
            role=(
                "human"
                if name == "review"
                else phase_spec.agent_type
                if phase_spec is not None
                else "auto"
            ),
            approved=approved,
            output_summary=text[:500],
            iteration=1,
            duration_s=0.0,
        )
        self._workflow_run = dataclasses.replace(
            self._workflow_run,
            phase_history=self._workflow_run.phase_history + [record],
        )
        self._cfg.app_state.workflow_run.set(self._workflow_run)
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowPhaseCompleted",
                {
                    "run_id": self._run_id,
                    "phase_name": name,
                    "workflow_name": self.workflow_name,
                    "role": record.role,
                    "full_text": text[:8_000],
                    "approved": approved,
                    "structured": structured or {},
                    "state": state_for_phase(name).name,
                },
            )
        )

    async def _finish_run(self, result: AuthoringResult, *, status: str) -> None:
        from agenthicc.kernel import Event

        self._state = (
            AuthoringState.COMPLETE
            if status == "complete"
            else AuthoringState.REJECTED
            if result.status == "rejected"
            else AuthoringState.FAILED
        )

        # Emit the terminal summary as a normal transcript text event so the
        # user always gets a visible response instead of an apparently idle UI.
        if not self._summary_emitted:
            summary = result.summary or self._summary(result)
            self._cfg.conv_store.append_event("text", {"text": summary})
            self._summary_emitted = True

        if self._workflow_run is not None:
            self._workflow_run = dataclasses.replace(
                self._workflow_run,
                status=status,
                current_phase=None,
            )
            self._cfg.app_state.workflow_run.set(self._workflow_run)
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunCompleted",
                {
                    "run_id": self._run_id,
                    "workflow_name": self.workflow_name,
                    "phases_run": len(self._workflow_run.phase_history)
                    if self._workflow_run
                    else 0,
                    "status": status,
                    "state": self._state.name,
                    "result": result.to_dict(),
                },
            )
        )

    async def _stage_artifact(
        self,
        candidate: WorkflowCandidate,
        report: ValidationReport,
        intent: str,
    ) -> AuthoringArtifact:
        root = WorkspaceView(self._project_root)
        stage_dir = root.resolve(Path(".agenthicc") / "authoring" / self._run_id)
        stage_dir.mkdir(parents=True, exist_ok=True)
        stage_path = stage_dir / f"{candidate.name}.py"
        root.write_text(stage_path, candidate.code.rstrip() + "\n")
        digest = source_sha256(candidate.code.rstrip() + "\n")
        manifest_path = stage_dir / "manifest.json"
        artifact = AuthoringArtifact(
            name=candidate.name,
            state="staged",
            staged_path=str(stage_path),
            published_path=None,
            sha256=digest,
            validation=report,
            manifest_path=str(manifest_path),
        )
        manifest = {
            "workflow": self.workflow_name,
            "run_id": self._run_id,
            "intent": intent,
            "artifact_kind": self.artifact_kind,
            "name": candidate.name,
            "description": candidate.description,
            "staged_path": str(stage_path),
            "destination": str(Path(".agenthicc") / self.destination_dir / f"{candidate.name}.py"),
            "state": "staged",
            "published_path": None,
            "sha256": digest,
            "validation": report.to_dict(),
        }
        root.write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        return artifact

    async def _request_publication_approval(
        self,
        artifact: AuthoringArtifact,
        candidate: WorkflowCandidate,
    ) -> bool:
        if self._cfg.approval_svc is None:
            return False
        from agenthicc.tools.approval import ApprovalRequest

        destination = Path(".agenthicc") / self.destination_dir / f"{candidate.name}.py"
        request = ApprovalRequest(
            tool_name=f"publish_{self.artifact_kind}",
            tool_use_id=uuid.uuid4().hex,
            tool_input={
                "destination": str(destination),
                "staged_path": artifact.staged_path,
                "overwrite": (self._project_root / destination).exists(),
                "preview": candidate.code[:4_000],
            },
            capabilities=frozenset({"write"}),
            event=asyncio.Event(),
            kind="authoring_review",
        )
        response = await self._cfg.approval_svc.request_approval(request)
        return response.allowed

    def _publish_artifact(
        self,
        artifact: AuthoringArtifact,
        candidate: WorkflowCandidate,
    ) -> AuthoringArtifact:
        root = WorkspaceView(self._project_root)
        destination = root.resolve(
            Path(".agenthicc") / self.destination_dir / f"{candidate.name}.py"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = root.resolve(artifact.staged_path)
        current = source.read_text(encoding="utf-8")
        if source_sha256(current) != artifact.sha256:
            raise ValueError(f"staged {self.artifact_label} changed after validation")
        temporary = destination.parent / f".{candidate.name}.{self._run_id}.tmp"
        root.write_text(temporary, current)
        os.replace(temporary, destination)
        if artifact.manifest_path is None:
            raise ValueError(f"staged {self.artifact_label} has no manifest path")
        manifest_path = root.resolve(artifact.manifest_path)
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_value, dict):
            raise ValueError("authoring manifest must be a JSON object")
        manifest_value["state"] = "published"
        manifest_value["published_path"] = str(destination)
        root.write_text(manifest_path, json.dumps(manifest_value, indent=2) + "\n")
        return dataclasses.replace(
            artifact,
            state="published",
            published_path=str(destination),
        )

    def _validation_text(self, report: ValidationReport, *, attempts: int | None = None) -> str:
        if report.valid:
            return f"{self.artifact_label.title()} source validation passed."
        message = f"{self.artifact_label.title()} source validation failed: " + "; ".join(
            item.message for item in report.findings
        )
        if attempts is not None and attempts > 1:
            message += f" Generation stopped after {attempts} attempts."
        return message

    def _summary(self, result: AuthoringResult) -> str:
        if self.artifact_kind == "workflow" and result.status == "complete":
            if result.artifact is not None:
                return (
                    f"Agent wrote workflow {result.artifact.name!r} to "
                    f"{result.artifact.staged_path}. {self._activation_message(result.artifact.name)} "
                    "The runner did not copy, publish, parse, or validate the source."
                )
            return (
                "The execute agent completed its handoff, but did not report a valid "
                "workflow filename. The runner did not copy, publish, parse, or validate "
                "assistant output."
            )
        if result.status == "published" and result.artifact is not None:
            return (
                f"Created {self.artifact_label} {result.artifact.name!r} at "
                f"{result.artifact.published_path}. {self._activation_message(result.artifact.name)}"
            )
        return result.error or (
            f"{self.artifact_label.title()} authoring ended with status {result.status}."
        )

    def _activation_message(self, name: str) -> str:
        """Describe the explicit activation step for the generated artifact."""

        return f"Run /workflows reload, then use /workflow {name}."


class CreateToolRunner(CreateWorkflowRunner):
    """Shared authoring lifecycle specialized for ``TOOLS`` modules."""

    workflow_name = "create_tools"
    artifact_kind = "tool"
    destination_dir = "tools"
    artifact_label = "tool"
    activation = "tools-reload"

    def _generation_prompt(self, intent: str) -> str:
        return f"""You are the implementation agent in the design phase of the built-in
agenthicc ``create_tools`` workflow.

Generate the complete Python source for exactly one project-local lauren-ai
tool module from the user's intent. Do not return a plan, pseudocode, a tool
skeleton, or advice for another agent. Write the implementation directly.

Inspect the current tool guides, lauren-ai decorator and context conventions,
capability metadata, HTTP helpers, relevant loaders, and tests before choosing
an implementation. Use only existing agenthicc APIs. Keep filesystem,
network, approval, dependency, and output behavior inside the repository's
existing ownership and security boundaries.

Before generating source, call the built-in
``inspect_agenthicc_documentation(path)`` and
``inspect_agenthicc_source(module, symbol)`` tools. Use Python introspection
against the installed agenthicc modules to confirm the latest decorator,
context, capability, loader, and HTTP APIs rather than relying on stale
examples.

SOURCE CONTRACT

- Generate the complete raw Python source directly in the assistant response.
  Return exactly one raw source file without analysis or commentary. Do not use XML, JSON,
  Markdown fences, or another response envelope. After the source is complete,
  immediately call complete_design_phase(summary). The runner captures and
  validates the response before staging it; do not write to the discoverable
  tools directory yourself.
- Set literal module metadata ``ARTIFACT_NAME = "lowercase_module_name"`` and
  ``ARTIFACT_DESCRIPTION = "short description"``. The authoring lifecycle uses
  these values to choose the staged and published filename when no legacy
  envelope supplies metadata.
- Import Lauren's ``@tool`` decorator and export every public callable through
  one literal ``TOOLS`` list or tuple. Use accurate annotations and bounded,
  structured return values.
- Use ``ToolContext`` only when the tool needs runtime metadata or guarded
  resources. Use ``WorkspaceView`` for filesystem paths, the existing network
  guard and ``agenthicc_http_client()`` for HTTP, and capability decorators
  that match the operation. Do not invent integrations or claim unavailable
  MCP services are configured.
- Handle malformed input, timeouts, transient network errors, missing
  prerequisites, and repeated calls with explicit, recoverable results where
  the contract permits. Do not log credentials or unbounded remote content.
- Keep imports side-effect free. Do not install packages, edit configuration,
  write directly to ``.agenthicc/tools``, execute shell text, or create extra
  files, commands, workflows, tests, or dependencies during this turn.

Before returning, verify the metadata, decorator, TOOLS export, annotations,
capability boundary, error handling, and loader compatibility. Return the
source directly even when the requested tool requires a configured external
service; report that prerequisite at runtime instead of fabricating it.


USER INTENT:
{intent}
{self._phase_prompt("design")}
"""

    def _activation_message(self, name: str) -> str:
        return "Run /tools reload, then ask the agent to use the generated tool."


class CreateCommandRunner(CreateWorkflowRunner):
    """Shared authoring lifecycle specialized for ``Command`` modules."""

    workflow_name = "create_commands"
    artifact_kind = "command"
    destination_dir = "commands"
    artifact_label = "command"
    activation = "commands-reload"

    def _generation_prompt(self, intent: str) -> str:
        return f"""You are the implementation agent in the design phase of the built-in
agenthicc ``create_commands`` workflow.

Generate the complete Python source for exactly one project-local slash-command
module from the user's intent. Do not return a plan, pseudocode, a command
skeleton, or advice for another agent. Write the implementation directly.

Inspect the current ``agenthicc.commands.Command`` and ``CommandContext``
contracts, command plugin loader, dispatcher, picker behavior, busy policies,
relevant guides, and tests before choosing an implementation. Use only existing
agenthicc APIs and preserve command ownership, capability, approval, and busy
state boundaries.

Before generating source, call the built-in
``inspect_agenthicc_documentation(path)`` and
``inspect_agenthicc_source(module, symbol)`` tools. Use Python introspection
against the installed agenthicc modules to confirm the latest command,
context, loader, dispatcher, and busy-policy APIs rather than relying on stale
examples.

SOURCE CONTRACT

- Generate the complete raw Python source directly in the assistant response.
  Return exactly one raw source file without analysis or commentary. Do not use XML, JSON,
  Markdown fences, or another response envelope. After the source is complete,
  immediately call complete_design_phase(summary). The runner captures and
  validates the response before staging it; do not write to the discoverable
  commands directory yourself.
- Set literal module metadata ``ARTIFACT_NAME = "lowercase_module_name"`` and
  ``ARTIFACT_DESCRIPTION = "short description"``. The authoring lifecycle uses
  these values to choose the staged and published filename when no legacy
  envelope supplies metadata.
- Import ``Command`` and ``CommandContext`` from the canonical command module.
  Export exactly one literal ``COMMAND`` or ``COMMANDS`` value containing
  validated ``Command`` objects. Use a synchronous handler returning ``bool``
  or a menu factory with the documented context contract; do not export both
  forms for one command unless the existing contract explicitly requires it.
- Parse arguments from ``ctx.args`` and use context-owned console, registry,
  and callbacks. Give every command a slash-prefixed literal name, literal
  description, intentional group, aliases, and argument hint when applicable.
- Do not execute arbitrary shell text, make import-time network requests,
  install dependencies, edit configuration, write directly to
  ``.agenthicc/commands``, or create extra files, tools, workflows, or tests.
  Keep command behavior bounded, explicit, and safe while a run is active.

Before returning, verify the metadata, command export shape, literal command
names and descriptions, handler or menu contract, argument behavior, busy
policy, and loader compatibility. Return the source directly even when the
requested command depends on a prerequisite; report that prerequisite through
the command's normal bounded behavior.


USER INTENT:
{intent}
{self._phase_prompt("design")}
"""

    def _activation_message(self, name: str) -> str:
        return "Run /commands reload, then invoke the generated slash command."
