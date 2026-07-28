"""Workflow plugin definition for ``create_workflow``."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner


@dataclass
class CreateWorkflowParams(WorkflowParams):
    """Optional model overrides for the authoring phases."""

    interpret_model: str = ""
    design_model: str = ""
    execute_model: str = ""
    summarize_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Return the configured phase-to-model mapping."""

        return {
            "interpret": self.interpret_model,
            "design": self.design_model,
            "execute": self.execute_model,
            "summarize": self.summarize_model,
        }


class CreateWorkflow(WorkflowPlugin):
    """Create one project-local ``WorkflowPlugin`` from a user's use case."""

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
                "You are the INTERPRET phase of create_workflow. Normalize the user's use case "
                "into a precise purpose for one downstream custom workflow. Choose a stable "
                "lowercase Python workflow name, identify inputs, outputs, tools, safety limits, "
                "and success criteria. You may inspect files and documentation, but do not write "
                "source. When complete, call "
                "complete_interpret_phase(summary, workflow_name). This is the only tool call "
                "that can advance to DESIGN. A prose response alone cannot advance the phase."
            ),
        ),
        PhaseSpec(
            name="design",
            agent_type="planner",
            max_turns=20,
            max_iterations=20,
            next="execute",
            system_prompt_override=(
                "You are the DESIGN phase of create_workflow. Turn the normalized use case into "
                "a complete implementation design for a downstream WorkflowPlugin. Describe "
                "each generated phase, its prompt and tools, typed inputs/outputs, transition "
                "rules, limits, persistence and resume behaviour, and tests. For stateful or "
                "conditional behaviour, follow code_plan: use a typed State enum, a typed Context "
                "dataclass, one bounded async method per phase, and an outer while-not-terminal "
                "match/case loop. Do not write source. Call "
                "complete_design_phase(design); this is the only tool call that can advance to "
                "EXECUTE. Prose alone cannot advance the phase."
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
                "You are the EXECUTE phase of create_workflow. Generate the complete Python "
                "source from the design and write it directly with write_file to "
                ".agenthicc/workflows/<workflow_name>.py. Use current agenthicc contracts; do not "
                "invent historical paths. A custom runner must keep phase methods, the outer "
                "state loop, tool-gated transitions, context artifacts, and resume logic in its "
                "own ownership boundary. Do not use shell, staging, parsing, validation, or an "
                "assistant response as a substitute for write_file. After write_file succeeds, "
                "call complete_execute_phase(summary, artifact_name, artifact_description). "
                "This is the only tool call that can advance to SUMMARIZE."
            ),
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            max_turns=20,
            max_iterations=20,
            system_prompt_override=(
                "You are the terminal SUMMARIZE phase of create_workflow. Report the truthful "
                "workflow name, exact written path, what the generated workflow does, and the "
                "next action for the user to reload and run it. Do not claim that source was "
                "validated or activated unless the tools prove it. Call "
                "complete_summarize_phase(summary); this is the only tool call that can complete "
                "the create_workflow run. Prose alone cannot close the run."
            ),
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateWorkflowRunner:
        """Build the explicit authoring state-machine runner."""

        from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner

        return CreateWorkflowRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> CreateWorkflowParams:
        """Build typed parameters from the workflow configuration mapping."""

        names = ("interpret_model", "design_model", "execute_model", "summarize_model")
        return CreateWorkflowParams(
            **{name: value for name in names if isinstance(value := source.get(name), str)}
        )


__all__ = ["CreateWorkflow", "CreateWorkflowParams"]
