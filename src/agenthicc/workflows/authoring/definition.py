"""Builtin definition and parameters for ``create_workflow``."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
    from agenthicc.workflows.config import WorkflowConfig


@dataclass
class CreateWorkflowParams(WorkflowParams):
    """Optional per-phase model overrides for workflow authoring."""

    interpret_model: str = ""
    design_model: str = ""
    execute_model: str = ""
    summarize_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        return {
            "interpret": self.interpret_model,
            "design": self.design_model,
            "execute": self.execute_model,
            "summarize": self.summarize_model,
        }


class CreateWorkflow(WorkflowPlugin):
    """Author one project-local specialized workflow from natural language."""

    name = "create_workflow"
    description = "Interpret, design, write, and summarize a custom workflow."
    mode_bindings: list[str] = []
    max_total_phase_runs = 0
    phases = [
        PhaseSpec(
            name="interpret",
            agent_type="planner",
            max_turns=20,
            max_iterations=20,
            next="design",
            system_prompt_override=(
                "You are the INTERPRET phase of create_workflow. The ultimate purpose is to "
                "create one specialized agenthicc workflow for the user's use case. Normalize "
                "the intent, choose a valid stable lowercase workflow name, identify runtime "
                "inputs, outputs, tools or MCP services, safety boundaries, and success criteria. "
                "Do not generate source or modify files. When complete, call "
                "complete_interpret_phase(summary, workflow_name). A prose response alone cannot "
                "advance the phase."
            ),
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            max_turns=20,
            max_iterations=20,
            next="execute",
            system_prompt_override=(
                "You are the DESIGN phase of create_workflow. Produce a complete implementation "
                "design for the normalized intent. Inspect current agenthicc contracts and docs "
                "as needed. Define every generated phase's objective, self-contained prompt, "
                "inputs, outputs, tools, evidence, and transition. Prefer declarative PhaseSpec "
                "when sufficient. For conditional, looping, parallel, or transformed workflows, "
                "design a typed State(Enum), typed dataclass Context, one bounded async function "
                "per non-terminal state, and a run() while-not-terminal match/case driver, with "
                "resume() using the same dispatch path, following code_plan. Do not write source. "
                "Call complete_design_phase(design); prose alone cannot advance."
            ),
        ),
        PhaseSpec(
            name="execute",
            agent_type="executor",
            max_turns=20,
            max_iterations=20,
            next="summarize",
            mode_override="Auto",
            system_prompt_override=(
                "You are the EXECUTE phase of create_workflow. Generate the complete workflow "
                "Python source directly from the design and write it with the canonical "
                "write_file tool to .agenthicc/workflows/<workflow_name>.py. The content argument "
                "must contain the complete source; do not use shell, batch_write, a response "
                "envelope, staging, validation, publishing, or end-user approval. If the design "
                "requires a custom runner, implement its typed state/context phase functions and "
                "outer match/case loop directly. After write_file succeeds, call "
                "complete_execute_phase(summary, artifact_name, artifact_description). The runner "
                "only checks the exact file exists and never copies assistant prose."
            ),
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            max_turns=20,
            max_iterations=20,
            system_prompt_override=(
                "You are the terminal SUMMARIZE phase of create_workflow. Report the truthful "
                "workflow name, exact agent-written path, what was created, and the next action "
                "to reload and run it. Do not claim source validation or activation. Call "
                "complete_summarize_phase(summary); prose alone cannot close the run."
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

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> CreateWorkflowParams:
        names = ("interpret_model", "design_model", "execute_model", "summarize_model")
        return CreateWorkflowParams(
            **{name: value for name in names if isinstance(value := source.get(name), str)}
        )


__all__ = ["CreateWorkflow", "CreateWorkflowParams"]
