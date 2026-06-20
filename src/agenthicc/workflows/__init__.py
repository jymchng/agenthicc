"""Workflow system — code_plan state machine + generic infrastructure."""
from __future__ import annotations

from agenthicc.workflows.base import BaseWorkflowRunner
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflows.plugin import (
    PhaseRunRecord,
    PhaseRole,
    PhaseSpec,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowParams,
    WorkflowPlugin,
    WorkflowRun,
    _parse_output_schema,
)
from agenthicc.workflows.registry import WorkflowRegistry, build_workflow_registry
# Canonical locations (PRD-112):
from agenthicc.workflows.default.runner import WorkflowRunner, build_workflow_runner
from agenthicc.workflows.default.definition import Architect, PlanOnly, ReviewOnly, Supervised
from agenthicc.workflows.code_plan import (
    CodePlan, CodePlanParams,
    CodePlanRunner, CodePlanState, CodePlanContext,
)

__all__ = [
    "BaseWorkflowRunner",
    "WorkflowConfig",
    "load_builtin_workflows", "load_python_workflows",
    "PhaseRole", "PhaseSpec", "WorkflowContext", "WorkflowDefinition",
    "WorkflowParams", "WorkflowPlugin", "WorkflowRun", "PhaseRunRecord", "_parse_output_schema",
    "WorkflowRegistry", "build_workflow_registry",
    "WorkflowRunner", "build_workflow_runner",
    "Architect", "PlanOnly", "ReviewOnly", "Supervised",
    "CodePlan", "CodePlanParams",
    "CodePlanRunner", "CodePlanState", "CodePlanContext",
]
