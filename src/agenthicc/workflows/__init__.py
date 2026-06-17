"""Workflow system — code_plan state machine + generic infrastructure."""
from __future__ import annotations

from agenthicc.workflows.base import BaseWorkflowRunner
from agenthicc.workflows.plugin import (
    PhaseRunRecord,
    PhaseRole,
    PhaseSpec,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowPlugin,
    WorkflowRun,
    _parse_output_schema,
)
from agenthicc.workflows.registry import WorkflowRegistry, build_workflow_registry
from agenthicc.workflows.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflows.runner import WorkflowRunner, build_workflow_runner
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.code_plan import CodePlanRunner, CodePlanState, CodePlanContext

__all__ = [
    # Base
    "BaseWorkflowRunner",
    # Legacy types (backward compat)
    "PhaseRole", "PhaseSpec", "WorkflowContext", "WorkflowDefinition",
    "WorkflowPlugin", "WorkflowRun", "PhaseRunRecord", "_parse_output_schema",
    # Infrastructure
    "WorkflowRegistry", "build_workflow_registry",
    "load_builtin_workflows", "load_python_workflows",
    "WorkflowRunner", "build_workflow_runner",
    "WorkflowConfig",
    # code_plan state machine
    "CodePlanRunner", "CodePlanState", "CodePlanContext",
]
