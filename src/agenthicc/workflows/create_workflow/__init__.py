"""create_workflow — the meta-workflow that authors new custom workflows.

Structure mirrors ``agenthicc.workflows.code_plan``:

* :mod:`.state` — typed :class:`CreateWorkflowState` enum, :class:`PhaseArtifact`,
  and the :class:`CreateWorkflowContext` carried across phases.
* :mod:`.phase_tools` — the ``@tool()`` closures that are the *only* way a phase
  can transition.
* :mod:`.inspection_tools` — read-only tools exposing the real authoring API.
* :mod:`.validation` — deterministic import-and-check of the generated package.
* :mod:`.runner` — the outer state machine and its per-phase inner loops.
* :mod:`.definition` — the :class:`CreateWorkflow` plugin and its params.
"""

from __future__ import annotations

from agenthicc.workflows.create_workflow.definition import CreateWorkflow, CreateWorkflowParams
from agenthicc.workflows.create_workflow.inspection_tools import make_inspection_tools
from agenthicc.workflows.create_workflow.phase_tools import (
    make_design_tools,
    make_generation_tools,
    make_validation_tools,
    validate_workflow_name,
)
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.catalog import (
    AUTHORING_CATALOG_VERSION,
    AuthoringSnapshotCache,
    AuthoringSnapshot,
    ToolAccessDecision,
    ToolCatalogEntry,
    build_authoring_snapshot,
    build_tool_catalog,
    explain_tool_access,
)
from agenthicc.workflows.create_workflow.authoring import (
    PENDING_MARKER_NAME,
    AuthoringError,
    AuthoringLease,
    ExistingWorkflowTarget,
    PackageFile,
    PackageManifest,
    begin_authoring,
    canonical_workflow_path,
    finalize_authoring,
    inspect_existing_target,
    prepare_authoring,
    read_authoring_lease,
    restore_authoring,
    scan_committed_package,
    scan_package,
    verify_authoring,
)
from agenthicc.workflows.create_workflow.generation_tools import (
    make_generation_writer_tools,
    make_package_read_tools,
)
from agenthicc.workflows.create_workflow.smoke import (
    SmokeCheck,
    SmokeReport,
    run_generated_workflow_smoke,
)
from agenthicc.workflows.create_workflow.state import (
    CreateWorkflowContext,
    CreateWorkflowState,
    PhaseArtifact,
)
from agenthicc.workflows.create_workflow.validation import (
    ValidationReport,
    validate_workflow_file,
)

__all__ = [
    "CreateWorkflowState",
    "CreateWorkflowContext",
    "PhaseArtifact",
    "CreateWorkflowRunner",  # run() → CreateWorkflowContext
    "CreateWorkflow",
    "CreateWorkflowParams",
    "ValidationReport",
    "validate_workflow_file",
    "validate_workflow_name",
    "make_design_tools",
    "make_generation_tools",
    "make_validation_tools",
    "make_inspection_tools",
    "AUTHORING_CATALOG_VERSION",
    "AuthoringSnapshotCache",
    "AuthoringSnapshot",
    "ToolAccessDecision",
    "ToolCatalogEntry",
    "build_authoring_snapshot",
    "build_tool_catalog",
    "explain_tool_access",
    "PENDING_MARKER_NAME",
    "AuthoringError",
    "AuthoringLease",
    "ExistingWorkflowTarget",
    "PackageFile",
    "PackageManifest",
    "begin_authoring",
    "canonical_workflow_path",
    "finalize_authoring",
    "inspect_existing_target",
    "prepare_authoring",
    "read_authoring_lease",
    "restore_authoring",
    "scan_committed_package",
    "scan_package",
    "verify_authoring",
    "make_generation_writer_tools",
    "make_package_read_tools",
    "SmokeCheck",
    "SmokeReport",
    "run_generated_workflow_smoke",
]
