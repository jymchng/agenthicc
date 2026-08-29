"""Workflow system (PRD-87, PRD-112, PRD-116)."""

from __future__ import annotations

import importlib
from typing import Final


# Keep the historical package-level API while avoiding importing every built-in
# runner when a caller only needs ``workflows.plugin`` or the registry.  This
# is particularly important for CLI startup: workflow implementations import
# provider, browser, and filesystem integrations that are not needed to parse
# arguments or render basic metadata.
_LAZY_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "BaseWorkflowRunner": ("agenthicc.workflows.base_runner", "BaseWorkflowRunner"),
    "WorkflowConfig": ("agenthicc.workflows.config", "WorkflowConfig"),
    "load_builtin_workflows": ("agenthicc.workflows.loader", "load_builtin_workflows"),
    "builtin_workflow_descriptors": ("agenthicc.workflows.loader", "builtin_workflow_descriptors"),
    "load_builtin_workflow": ("agenthicc.workflows.loader", "load_builtin_workflow"),
    "load_python_workflows": ("agenthicc.workflows.loader", "load_python_workflows"),
    "PhaseRunRecord": ("agenthicc.workflows.plugin", "PhaseRunRecord"),
    "PhaseRole": ("agenthicc.workflows.plugin", "PhaseRole"),
    "PhaseSpec": ("agenthicc.workflows.plugin", "PhaseSpec"),
    "WorkflowContext": ("agenthicc.workflows.plugin", "WorkflowContext"),
    "WorkflowEntry": ("agenthicc.workflows.plugin", "WorkflowEntry"),
    "WorkflowParams": ("agenthicc.workflows.plugin", "WorkflowParams"),
    "WorkflowPlugin": ("agenthicc.workflows.plugin", "WorkflowPlugin"),
    "WorkflowRun": ("agenthicc.workflows.plugin", "WorkflowRun"),
    "_parse_output_schema": ("agenthicc.workflows.plugin", "_parse_output_schema"),
    "WorkflowRegistry": ("agenthicc.workflows.registry", "WorkflowRegistry"),
    "build_workflow_registry": ("agenthicc.workflows.registry", "build_workflow_registry"),
    "WorkflowRunner": ("agenthicc.workflows.default.runner", "WorkflowRunner"),
    "PhaseAnnotation": ("agenthicc.workflows.phase_lifecycle", "PhaseAnnotation"),
    "PhaseBoundaryError": ("agenthicc.workflows.phase_lifecycle", "PhaseBoundaryError"),
    "ResumeResolution": ("agenthicc.workflows.phase_lifecycle", "ResumeResolution"),
    "checkpoint_phase_boundary": (
        "agenthicc.workflows.phase_lifecycle",
        "checkpoint_phase_boundary",
    ),
    "publish_phase_annotation": (
        "agenthicc.workflows.phase_lifecycle",
        "publish_phase_annotation",
    ),
    "reconcile_phase_cursor": (
        "agenthicc.workflows.phase_lifecycle",
        "reconcile_phase_cursor",
    ),
    "CodePlan": ("agenthicc.workflows.code_plan", "CodePlan"),
    "CodePlanParams": ("agenthicc.workflows.code_plan", "CodePlanParams"),
    "CodePlanRunner": ("agenthicc.workflows.code_plan", "CodePlanRunner"),
    "CodePlanState": ("agenthicc.workflows.code_plan", "CodePlanState"),
    "CodePlanContext": ("agenthicc.workflows.code_plan", "CodePlanContext"),
    "CreateWorkflow": ("agenthicc.workflows.create_workflow", "CreateWorkflow"),
    "CreateWorkflowContext": ("agenthicc.workflows.create_workflow", "CreateWorkflowContext"),
    "CreateWorkflowParams": ("agenthicc.workflows.create_workflow", "CreateWorkflowParams"),
    "CreateWorkflowRunner": ("agenthicc.workflows.create_workflow", "CreateWorkflowRunner"),
    "CreateWorkflowState": ("agenthicc.workflows.create_workflow", "CreateWorkflowState"),
    "PhaseArtifact": ("agenthicc.workflows.create_workflow", "PhaseArtifact"),
    "GoalFlowWorkflow": ("agenthicc.workflows.goal_flow", "GoalFlowWorkflow"),
    "GoalContext": ("agenthicc.workflows.goal_flow.runner", "GoalContext"),
    "GoalFlowParams": ("agenthicc.workflows.goal_flow.runner", "GoalFlowParams"),
    "GoalFlowRunner": ("agenthicc.workflows.goal_flow.runner", "GoalFlowRunner"),
    "GoalState": ("agenthicc.workflows.goal_flow.runner", "GoalState"),
    "MakeAgenthiccToolWorkflow": (
        "agenthicc.workflows.make_agenthicc_tool",
        "MakeAgenthiccToolWorkflow",
    ),
    "MakeToolContext": ("agenthicc.workflows.make_agenthicc_tool", "MakeToolContext"),
    "MakeToolParams": ("agenthicc.workflows.make_agenthicc_tool", "MakeToolParams"),
    "MakeToolRunner": ("agenthicc.workflows.make_agenthicc_tool", "MakeToolRunner"),
    "MakeToolState": ("agenthicc.workflows.make_agenthicc_tool", "MakeToolState"),
    "ToolParam": ("agenthicc.workflows.make_agenthicc_tool", "ToolParam"),
    "BookChapterInfo": ("agenthicc.workflows.make_book.runner", "ChapterInfo"),
    "MakeBookContext": ("agenthicc.workflows.make_book", "MakeBookContext"),
    "MakeBookParams": ("agenthicc.workflows.make_book", "MakeBookParams"),
    "MakeBookRunner": ("agenthicc.workflows.make_book", "MakeBookRunner"),
    "MakeBookState": ("agenthicc.workflows.make_book", "MakeBookState"),
    "MakeBookWorkflow": ("agenthicc.workflows.make_book", "MakeBookWorkflow"),
    "SiteImitateContext": ("agenthicc.workflows.site_imitate", "SiteImitateContext"),
    "SiteImitateParams": ("agenthicc.workflows.site_imitate", "SiteImitateParams"),
    "SiteImitateRunner": ("agenthicc.workflows.site_imitate", "SiteImitateRunner"),
    "SiteImitateState": ("agenthicc.workflows.site_imitate", "SiteImitateState"),
    "SiteImitateWorkflow": ("agenthicc.workflows.site_imitate", "SiteImitateWorkflow"),
    "ReconstructContext": (
        "agenthicc.workflows.reconstruct_site",
        "ReconstructContext",
    ),
    "ReconstructSiteParams": (
        "agenthicc.workflows.reconstruct_site",
        "ReconstructSiteParams",
    ),
    "ReconstructSiteRunner": (
        "agenthicc.workflows.reconstruct_site",
        "ReconstructSiteRunner",
    ),
    "ReconstructSiteWorkflow": (
        "agenthicc.workflows.reconstruct_site",
        "ReconstructSiteWorkflow",
    ),
    "ReconstructState": (
        "agenthicc.workflows.reconstruct_site",
        "ReconstructState",
    ),
}


