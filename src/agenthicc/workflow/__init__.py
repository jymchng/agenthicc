"""Workflow system — graph-based agentic pipelines (PRD-101)."""
from __future__ import annotations

from agenthicc.workflow.plugin import (
    DataBus,
    EdgeGate,
    EdgeSpec,
    NodeResult,
    PhaseNode,
    PhaseRole,
    PhaseRunRecord,
    WorkflowGraph,
    WorkflowPlugin,
    WorkflowRun,
)
from agenthicc.workflow.registry import WorkflowRegistry, build_workflow_registry
from agenthicc.workflow.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflow.runner import WorkflowRunner, build_workflow_runner
from agenthicc.workflow.config import WorkflowConfig

__all__ = [
    "PhaseRole", "PhaseNode", "PhaseRunRecord",
    "EdgeGate", "EdgeSpec",
    "WorkflowGraph", "WorkflowPlugin", "WorkflowRun",
    "DataBus", "NodeResult",
    "WorkflowRegistry", "build_workflow_registry",
    "load_builtin_workflows", "load_python_workflows",
    "WorkflowRunner", "build_workflow_runner",
    "WorkflowConfig",
]
