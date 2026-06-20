"""Workflow system (PRD-87, PRD-112, PRD-116)."""
from __future__ import annotations

from agenthicc.workflows.base_runner import BaseWorkflowRunner
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflows.plugin import (
    PhaseRunRecord,
    PhaseRole,
    PhaseSpec,
    WorkflowContext,
    WorkflowEntry,
    WorkflowParams,
    WorkflowPlugin,
    WorkflowRun,
    _parse_output_schema,
)
from agenthicc.workflows.registry import WorkflowRegistry, build_workflow_registry
from agenthicc.workflows.default.runner import WorkflowRunner
from agenthicc.workflows.code_plan import (
    CodePlan, CodePlanParams,
    CodePlanRunner, CodePlanState, CodePlanContext,
)


__all__ = [
    "BaseWorkflowRunner",
    "WorkflowConfig",
    "load_builtin_workflows", "load_python_workflows",
    "PhaseRole", "PhaseSpec", "WorkflowContext", "WorkflowEntry",
    "WorkflowParams", "WorkflowPlugin", "WorkflowRun", "PhaseRunRecord",
    "_parse_output_schema",
    "WorkflowRegistry", "build_workflow_registry",
    "WorkflowRunner",
    "CodePlan", "CodePlanParams",
    "CodePlanRunner", "CodePlanState", "CodePlanContext",
]