def __getattr__(name: str) -> object:
    """Resolve a legacy export only when a caller actually requests it."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "BaseWorkflowRunner",
    "WorkflowConfig",
    "load_builtin_workflows",
    "load_python_workflows",
    "PhaseRole",
    "PhaseSpec",
    "WorkflowContext",
    "WorkflowEntry",
    "WorkflowParams",
    "WorkflowPlugin",
    "WorkflowRun",
    "PhaseRunRecord",
    "_parse_output_schema",
    "WorkflowRegistry",
    "build_workflow_registry",
    "WorkflowRunner",
    "PhaseAnnotation",
    "PhaseBoundaryError",
    "ResumeResolution",
    "checkpoint_phase_boundary",
    "publish_phase_annotation",
    "reconcile_phase_cursor",
    "CodePlan",
    "CodePlanParams",
    "CodePlanRunner",
    "CodePlanState",
    "CodePlanContext",
    "CreateWorkflow",
    "CreateWorkflowContext",
    "CreateWorkflowParams",
    "CreateWorkflowRunner",
    "CreateWorkflowState",
    "PhaseArtifact",
    "GoalContext",
    "GoalFlowParams",
    "GoalFlowRunner",
    "GoalState",
    "GoalFlowWorkflow",
    "MakeAgenthiccToolWorkflow",
    "MakeToolContext",
    "MakeToolParams",
    "MakeToolRunner",
    "MakeToolState",
    "ToolParam",
    "BookChapterInfo",
    "MakeBookContext",
    "MakeBookParams",
    "MakeBookRunner",
    "MakeBookState",
    "MakeBookWorkflow",
    "SiteImitateContext",
    "SiteImitateParams",
    "SiteImitateRunner",
    "SiteImitateState",
    "SiteImitateWorkflow",
    "ReconstructContext",
    "ReconstructSiteParams",
    "ReconstructSiteRunner",
    "ReconstructSiteWorkflow",
    "ReconstructState",
]
