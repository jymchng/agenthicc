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

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import WorkflowRun

log = logging.getLogger(__name__)

_PHASES = ("interpret", "design", "stage", "validate", "review", "publish", "summarize")
_MAX_GENERATION_ATTEMPTS = 2
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
    activation = "restart-session"

    def __init__(self, config: WorkflowConfig, mode_manager: ModeManager | None = None) -> None:
        self._cfg = config
        self._mode_manager = mode_manager
        self._run_id = ""
        self._shared_memory: ShortTermMemory | None = None
        self._workflow_run: WorkflowRun | None = None
        self._project_root = Path.cwd().resolve()

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

        return AuthoringResult(
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

    async def run(self, intent: str) -> AuthoringResult:
        """Run the complete authoring lifecycle for *intent*."""

        if not intent.strip():
            raise ValueError(f"{self.artifact_label.title()} authoring intent must not be empty")
        from lauren_ai._memory import ShortTermMemory
        from agenthicc.kernel import Event
        from agenthicc.workflows.plugin import WorkflowRun

        self._run_id = uuid.uuid4().hex
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
            await self._complete_phase(
                "interpret",
                intent,
                approved=True,
                structured={"intent": intent},
            )
            await self._start_phase("design")
            candidate, report, attempts, generation_text = await self._generate(intent)
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
                self._validation_text(report),
                approved=report.valid,
                structured=report.to_dict(),
            )
            if candidate is None or not report.valid:
                result = self._result(
                    run_id=self._run_id,
                    status="failed",
                    approval="not-requested",
                    error=self._validation_text(report),
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
        """Parse the model envelope for this artifact kind."""

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

    def _generation_prompt(self, intent: str) -> str:
        """Return the contract-specific design prompt."""

        return (
            "You are the design phase of agenthicc's create_workflow workflow.\n\n"
            "Create exactly one complete Python WorkflowPlugin for the user's intent. "
            "Inspect relevant repository modules and use existing agenthicc APIs. "
            "The workflow must be self-contained, use PhaseSpec transitions, and keep "
            "external access behind existing tools/MCP capabilities.\n\n"
            "Return ONLY this envelope, with no explanation outside it:\n"
            '<workflow name="lowercase_name" description="short description">\n'
            "```python\n"
            "from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin\n"
            "...complete source...\n"
            "```\n"
            "</workflow>\n\n"
            f"USER INTENT:\n{intent}\n"
        )

    async def _generate(
        self, intent: str
    ) -> tuple[WorkflowCandidate | None, ValidationReport, int, str]:
        from agenthicc.runners.agent_turn import _run_agent_turn

        feedback = ""
        last_report = ValidationReport()
        last_text = ""
        for attempt in range(1, _MAX_GENERATION_ATTEMPTS + 1):
            output: list[str] = []
            prompt = self._generation_prompt(intent)
            if feedback:
                prompt += f"\nThe previous candidate failed validation. Correct these findings:\n{feedback}\n"
            await _run_agent_turn(
                prompt,
                runner=self._cfg.agent_runner,
                processor=self._cfg.processor,
                session_memory=self._shared_memory,
                max_agent_turns=min(12, self._cfg.cfg.execution.max_agent_turns),
                conv_store=self._cfg.conv_store,
                app_state=self._cfg.app_state,
                exec_cfg=self._cfg.cfg.execution,
                skills=self._cfg.skills,
                skill_permissions=self._cfg.cfg.agents.skill_permissions_for("planner"),
                mention_cache=self._cfg.mention_cache,
                project_plugin_tools=self._cfg.all_plugin_tools(),
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
            last_text = "".join(output).strip()
            try:
                candidate = self._parse_candidate(last_text)
            except ValueError as exc:
                feedback = str(exc)
                last_report = ValidationReport((ValidationFinding("response-parse", str(exc)),))
                continue
            last_report = self._validate_candidate(candidate)
            if last_report.valid:
                return candidate, last_report, attempt, last_text
            feedback = "\n".join(f"- {item.message}" for item in last_report.findings)
        return None, last_report, _MAX_GENERATION_ATTEMPTS, last_text

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
        record = PhaseRunRecord(
            phase_name=name,
            role="human" if name == "review" else "auto",
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
                },
            )
        )

    async def _finish_run(self, result: AuthoringResult, *, status: str) -> None:
        from agenthicc.kernel import Event

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

    def _validation_text(self, report: ValidationReport) -> str:
        if report.valid:
            return f"{self.artifact_label.title()} source validation passed."
        return f"{self.artifact_label.title()} source validation failed: " + "; ".join(
            item.message for item in report.findings
        )

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

        return f"Restart the session, then use /workflow {name}."


class CreateToolRunner(CreateWorkflowRunner):
    """Shared authoring lifecycle specialized for ``TOOLS`` modules."""

    workflow_name = "create_tools"
    artifact_kind = "tool"
    destination_dir = "tools"
    artifact_label = "tool"

    def _generation_prompt(self, intent: str) -> str:
        return (
            "You are the design phase of agenthicc's create_tools workflow.\n\n"
            "Create exactly one project tool module for the user's intent. Inspect the "
            "existing tool guides, lauren-ai tool conventions, and relevant tests. The "
            "module must use Lauren's @tool decorator and export every public callable "
            "through a literal TOOLS list. Keep filesystem, network, approval, and output "
            "behavior inside existing agenthicc boundaries.\n\n"
            "Return ONLY this envelope, with no explanation outside it:\n"
            '<tool name="lowercase_module_name" description="short description">\n'
            "```python\n"
            "from lauren_ai import tool\n"
            "\n"
            '@tool(name="tool_name", description="...")\n'
            "async def tool_name(...) -> dict[str, object]:\n"
            "    ...\n"
            "\n"
            "TOOLS = [tool_name]\n"
            "```\n"
            "</tool>\n\n"
            f"USER INTENT:\n{intent}\n"
        )

    def _activation_message(self, name: str) -> str:
        return "Restart the session, then ask the agent to use the generated tool."


class CreateCommandRunner(CreateWorkflowRunner):
    """Shared authoring lifecycle specialized for ``Command`` modules."""

    workflow_name = "create_commands"
    artifact_kind = "command"
    destination_dir = "commands"
    artifact_label = "command"
    activation = "commands-reload"

    def _generation_prompt(self, intent: str) -> str:
        return (
            "You are the design phase of agenthicc's create_commands workflow.\n\n"
            "Create exactly one project command module for the user's intent. Inspect "
            "agenthicc.commands.Command, the command plugin loader, dispatcher, and tests. "
            "Export COMMAND for one command or COMMANDS for multiple commands, with a "
            "small validated handler or menu factory. Do not execute shell text or bypass "
            "existing capability and approval boundaries.\n\n"
            "Return ONLY this envelope, with no explanation outside it:\n"
            '<command name="lowercase_module_name" description="short description">\n'
            "```python\n"
            "from agenthicc.commands import Command, CommandContext\n"
            "\n"
            "def handle_command(ctx: CommandContext) -> bool:\n"
            '    ctx.console.print("...")\n'
            "    return True\n"
            "\n"
            'COMMAND = Command("/example", "...", handler=handle_command)\n'
            "```\n"
            "</command>\n\n"
            f"USER INTENT:\n{intent}\n"
        )

    def _activation_message(self, name: str) -> str:
        return "Run /commands reload, then invoke the generated slash command."
