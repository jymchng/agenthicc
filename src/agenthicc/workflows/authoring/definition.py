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
                "run: generate the complete implementation of the future workflow, rather "
                "than answering the runtime request yourself. Inspect the current plugin, "
                "PhaseSpec, runner, tool, capability, workspace, MCP, approval, and "
                "activation contracts before deciding how to implement it. Prefer one "
                "declarative WorkflowPlugin with literal PhaseSpec values and the inherited "
                "runner; introduce a custom runner only when the requested orchestration "
                "cannot be expressed by a phase graph. Give every generated phase a "
                "self-contained prompt that specifies its objective, available tools, "
                "inputs, outputs, verification evidence, safety boundaries, completion "
                "signal, and next-phase handoff. Keep generated code secure, typed, "
                "loader-compatible, and faithful to the interpreted intent. Generate the "
                "complete raw Python source directly: no plan, pseudocode, patch, XML, "
                "JSON, Markdown fence, or explanatory envelope, and do not write to the "
                "discoverable workflow directory. TRANSITION: submit the complete source "
                "with submit_generated_source(source, artifact_name, artifact_description), "
                "then call complete_design_phase(summary) only after the source and its "
                "metadata are complete. A successful tool handoff moves the authoring run "
                "to STAGE; free-form text cannot advance it."
            ),
            next="stage",
            max_iterations=2,
            max_turns=20,
        ),
        PhaseSpec(
            name="stage",
            agent_type="auto",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow from "
                "the user's intent and carry it safely through authoring to publication. "
                "You are in the STAGE phase. The DESIGN phase has supplied a complete "
                "candidate; confirm that the candidate and metadata are ready to be stored "
                "without changing the user's requested behavior. Staging must use the "
                "run-scoped authoring directory, manifest, and source hash so the candidate "
                "remains isolated, auditable, and undiscoverable. Do not publish, import, "
                "execute, activate, or write into the discoverable workflow directory, and "
                "do not silently repair source in this phase. TRANSITION: after confirming "
                "the candidate is ready for isolated staging, call "
                "complete_stage_phase(summary). The runner performs the staging side "
                "effect and then moves to VALIDATE; a prose response alone does not advance "
                "the authoring workflow."
            ),
            next="validate",
            max_turns=20,
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow that is "
                "safe, loader-compatible, and ready for an explicit publication decision. "
                "You are in the VALIDATE phase. Inspect the immutable run-scoped staged "
                "artifact and the current agenthicc contracts without importing or executing "
                "generated code. Check syntax, allowed imports and calls, stable name and "
                "description, literal non-empty phase prompts, phase references and graph "
                "termination, agent roles, runner selection, build_runner wiring, tool and "
                "capability boundaries, activation/reload instructions, and consistency of "
                "the staged source, manifest, digest, and deterministic validation report. "
                "Report every blocking finding precisely; never claim safety merely because "
                "the source parses. Do not modify, publish, activate, or execute the staged "
                "workflow. TRANSITION: call complete_validate_phase(summary) only when the "
                "candidate is complete, safe, and ready for human publication review. The "
                "runner then moves to REVIEW; if prerequisites fail, the transition tool "
                "returns the error and fix and the agent must correct or explain it before "
                "trying again."
            ),
            next="review",
            max_turns=20,
        ),
        PhaseSpec(
            name="review",
            agent_type="human",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow while "
                "keeping publication an explicit, informed, user-controlled decision. You "
                "are in the REVIEW phase. Present the validated staged workflow's name, "
                "purpose, intended runtime behavior, phase topology, integrations, source "
                "path, validation evidence, digest/manifest identity, safety implications, "
                "destination, and the exact reload or activation action. Do not alter the "
                "source, bypass validation, publish, or execute the future workflow. "
                "TRANSITION: after presenting the evidence, call "
                "request_publication_approval() exactly once to obtain the explicit "
                "publication decision. If approved, that tool invocation moves the run to "
                "PUBLISH; if denied, the artifact stays staged and the runner moves to "
                "SUMMARIZE. Do not treat a prose approval or denial as a transition."
            ),
            next="publish",
            on_reject="summarize",
            max_turns=20,
        ),
        PhaseSpec(
            name="publish",
            agent_type="auto",
            system_prompt_override=(
                "ULTIMATE PURPOSE: create one new specialized agenthicc workflow by "
                "safely completing publication of exactly the artifact that passed validation "
                "and received explicit approval. You are in the PUBLISH phase. Reconfirm "
                "that approval is granted, the artifact is still staged, the source digest "
                "and manifest still match, and no unapproved source replacement occurred. "
                "Publication must be atomic, must preserve the manifest and digest, and must "
                "write only to the intended project workflow directory. Never publish an "
                "unvalidated, changed, unapproved, or newly generated artifact; never claim "
                "the workflow is active before the required reload or restart. TRANSITION: "
                "after the checks are complete, call complete_publish_phase(summary). The "
                "runner performs publication and then moves to SUMMARIZE; the tool call is "
                "the handoff, not a declaration that publication already happened."
            ),
            next="summarize",
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
                "artifact kind, staged or published path, manifest/digest, validation "
                "outcome, approval state, unresolved errors, and the exact next action to "
                "reload, discover, resume, or run the newly created workflow. Do not claim "
                "publication, activation, or success that the result does not prove. "
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
                "directory. TRANSITION: submit the complete source with "
                "submit_generated_source(...), then call complete_design_phase(summary)."
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
            next="validate",
            max_turns=4,
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "Validate the staged tool without importing or executing it. Check "
                "syntax, safe imports and calls, metadata, lauren-ai decorator usage, "
                "literal TOOLS export, callable references, annotations, capabilities, "
                "and activation requirements. Report blocking findings precisely and "
                "call complete_validate_phase(summary) only for a loader-compatible candidate."
            ),
            next="review",
            max_turns=8,
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
                "write to the discoverable commands directory. TRANSITION: submit the "
                "complete source with submit_generated_source(...), then call "
                "complete_design_phase(summary)."
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
            next="validate",
            max_turns=4,
        ),
        PhaseSpec(
            name="validate",
            agent_type="verifier",
            system_prompt_override=(
                "Validate the staged command without importing or executing it. Check "
                "syntax, safe imports and calls, metadata, Command export shape, literal "
                "slash names and descriptions, handler or menu references, argument "
                "contract, and activation requirements. Report blocking findings "
                "precisely and call complete_validate_phase(summary) only for a "
                "loader-compatible candidate."
            ),
            next="review",
            max_turns=8,
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
