"""CreateWorkflow plugin definition and tunable parameters.

Co-located with the state machine and runner that back it, mirroring
``agenthicc.workflows.code_plan.definition``.

The declarative ``phases`` list here is the metadata the TUI and the workflow
registry read (names, order, per-phase prompts, the write-capable mode override,
and the validate → generate rejection edge).  Execution itself is driven by
:class:`~agenthicc.workflows.create_workflow.runner.CreateWorkflowRunner`, whose
state machine follows exactly this graph.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

#: TOML keys accepted under ``[workflows.create_workflow]``.
_PARAM_FIELDS: tuple[str, ...] = (
    "design_model",
    "generate_model",
    "validate_model",
    "summary_model",
)


@dataclasses.dataclass
class CreateWorkflowParams(WorkflowParams):
    """Tunable parameters for the create_workflow workflow.

    Each field maps to a TOML key under ``[workflows.create_workflow]``.

    Example agenthicc.toml::

        [workflows.create_workflow]
        generate_model = "claude-opus-5"   # strongest model writes the source
        validate_model = ""               # empty → use execution.model
    """

    design_model: str = field(default="")
    generate_model: str = field(default="")
    validate_model: str = field(default="")
    summary_model: str = field(default="")

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name → configured model override (empty = global default)."""
        return {
            "design": self.design_model,
            "generate": self.generate_model,
            "validate": self.validate_model,
            "summarize": self.summary_model,
        }


class CreateWorkflow(WorkflowPlugin):
    """Author a new custom workflow: Design → Generate → Validate → Summary.

    The meta-workflow downstream users invoke to build their own workflows.  One
    agent runs all four phases sharing a single ``ShortTermMemory``, so the phase
    that writes the file already has the approved design in context.

    Design is read-only and gated on human approval. Generation runs in ``Yolo``
    mode so the write tools are available.  Validation imports the generated file
    deterministically before the agent votes, and a rejection loops back to
    generation.
    """

    name = "create_workflow"
    description = "Design → Generate → Validate → Summary  (author a new custom workflow)"
    mode_bindings: list[str] = []  # manual only — invoke with /workflow create_workflow
    phases = [
        PhaseSpec(
            name="design",
            agent_type="auto",
            max_turns=20,
            next="generate",
            on_reject="design",
            max_iterations=20,
            require_plan_finalization=True,
            mode_override=None,
            system_prompt_override=(
                "You are in the DESIGN phase of create_workflow. Design the new workflow — "
                "its lower_snake_case name, its phases, the transition graph, AND its own "
                "state-machine runner (state enum, context dataclass, one method per state, "
                "the transition tool ending each phase) — without writing any files. "
                "Inspect the authoring API with describe_phasespec(), "
                "list_tool_capabilities(), list_agent_roles(), "
                "describe_cloakbrowser_tools(), describe_playwright_tools(), "
                "describe_runner_pattern(), describe_transition_tool_pattern(), "
                "describe_prompt_cache_contract(), show_workflow_template(), "
                "validate_workflow_cache_contract(path), and show_example_workflow(). Present "
                "the design with "
                "request_design_approval(design, workflow_name) and call "
                "finalize_design(design, workflow_name) once it is approved. If the "
                "request is not about creating a new workflow, call "
                "exit_create_workflow(suggestion) instead. Only a successful call to "
                "request_design_approval(), finalize_design(), or "
                "exit_create_workflow() changes phase; prose never advances it. Keep "
                "immutable workflow policy in a literal CACHE_CONTRACT and put phase "
                "state, artifacts, questions, answers, and summaries in dynamic context. "
                "The runner must inherit WorkflowConfig.workspace_scope and "
                "WorkflowConfig.workspace_access unchanged; never create a second "
                "workspace policy or bypass it with raw filesystem calls. "
                "The generated runner must use ask_user for material ambiguity and must "
                "not guess."
            ),
        ),
        PhaseSpec(
            name="generate",
            agent_type="auto",
            max_turns=20,
            next="validate",
            max_iterations=20,
            require_explicit_completion=True,
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the GENERATION phase of create_workflow. You already designed "
                "the workflow — do NOT re-design. Write the complete WorkflowPlugin package "
                "to .agenthicc/workflows/<name>/ with the write tools. The directory must "
                "contain runner.py (the plugin entry point), __init__.py when useful for "
                "re-exports, and workflow-specific tools/helpers in sibling files such as "
                "tools.py. Put the module docstring, imports, plugin class, every approved "
                "PhaseSpec, and the approved state-machine runner in runner.py — state enum, context dataclass, one "
                "async method per state, the while/match driver in run(), resume(), the "
                "phase tool factories, and build_runner() returning it. No stubs or TODOs. "
                "Declare a literal CACHE_CONTRACT containing stable workflow policy, pass "
                "it as stable_system_prompt to CodePlanRunner.run_phase(), keep phase state "
                "and artifacts dynamic, and use ask_user whenever requirements are missing "
                "or ambiguous rather than guessing. Inherit the parent session's "
                "WorkflowConfig.workspace_scope and workspace_access for every phase, "
                "custom tool, mention, Git, and command cwd; never construct a second "
                "allow-list or unrestricted sandbox. For every phase tool factory, import "
                "tool_control from agenthicc.tools.capabilities and use bare @tool_control "
                "above @tool(); never import it from lauren_ai._tools or write "
                "@tool_control(). "
                "When the directory and all of its files are fully written, call "
                "mark_generation_complete(summary, path). "
                "Only a successful mark_generation_complete() call changes phase; prose "
                "such as 'done' never advances it."
            ),
        ),
        PhaseSpec(
            name="validate",
            agent_type="auto",
            max_turns=20,
            next="summarize",
            on_reject="generate",
            max_iterations=20,
            require_explicit_review=True,
            mode_override=None,
            system_prompt_override=(
                "You are in the VALIDATION phase of create_workflow. The generated package "
                "has already been imported and checked; read that report first. Call "
                "reject_workflow(reason) if it lists any error or the file does not match the "
                "approved design, otherwise call approve_workflow(summary). You MUST call "
                "exactly one of these two tools. Only a successful call to "
                "approve_workflow() or reject_workflow() changes phase; prose never "
                "advances it."
            ),
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            max_turns=4,
            output_schema="free_text",
            mode_override=None,
            system_prompt_override=(
                "You are in the SUMMARY phase of create_workflow. Tell the user which "
                "workflow was created, where it lives, what its phases are, and that "
                "'/workflows reload' makes it available in this session. No "
                "phase-transition tool is available; the runner owns completion."
            ),
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateWorkflowRunner:
        """Return a CreateWorkflowRunner — uses its own state machine."""
        from agenthicc.workflows.create_workflow.runner import (  # noqa: PLC0415
            CreateWorkflowRunner,
        )

        return CreateWorkflowRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> WorkflowParams:
        """Build :class:`CreateWorkflowParams` from *source* (merged TOML/CLI)."""
        values: dict[str, str] = {
            field_name: value
            for field_name in _PARAM_FIELDS
            if isinstance(value := source.get(field_name), str)
        }
        return CreateWorkflowParams(
            design_model=values.get("design_model", ""),
            generate_model=values.get("generate_model", ""),
            validate_model=values.get("validate_model", ""),
            summary_model=values.get("summary_model", ""),
        )
