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
    """Design, execute, and directly write one project-local workflow plugin."""

    name = "create_workflow"
    description = "Create an agenthicc workflow from the next user intent."
    mode_bindings: list[str] = []
    phases = [
        PhaseSpec(
            name="interpret",
            agent_type="planner",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow from "
                "the user's request. You are authoring that new workflow, not executing "
                "the runtime task it will later perform. In this INTERPRET phase, study "
                "only the current contracts needed to understand the request and turn it "
                "into one precise workflow intent. Identify the workflow's purpose, stable "
                "name, runtime inputs, expected outputs, required MCP/tools or services, "
                "phase responsibilities, verification evidence, safety and capability "
                "constraints, activation requirements, and measurable success criteria. "
                "Preserve the user's meaning; resolve ambiguity conservatively and do not "
                "invent integrations that are not available. Do not generate Python source, "
                "publish anything, or execute the future workflow in this phase. "
                "TRANSITION: when the intent is precise enough for source generation, call "
                "complete_interpret_phase(summary) with the normalized intent and evidence. "
                "That tool invocation is the only accepted handoff and moves the run to "
                "the DESIGN phase; a prose response alone does not advance the workflow."
            ),
            next="design",
            max_turns=20,
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow from "
                "the interpreted user intent. You are the DESIGN phase of that authoring "
                "run: produce the complete implementation specification for the future "
                "workflow, rather than answering the runtime request yourself or writing "
                "source code. Inspect the current plugin, "
                "PhaseSpec, runner, tool, capability, workspace, MCP, approval, and "
                "activation contracts before deciding how to implement it. Prefer one "
                "declarative WorkflowPlugin with literal PhaseSpec values and the inherited "
                "runner; introduce a custom runner only when the requested orchestration "
                "cannot be expressed by a phase graph. Give every generated phase a "
                "self-contained prompt that specifies its objective, available tools, "
                "inputs, outputs, verification evidence, safety boundaries, completion "
                "signal, and next-phase handoff. Keep generated code secure, typed, "
                "loader-compatible, and faithful to the interpreted intent. Do not "
                "generate the final Python source, call write_file, call batch_write, "
                "execute shell commands, or modify the workspace in this phase. "
                "TRANSITION: when the implementation specification is complete, call "
                "complete_design_phase(summary). Include the stable workflow name, "
                "phase graph, complete per-phase prompts, APIs/tools/MCP services, "
                "inputs, outputs, verification behavior, safety constraints, and "
                "activation notes in that summary. A successful handoff moves the run "
                "to EXECUTE; a prose response alone does not advance the workflow."
            ),
            next="execute",
            max_iterations=20,
            max_turns=20,
        ),
        PhaseSpec(
            name="execute",
            agent_type="executor",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow from "
                "the interpreted user intent. You are the EXECUTE phase of that authoring "
                "run. Consume the design specification from the previous phase and "
                "implement the complete workflow source directly. Inspect only the "
                "current contracts still needed to resolve implementation details. Prefer "
                "one declarative WorkflowPlugin with literal PhaseSpec values and the "
                "inherited runner; introduce a custom runner only when the specification "
                "cannot be expressed by a phase graph. Generate exactly one complete "
                "Python workflow source file. TRANSITION: use the canonical write_file tool with "
                "path .agenthicc/workflows/<stable_name>.py, wait for its successful "
                "result, and then call complete_execute_phase(summary, artifact_name, "
                "artifact_description). Do not use batch_write, shell redirection, an "
                "unguarded filesystem API, or a response envelope. The runner never "
                "copies assistant response text, parses or validates the source, stages "
                "or publishes the file, or asks for end-user approval. A successful "
                "handoff moves the run to SUMMARIZE; if the write or handoff fails, "
                "retry it rather than returning prose."
            ),
            next="summarize",
            max_iterations=20,
            max_turns=20,
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow and finish "
                "its authoring run with an accurate, actionable handoff to the user. You "
                "are in the SUMMARIZE phase, the final phase of this authoring state machine. "
                "Report only the authoritative structured result: workflow name, status, "
                "artifact kind, agent-written path when reported, unresolved errors, and "
                "the exact next action to reload, discover, or run "
                "the newly created workflow. Do not claim activation or success that the "
                "result does not prove. "
                "TRANSITION: after stating the complete truthful summary, call "
                "complete_summarize_phase(summary). That tool invocation closes the final "
                "phase and moves the runner to its terminal COMPLETE, REJECTED, or FAILED "
                "state; there is no later authoring phase, so do not continue with extra "
                "implementation or runtime execution."
            ),
            max_turns=20,
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateWorkflowRunner:
        from agenthicc.workflows.authoring.runner import CreateWorkflowRunner

        return CreateWorkflowRunner(config, mode_manager, phase_specs=tuple(cls.phases))


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
                "contract to design. TRANSITION: call "
                "complete_interpret_phase(summary) after the tool contract is precise."
            ),
            next="design",
            max_turns=8,
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
                "directory. TRANSITION: return the complete source directly, then call "
                "complete_design_phase(summary). The runner captures and validates the "
                "response before staging it."
            ),
            next="stage",
            max_iterations=2,
            max_turns=20,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "Store the generated tool source in the run-scoped authoring staging "
                "area with its manifest and hash. Keep it undiscoverable and do not "
                "publish, import, or execute it before validation and explicit approval. "
                "Call complete_stage_phase(summary) only when it is ready for staging."
            ),
            next="review",
            max_turns=4,
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "Present the validated staged tool, its callable behavior, capability "
                "and integration requirements, source path, validation findings, and "
                "activation implications for explicit publication approval. If denied, "
                "call request_publication_approval() for the decision. If denied, "
                "preserve the staged artifact and explain how it can be resumed."
            ),
            next="publish",
            on_reject="summarize",
            max_turns=4,
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "After explicit approval, atomically publish the validated tool to the "
                "project tools directory, preserve its manifest and digest, and report "
                "the exact /tools reload action. Call complete_publish_phase(summary) "
                "before publication. Never publish an unvalidated or unapproved tool."
            ),
            next="summarize",
            max_turns=4,
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "Summarize the tool-authoring outcome with the artifact name, status, "
                "staged or published path, validation result, approval state, required "
                "reload action, and any unresolved prerequisite. Never claim the tool "
                "is active before reload. Call complete_summarize_phase(summary) after "
                "stating the authoritative result."
            ),
            max_turns=4,
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateToolRunner:
        from agenthicc.workflows.authoring.runner import CreateToolRunner

        return CreateToolRunner(config, mode_manager, phase_specs=tuple(cls.phases))


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
                "contract to design. TRANSITION: call "
                "complete_interpret_phase(summary) after the command contract is precise."
            ),
            next="design",
            max_turns=8,
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            system_prompt_override=(
                "Generate the complete raw Python source for the requested slash-command "
                "module. Include ARTIFACT_NAME, ARTIFACT_DESCRIPTION, canonical literal "
                "Command metadata, exactly one compatible COMMAND or COMMANDS export, "
                "and a bounded handler or menu factory. Do not return an envelope or "
                "write to the discoverable commands directory. TRANSITION: return the "
                "complete source directly, then call complete_design_phase(summary). "
                "The runner captures and validates the response before staging it."
            ),
            next="stage",
            max_iterations=2,
            max_turns=20,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "Store the generated command source in the run-scoped authoring staging "
                "area with its manifest and hash. Keep it undiscoverable and do not "
                "publish, import, or execute it before validation and explicit approval. "
                "Call complete_stage_phase(summary) only when it is ready for staging."
            ),
            next="review",
            max_turns=4,
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "Present the validated staged command, invocation syntax, argument and "
                "busy-state behavior, source path, validation findings, and activation "
                "implications for explicit publication approval. If denied, preserve the "
                "staged artifact and explain how it can be resumed. Call "
                "request_publication_approval() for the decision."
            ),
            next="publish",
            on_reject="summarize",
            max_turns=4,
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "After explicit approval, atomically publish the validated command to "
                "the project commands directory, preserve its manifest and digest, and "
                "report the exact /commands reload action. Never publish an unvalidated "
                "or unapproved command. Call complete_publish_phase(summary) before "
                "publication."
            ),
            next="summarize",
            max_turns=4,
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            system_prompt_override=(
                "Summarize the command-authoring outcome with the artifact name, status, "
                "staged or published path, validation result, approval state, canonical "
                "invocation, required reload action, and unresolved errors. Never claim "
                "the command is active before reload. Call complete_summarize_phase(summary) "
                "after stating the authoritative result."
            ),
            max_turns=4,
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateCommandRunner:
        from agenthicc.workflows.authoring.runner import CreateCommandRunner

        return CreateCommandRunner(config, mode_manager, phase_specs=tuple(cls.phases))


# Singular names are the concise interactive spellings. The registry exposes
# them as aliases while keeping the PRD's plural names canonical for discovery.
CreateTool = CreateTools
CreateCommand = CreateCommands
