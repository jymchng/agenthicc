"""Regression tests for PRD-174 checkpoint-safe authoring provenance."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agenthicc.workflows.checkpoint import context_from_payload, context_to_payload
from agenthicc.workflows.create_workflow.state import CreateWorkflowContext, CreateWorkflowState

pytestmark = pytest.mark.unit


def test_authoring_provenance_round_trips_without_session_memory() -> None:
    memory = MagicMock()
    context = CreateWorkflowContext(
        intent="build a review workflow",
        run_id="run-123",
        workflow_name="review_flow",
        shared_memory=memory,
        state=CreateWorkflowState.VALIDATE,
        phase_iteration=2,
        authoring_snapshot={"snapshot_id": "snapshot-1", "catalog_version": "v1"},
        selected_tools=["read_file", "ask_user"],
        dependency_summary={"browser": {"status": "not_configured"}},
        draft_manifest={"fingerprint": "draft-1", "files": []},
        draft_fingerprint="draft-1",
        validation_evidence={"categories": {"cache_contract": "pass"}},
        publication={"status": "published", "published_path": "/workspace/workflows/review_flow"},
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
    assert restored.draft_fingerprint == "draft-1"
    assert restored.publication["status"] == "published"
    assert restored.question_metadata["status"] == "answered"


def test_legacy_payload_gets_safe_empty_provenance_defaults() -> None:
    restored = context_from_payload(
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
    assert isinstance(restored, CreateWorkflowContext)
    assert restored.authoring_snapshot == {}
    assert restored.selected_tools == []
    assert restored.publication == {}
