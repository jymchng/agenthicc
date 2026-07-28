"""Shared runner for workflow, tool, and command authoring (PRD-147)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import uuid
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
    parse_workflow_response,
    source_sha256,
    validate_command_candidate,
    validate_tool_candidate,
    validate_workflow_candidate,
)
from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.authoring.state import AuthoringState, state_for_phase

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowRun
    from agenthicc.workflows.plugin import PhaseSpec
    from agenthicc.tools.base import ToolLike

log = logging.getLogger(__name__)

_PHASES = ("interpret", "design", "stage", "validate", "review", "publish", "summarize")
_DEFAULT_MAX_GENERATION_ATTEMPTS = 3
_MAX_GENERATION_ATTEMPTS = 10
_DEFAULT_MAX_PHASE_TURNS = 20
_MAX_PHASE_TURNS = 100
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class CreateWorkflowRunner(BaseWorkflowRunner):
    """Generate, validate, approve, and publish one executable artifact.

    The lifecycle is shared by the three built-in authoring workflows. Concrete
    subclasses only select the artifact contract, destination, and prompt.
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
        self._state = AuthoringState.INTERPRET

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
        """Run the complete authoring lifecycle for *intent*."""

        if not intent.strip():
            raise ValueError(f"{self.artifact_label.title()} authoring intent must not be empty")
        from lauren_ai._memory import ShortTermMemory
        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import WorkflowRun

        self._run_id = uuid.uuid4().hex
        self._summary_emitted = False
        self._state = AuthoringState.INTERPRET
        self._shared_memory = ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        self._workflow_run = WorkflowRun(
            run_id=self._run_id,
            workflow_name=self.workflow_name,
            intent=intent,
            current_phase="interpret",
            total_phases=len(_PHASES),
        )
        self._cfg.app_state.workflow_run.set(self._workflow_run)
        await self._cfg.processor.emit(
            Event.create(
                "WorkflowRunStarted",
                {
                    "run_id": self._run_id,
                    "workflow_name": self.workflow_name,
                    "intent": intent,
                    "phase_names": list(_PHASES),
                },
            )
        )
        await self._start_phase("interpret")

        artifact: AuthoringArtifact | None = None
        result: AuthoringResult
        try:
            interpreted_intent = intent
            if self._phase_specs:
                interpreted_intent = await self._run_interpret_phase(intent)
            await self._complete_phase(
                "interpret",
                interpreted_intent,
                approved=True,
                structured={"intent": intent, "interpretation": interpreted_intent},
            )
            await self._start_phase("design")
            candidate, report, attempts, generation_text = await self._generate(interpreted_intent)
            await self._complete_phase(
                "design",
                generation_text,
                approved=report.valid,
                structured={"attempts": attempts},
            )
            await self._start_phase("stage")
            if candidate is not None and report.valid:
                artifact = await self._stage(candidate, report, intent)
                await self._complete_phase(
                    "stage",
                    f"Staged {artifact.staged_path}.",
                    approved=True,
                    structured=artifact.to_dict(),
                )
            else:
                await self._complete_phase(
                    "stage",
                    f"No artifact staged because generation did not produce a valid {self.artifact_label}.",
                    approved=False,
                )
            await self._complete_phase(
                "validate",
                self._validation_text(report, attempts=attempts),
                approved=report.valid,
                structured=report.to_dict(),
            )
            if candidate is None or not report.valid:
                result = self._result(
                    run_id=self._run_id,
                    status="failed",
                    approval="not-requested",
                    error=self._validation_text(report, attempts=attempts),
                    attempts=attempts,
                )
                await self._complete_phase("summarize", result.error or "Generation failed")
                await self._finish_run(result, status="failed")
                return result

            if artifact is None:
                raise RuntimeError(f"valid {self.artifact_label} candidate was not staged")
            await self._start_phase("review")
            approval = await self._request_publication_approval(artifact, candidate)
            if not approval:
                result = self._result(
                    run_id=self._run_id,
                    status="rejected",
                    artifact=dataclasses.replace(artifact, state="staged"),
                    approval="denied",
                    activation=None,
                    error="Publication approval was denied; the candidate remains staged.",
                    attempts=attempts,
                )
                denial_text = result.error or "Publication approval was denied."
                await self._complete_phase("review", denial_text, approved=False)
                await self._complete_phase("summarize", denial_text)
                await self._finish_run(result, status="failed")
                return result

            await self._complete_phase("review", "Publication approved.", approved=True)
            await self._start_phase("publish")
            published = self._publish(artifact, candidate)
            result = self._result(
                run_id=self._run_id,
                status="published",
                artifact=published,
                approval="approved",
                activation=self.activation,
                attempts=attempts,
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
                    run_id=self._run_id,
                    status="cancelled",
                    artifact=artifact,
                    error="Workflow authoring was cancelled.",
                ),
                status="failed",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("%s authoring failed", self.workflow_name)
            result = self._result(
                run_id=self._run_id,
                status="failed",
                artifact=artifact,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._finish_run(result, status="failed")
            return result

    async def resume(self, context: object) -> AuthoringResult:
        """Resume a staged run without regenerating or duplicating side effects.

        The manifest and staged source are authoritative.  Resume revalidates
        the source and then continues at the approval/publication boundary.
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
                current_phase="review",
                total_phases=len(_PHASES),
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
                        "phase_names": list(_PHASES),
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
            published = self._publish(artifact, candidate)
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
                    total_phases=len(_PHASES),
                )
                self._cfg.app_state.workflow_run.set(self._workflow_run)
            await self._finish_run(result, status="failed")
            return result

    def _parse_candidate(self, text: str) -> WorkflowCandidate:
        """Parse the model source response for this artifact kind."""

        if self.artifact_kind == "workflow":
            return parse_workflow_response(text)
        return parse_authoring_response(text, self.artifact_kind)

    def _validate_candidate(self, candidate: WorkflowCandidate) -> ValidationReport:
        """Validate the model candidate using the selected export contract."""

        if self.artifact_kind == "workflow":
            return validate_workflow_candidate(candidate)
        if self.artifact_kind == "tool":
            return validate_tool_candidate(candidate)
        return validate_command_candidate(candidate)

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
        return (
            f"\n\n[AUTHORING PHASE: {phase_name}]\n"
            f"{spec.system_prompt_override.strip()}\n"
            "The phase may use multiple agent turns. Do not advance by merely "
            "writing a conversational answer; use the phase transition tool when "
            "the objective is complete."
        )

    def _generation_feedback(
        self,
        report: ValidationReport,
        *,
        parse_error: str | None = None,
    ) -> str:
        """Build actionable correction instructions for the next agent attempt."""

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

    async def _run_interpret_phase(self, intent: str) -> str:
        """Run the tool-gated interpretation phase for definition-backed runs."""

        from agenthicc.workflows.authoring.inspection_tools import (
            make_authoring_inspection_tools,
        )
        from agenthicc.workflows.authoring.phase_tools import make_authoring_transition_tools

        transition_event = asyncio.Event()
        transition_data: dict[str, object] = {}
        await self._run_authoring_turn(
            intent,
            phase_name="interpret",
            tools=(
                [
                    *self._cfg.all_plugin_tools(),
                    *make_authoring_inspection_tools(),
                    *make_authoring_transition_tools(
                        "interpret",
                        transition_event,
                        transition_data,
                    ),
                ]
            ),
            active_agent="planner",
            system_prompt=(
                "You are interpreting an authoring request. Inspect only the current "
                "contracts needed for this artifact, normalize the user's intent, "
                "and call complete_authoring_phase(summary) when the design handoff "
                "is precise. Do not generate source in this phase."
                + self._phase_prompt("interpret")
            ),
        )
        if not transition_event.is_set():
            raise ValueError("interpret phase ended without complete_authoring_phase()")
        summary = transition_data.get("summary")
        return summary if isinstance(summary, str) else intent

    async def _run_authoring_turn(
        self,
        text: str,
        *,
        phase_name: str,
        tools: list[ToolLike],
        active_agent: str,
        system_prompt: str,
        output: list[str] | None = None,
    ) -> None:
        """Run one bounded tool-capable turn for an authoring phase."""

        from agenthicc.runners.agent_turn import _run_agent_turn

        if self._shared_memory is None:
            raise RuntimeError("authoring shared memory is not initialized")
        await _run_agent_turn(
            text,
            runner=self._cfg.agent_runner,
            processor=self._cfg.processor,
            session_memory=self._shared_memory,
            max_agent_turns=max(
                1,
                min(self._phase_max_turns(phase_name), self._cfg.cfg.execution.max_agent_turns),
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
            memory_router=self._cfg.memory_router,
            semantic_index=self._cfg.semantic_index,
        )

    def _generation_prompt(self, intent: str) -> str:
        """Return the direct source-generation contract for ``create_workflow``."""

        return f"""You are the implementation agent in the design phase of the built-in
agenthicc ``create_workflow`` workflow.

Your output is the complete Python source for one custom specialized workflow.
Do not return a plan, pseudocode, a runner skeleton, or advice for another
agent to finish. Generate the source directly from the user's intent.

There are two workflow layers:

1. ``create_workflow`` is the authoring workflow. It interprets the user's
   intent, asks you to generate the artifact, stages and validates it, requests
   publication approval, and publishes it only after approval.
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
publishes only the Python workflow artifact. Never include API keys or tokens,
claim that TOML was published, silently edit configuration, install
dependencies, or bypass explicit activation.

SAFETY AND SOURCE CONTRACT

- Generate exactly one complete Python workflow source file.
- Define exactly one top-level ``WorkflowPlugin`` subclass.
- Use a literal list or tuple of ``PhaseSpec`` calls.
- Keep phase names and transitions valid and bounded.
- Do not use eval, exec, compile, __import__, os.system, subprocess, ctypes,
  import-time side effects, or unsafe filesystem/process bypasses.
- Do not write directly to ``.agenthicc/workflows`` during this turn.
- Do not generate extra tools, commands, tests, or files.
- Do not expose secrets or claim a generated integration is configured when it
  is not.

Before returning, verify that every user requirement maps to a phase objective,
prompt, tool/API, expected output, success criterion, and handoff. Verify the
source, class-level workflow name and description, phase references, runner
choice, and activation notes.

Generate the source directly as code. Return ONLY the complete raw Python source file:
start with the imports and include the full ``WorkflowPlugin`` class, phase
specifications, prompts, and any justified custom runner. Do not wrap the code
in XML, JSON, Markdown fences, or another special envelope. Do not add an
explanation before or after the source.

When the phase-local authoring tools are available, pass this same complete raw
source to ``submit_generated_source(source, artifact_name, artifact_description)``
and then call ``complete_authoring_phase(summary)``. The tool call is only the
handoff signal; the source itself must still be generated directly and remains
subject to static validation.

USER INTENT:
{intent}
{self._phase_prompt("design")}
"""

    async def _generate(
        self, intent: str
    ) -> tuple[WorkflowCandidate | None, ValidationReport, int, str]:
        from agenthicc.runners.agent_turn import _run_agent_turn
        from agenthicc.workflows.authoring.inspection_tools import (
            make_authoring_inspection_tools,
        )
        from agenthicc.workflows.authoring.phase_tools import make_authoring_transition_tools

        feedback = ""
        last_report = ValidationReport()
        last_text = ""
        attempt_limit = self._generation_attempt_limit()
        for attempt in range(1, attempt_limit + 1):
            output: list[str] = []
            transition_event = asyncio.Event()
            transition_data: dict[str, object] = {}
            prompt = self._generation_prompt(intent)
            if feedback:
                prompt += (
                    "\n\nRECOVERY ATTEMPT\n"
                    f"This is correction attempt {attempt} of {attempt_limit}.\n"
                    f"{feedback}\n"
                )
            await _run_agent_turn(
                prompt,
                runner=self._cfg.agent_runner,
                processor=self._cfg.processor,
                session_memory=self._shared_memory,
                max_agent_turns=max(
                    1,
                    min(
                        self._phase_max_turns("design"),
                        self._cfg.cfg.execution.max_agent_turns,
                    ),
                ),
                conv_store=self._cfg.conv_store,
                app_state=self._cfg.app_state,
                exec_cfg=self._cfg.cfg.execution,
                skills=self._cfg.skills,
                skill_permissions=self._cfg.cfg.agents.skill_permissions_for("planner"),
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=(
                    [
                        *self._cfg.all_plugin_tools(),
                        *make_authoring_inspection_tools(),
                        *make_authoring_transition_tools(
                            "design",
                            transition_event,
                            transition_data,
                        ),
                    ]
                ),
                mcp_registry=self._cfg.mcp_registry,
                active_agent="planner",
                completed_turns=self._cfg.completed_turns,
                approval_svc=self._cfg.approval_svc,
                output_collector=output,
                system_prompt_suffix=(
                    "You are generating source for a staged user extension. "
                    f"Never write directly to .agenthicc/{self.destination_dir}."
                ),
                memory_router=self._cfg.memory_router,
                semantic_index=self._cfg.semantic_index,
            )
            submitted_source = transition_data.get("source")
            last_text = (
                submitted_source.strip()
                if transition_event.is_set() and isinstance(submitted_source, str)
                else "".join(output).strip()
            )
            if self._phase_specs and not transition_event.is_set():
                parse_error = (
                    "the design agent did not call complete_authoring_phase(); "
                    "a phase cannot advance from free-form text"
                )
                last_report = ValidationReport(
                    (ValidationFinding("phase-transition", parse_error),)
                )
                feedback = self._generation_feedback(last_report, parse_error=parse_error)
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
                feedback = self._generation_feedback(last_report, parse_error=str(exc))
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
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", required_string("name")):
            raise ValueError("authoring manifest contains an invalid artifact name")
        name = required_string("name")
        if required_string("artifact_kind") != self.artifact_kind:
            raise ValueError("authoring manifest belongs to a different artifact kind")
        description = manifest_value.get("description", "")
        if not isinstance(description, str):
            raise ValueError("authoring manifest description must be a string")
        expected_stage = root.resolve(stage_dir / f"{name}.py")
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
        if source_sha256(source) != digest:
            raise ValueError(f"staged {self.artifact_label} changed after its last validation")
        candidate = WorkflowCandidate(name=name, code=source, description=description)
        report = self._validate_candidate(candidate)
        if not report.valid:
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
        index = _PHASES.index(name)
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

        # Authoring runs do not end in a normal agent response: publication is
        # completed by the workflow runner after the approval overlay closes.
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

    async def _stage(
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

    def _publish(
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

- Return only the complete raw Python source file. Do not use XML, JSON,
  Markdown fences, or any other special response envelope, and do not add
  explanation before or after the code.
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

- Return only the complete raw Python source file. Do not use XML, JSON,
  Markdown fences, or any other special response envelope, and do not add
  explanation before or after the code.
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
