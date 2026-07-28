"""Built-in workflow authoring definitions (PRD-147)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.authoring.runner import (
        CreateCommandRunner,
        CreateToolRunner,
        CreateWorkflowRunner,
    )
    from agenthicc.workflows.config import WorkflowConfig


class CreateWorkflow(WorkflowPlugin):
    """Generate and publish one validated project-local workflow plugin."""

    name = "create_workflow"
    description = "Create a validated agenthicc workflow from the next user intent."
    mode_bindings: list[str] = []
    phases = [
        PhaseSpec(
            name="interpret",
            agent_type="planner",
            system_prompt_override=(
                "Normalize the user's request into one specialized workflow intent. "
                "Identify the workflow purpose, runtime inputs, required integrations, "
                "expected outputs, safety constraints, and measurable success criteria. "
                "Preserve the user's meaning and hand off a concise intent record to "
                "the design phase."
            ),
            next="design",
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            system_prompt_override=(
                "Generate the complete Python source for the specialized workflow "
                "described by the interpreted intent. Choose a declarative PhaseSpec "
                "graph unless custom orchestration is genuinely required. Give every "
                "generated phase a self-contained system_prompt_override that states "
                "its objective, tools, inputs, outputs, verification, completion "
                "signal, and next-phase handoff. Return raw source only and do not "
                "write to the discoverable workflow directory."
            ),
            next="stage",
            max_iterations=2,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "Store the generated workflow source in the run-scoped authoring "
                "staging area with its manifest and hash. Keep it undiscoverable and "
                "do not publish or execute it before validation and explicit approval."
            ),
            next="validate",
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "Validate the staged workflow without importing or executing it. Check "
                "syntax, safe imports and calls, name and phase references, literal "
                "non-empty phase prompts, runner selection, factory wiring, and "
                "activation requirements. Report blocking findings precisely and pass "
                "only a complete safe candidate to review."
            ),
            next="review",
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "Present the validated staged workflow, its intended behavior, source "
                "path, validation findings, and activation implications for explicit "
                "publication approval. If approval is denied, preserve the staged "
                "artifact and explain how it can be corrected or resumed."
            ),
            next="publish",
            on_reject="summarize",
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "After explicit approval, atomically publish the validated staged "
                "workflow to the project workflow directory, preserve its manifest "
                "and digest, and report the exact reload or activation action. Never "
                "publish an unvalidated or unapproved artifact."
            ),
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "Summarize the authoring outcome with the workflow name, status, "
                "staged or published path, validation result, approval state, and "
                "the exact next action needed to discover and run the specialized "
                "workflow. Mention unresolved errors without claiming success."
            ),
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateWorkflowRunner:
        from agenthicc.workflows.authoring.runner import CreateWorkflowRunner

        return CreateWorkflowRunner(config, mode_manager)


class CreateTools(WorkflowPlugin):
    """Generate and publish one validated project ``TOOLS`` module."""

    name = "create_tools"
    description = "Create a validated lauren-ai tool plugin from the next user intent."
    mode_bindings: list[str] = []
    phases = CreateWorkflow.phases

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateToolRunner:
        from agenthicc.workflows.authoring.runner import CreateToolRunner

        return CreateToolRunner(config, mode_manager)


class CreateCommands(WorkflowPlugin):
    """Generate and publish one validated project command plugin."""

    name = "create_commands"
    description = "Create a validated slash-command plugin from the next user intent."
    mode_bindings: list[str] = []
    phases = CreateWorkflow.phases

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateCommandRunner:
        from agenthicc.workflows.authoring.runner import CreateCommandRunner

        return CreateCommandRunner(config, mode_manager)


# Singular names are the concise interactive spellings. The registry exposes
# them as aliases while keeping the PRD's plural names canonical for discovery.
CreateTool = CreateTools
CreateCommand = CreateCommands
