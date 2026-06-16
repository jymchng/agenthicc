"""Workflow system — phase-based agentic pipelines (PRD-81, PRD-87)."""
from __future__ import annotations

from agenthicc.workflow.plugin import (
    PhaseOutput,
    PhaseRole,
    PhaseRunRecord,
    PhaseSpec,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowPlugin,
    WorkflowRun,
    _parse_output_schema,
)
from agenthicc.workflow.registry import WorkflowRegistry, build_workflow_registry
from agenthicc.workflow.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflow.runner import WorkflowRunner, build_workflow_runner
from agenthicc.workflow.config import WorkflowConfig

__all__ = [
    "PhaseRole", "PhaseSpec", "WorkflowContext", "WorkflowDefinition",
    "WorkflowPlugin", "WorkflowRun", "PhaseOutput", "PhaseRunRecord",
    "_parse_output_schema",
    "WorkflowRegistry", "build_workflow_registry",
    "load_builtin_workflows", "load_python_workflows",
    "WorkflowRunner", "build_workflow_runner",
    "WorkflowConfig",
]
