"""Unit tests for persisting and replaying plan approval modes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.testing.cassette import ApprovalEntry
from agenthicc.testing.mock_approval import MockApprovalService
from agenthicc.testing.recording_approval import RecordingApprovalService
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse

pytestmark = pytest.mark.unit


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="Plan Review",
        tool_use_id="plan-1",
        tool_input={"plan": "plan"},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="plan_review",
        mode_options=("Safe", "Yolo"),
    )


@pytest.mark.asyncio
async def test_recording_proxy_forwards_and_records_selected_mode(tmp_path: Path) -> None:
    inner = MagicMock()
    inner.request_approval = AsyncMock(return_value=ApprovalResponse(True, mode="Yolo"))
    path = tmp_path / "approvals.jsonl"
    recorder = RecordingApprovalService(inner, path)

    response = await recorder.request_approval(_request())
    recorder.respond(True, mode="Yolo")

    assert response.mode == "Yolo"
    inner.respond.assert_called_once_with(
        True,
        remember=False,
        remember_all=False,
        message="",
        mode="Yolo",
    )
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["mode"] == "Yolo"


def test_cassette_mode_is_optional_for_legacy_recordings() -> None:
    legacy = ApprovalEntry.from_dict({"index": 0, "allowed": True})
    modern = ApprovalEntry.from_dict({"index": 1, "allowed": True, "mode": "Yolo"})

    assert legacy.mode is None
    assert modern.mode == "Yolo"


@pytest.mark.asyncio
async def test_mock_approval_replays_mode_and_defaults_when_exhausted() -> None:
    service = MockApprovalService(
        [ApprovalEntry(0, "plan_review", "Plan Review", True, "", False, False, "Yolo")]
    )

    selected = await service.request_approval(_request())
    exhausted = await service.request_approval(_request())

    assert selected.mode == "Yolo"
    assert exhausted.allowed is True
    assert exhausted.mode is None
