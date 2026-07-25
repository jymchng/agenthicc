"""Built-in workflow authoring definition (PRD-147)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.authoring.runner import CreateWorkflowRunner
    from agenthicc.workflows.config import WorkflowConfig


class CreateWorkflow(WorkflowPlugin):
    """Generate and publish one validated project-local workflow plugin."""

    name = "create_workflow"
    description = "Create a validated agenthicc workflow from the next user intent."
    mode_bindings: list[str] = []
    phases = [
        PhaseSpec(name="interpret", agent_type="planner", next="design"),
        PhaseSpec(name="design", agent_type="planner", next="stage", max_iterations=2),
        PhaseSpec(name="stage", agent_type="auto", next="validate"),
        PhaseSpec(name="validate", agent_type="verifier", next="review"),
        PhaseSpec(name="review", agent_type="human", next="publish", on_reject="summarize"),
        PhaseSpec(name="publish", agent_type="auto", next="summarize"),
        PhaseSpec(name="summarize", agent_type="auto"),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateWorkflowRunner:
        from agenthicc.workflows.authoring.runner import CreateWorkflowRunner

        return CreateWorkflowRunner(config, mode_manager)
