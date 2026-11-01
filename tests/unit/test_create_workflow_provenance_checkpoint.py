"""Regression tests for PRD-174 checkpoint-safe authoring provenance."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    context_from_payload,
    context_to_payload,
)
from agenthicc.workflows.create_workflow.authoring import (
    begin_authoring,
    inspect_existing_target,
    prepare_authoring,
    scan_package,
)
from agenthicc.workflows.create_workflow.state import CreateWorkflowContext, CreateWorkflowState

pytestmark = pytest.mark.unit


def test_authoring_provenance_round_trips_without_session_memory(tmp_path: Path) -> None:
    memory = MagicMock()
    snapshot = inspect_existing_target(tmp_path, run_id="run-123", workflow_name="review_flow")
    lease = prepare_authoring(
        tmp_path,
        run_id="run-123",
        workflow_name="review_flow",
        expected_original_fingerprint=snapshot.fingerprint,
    )
    begin_authoring(lease)
    package = tmp_path / ".agenthicc" / "workflows" / "review_flow"
    (package / "runner.py").write_text("WORKFLOW = True\\n", encoding="utf-8")
    manifest = scan_package(lease)
    context = CreateWorkflowContext(
        intent="build a review workflow",
        run_id="run-123",
        workflow_name="review_flow",
        generated_path=lease.target_path,
        shared_memory=memory,
        state=CreateWorkflowState.VALIDATE,
        phase_iteration=2,
        authoring_snapshot={"snapshot_id": "snapshot-1", "catalog_version": "v1"},
        selected_tools=["read_file", "ask_user"],
        dependency_summary={"browser": {"status": "not_configured"}},
        authoring_lease=lease,
        package_manifest=manifest,
        validation_evidence={"categories": {"cache_contract": "pass"}},
        publication={"status": "pending", "published_path": lease.target_path},
        question_metadata={"asked_count": 1, "status": "answered"},
    )
    payload = context_to_payload(context)
    assert "shared_memory" not in payload
    json.dumps(payload)

    restored = context_from_payload(
        payload,
        memory=memory,
    )
    assert isinstance(restored, CreateWorkflowContext)
    assert restored.shared_memory is memory
    assert restored.state is CreateWorkflowState.VALIDATE
    assert restored.authoring_snapshot["snapshot_id"] == "snapshot-1"
    assert restored.authoring_lease is not None
    assert restored.package_manifest is not None
    assert restored.package_manifest.fingerprint == manifest.fingerprint
    assert restored.publication["status"] == "pending"
    assert restored.question_metadata["status"] == "answered"


def test_legacy_payload_without_canonical_authoring_state_is_rejected() -> None:
    with pytest.raises(CheckpointValidationError, match="missing canonical authoring state"):
        context_from_payload(
            {
                "kind": "CreateWorkflowContext",
                "fields": {
                    "intent": "legacy",
                    "run_id": "old-run",
                    "state": {"__enum__": "DESIGN"},
                },
            },
            memory=None,
        )
