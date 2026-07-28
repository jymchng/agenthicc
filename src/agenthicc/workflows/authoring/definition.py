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
    phases = [
        PhaseSpec(
            name="interpret",
            agent_type="planner",
            system_prompt_override=(
                "Normalize the user's request into one tool intent. Identify the "
                "callable purpose, inputs, outputs, external services, filesystem or "
                "network needs, capabilities, error cases, and measurable success "
                "criteria. Preserve the user's meaning and hand off a concise tool "
                "contract to design."
            ),
            next="design",
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            system_prompt_override=(
                "Generate the complete raw Python source for the requested lauren-ai "
                "tool module. Include ARTIFACT_NAME, ARTIFACT_DESCRIPTION, the @tool "
                "decorator, a literal TOOLS export, accurate annotations, bounded "
                "errors, and only the configured integrations the tool can actually "
                "use. Do not return an envelope or write to the discoverable tools "
                "directory."
            ),
            next="stage",
            max_iterations=2,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "Store the generated tool source in the run-scoped authoring staging "
                "area with its manifest and hash. Keep it undiscoverable and do not "
                "publish, import, or execute it before validation and explicit approval."
            ),
            next="validate",
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "Validate the staged tool without importing or executing it. Check "
                "syntax, safe imports and calls, metadata, lauren-ai decorator usage, "
                "literal TOOLS export, callable references, annotations, capabilities, "
                "and activation requirements. Report blocking findings precisely and "
                "pass only a loader-compatible candidate to review."
            ),
            next="review",
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "Present the validated staged tool, its callable behavior, capability "
                "and integration requirements, source path, validation findings, and "
                "activation implications for explicit publication approval. If denied, "
                "preserve the staged artifact and explain how it can be resumed."
            ),
            next="publish",
            on_reject="summarize",
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "After explicit approval, atomically publish the validated tool to the "
                "project tools directory, preserve its manifest and digest, and report "
                "the exact /tools reload action. Never publish an unvalidated or "
                "unapproved tool."
            ),
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "Summarize the tool-authoring outcome with the artifact name, status, "
                "staged or published path, validation result, approval state, required "
                "reload action, and any unresolved prerequisite. Never claim the tool "
                "is active before reload."
            ),
        ),
    ]

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
    phases = [
        PhaseSpec(
            name="interpret",
            agent_type="planner",
            system_prompt_override=(
                "Normalize the user's request into one slash-command intent. Identify "
                "the canonical command name, arguments, aliases, output, context "
                "callbacks, busy-state behavior, capabilities, and measurable success "
                "criteria. Preserve the user's meaning and hand off a concise command "
                "contract to design."
            ),
            next="design",
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            system_prompt_override=(
                "Generate the complete raw Python source for the requested slash-command "
                "module. Include ARTIFACT_NAME, ARTIFACT_DESCRIPTION, canonical literal "
                "Command metadata, exactly one compatible COMMAND or COMMANDS export, "
                "and a bounded handler or menu factory. Do not return an envelope or "
                "write to the discoverable commands directory."
            ),
            next="stage",
            max_iterations=2,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "Store the generated command source in the run-scoped authoring staging "
                "area with its manifest and hash. Keep it undiscoverable and do not "
                "publish, import, or execute it before validation and explicit approval."
            ),
            next="validate",
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "Validate the staged command without importing or executing it. Check "
                "syntax, safe imports and calls, metadata, Command export shape, literal "
                "slash names and descriptions, handler or menu references, argument "
                "contract, and activation requirements. Report blocking findings "
                "precisely and pass only a loader-compatible candidate to review."
            ),
            next="review",
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "Present the validated staged command, invocation syntax, argument and "
                "busy-state behavior, source path, validation findings, and activation "
                "implications for explicit publication approval. If denied, preserve the "
                "staged artifact and explain how it can be resumed."
            ),
            next="publish",
            on_reject="summarize",
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "After explicit approval, atomically publish the validated command to "
                "the project commands directory, preserve its manifest and digest, and "
                "report the exact /commands reload action. Never publish an unvalidated "
                "or unapproved command."
            ),
            next="summarize",
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "Summarize the command-authoring outcome with the artifact name, status, "
                "staged or published path, validation result, approval state, canonical "
                "invocation, required reload action, and unresolved errors. Never claim "
                "the command is active before reload."
            ),
        ),
    ]

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
