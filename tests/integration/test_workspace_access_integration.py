"""Integration coverage for policy enforcement at concrete tool adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import asyncio

import pytest

from agenthicc.tools.exec import RunCommandTool
from agenthicc.tools.fs import (
    GrepFilesTool,
    ListDirectoryTool,
    MoveFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope

pytestmark = pytest.mark.integration


@dataclass
class _Mode:
    name: str


class _Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse

        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed, scope_grant="target_once")


def _context(policy: WorkspaceAccessPolicy) -> dict[str, object]:
    return {
        "workspace_root": str(policy.scope.primary_root),
        "workspace_access": policy,
    }


@pytest.mark.asyncio
async def test_read_and_write_adapters_share_one_approved_parent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inline_to_thread(func: object, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("before", encoding="utf-8")
    approval = _Approval(True)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    read = await ReadFileTool().execute({"path": str(target)}, _context(policy))
    write = await WriteFileTool().execute(
        {"path": str(target), "content": "after"}, _context(policy)
    )

    assert read["content"] == "before"
    assert write["ok"] is True
    assert target.read_text(encoding="utf-8") == "after"
    assert len(approval.requests) == 2
    assert all(getattr(req, "workspace_access") for req in approval.requests)


@pytest.mark.asyncio
async def test_plan_denies_concrete_outside_write_before_io(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "new.txt"
    approval = _Approval(True)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Plan"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await WriteFileTool().execute(
        {"path": str(target), "content": "must not write"}, _context(policy)
    )

    assert result["ok"] is False
    assert "outside_workspace" in str(result["error"])
    assert not target.exists()
    assert approval.requests == []


@pytest.mark.asyncio
async def test_command_cwd_is_checked_by_the_same_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    approval = _Approval(True)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await RunCommandTool().execute(
        {
            "argv": [sys.executable, "-c", "import os; print(os.getcwd())"],
            "cwd": str(outside),
        },
        _context(policy),
    )

    assert result["ok"] is True
    assert str(outside.resolve()) in str(result["stdout"])
    assert len(approval.requests) == 1


@pytest.mark.asyncio
async def test_discovered_symlink_targets_are_checked_before_metadata_or_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inline_to_thread(func: object, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("do not expose", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)
    approval = _Approval(False)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    listed = await ListDirectoryTool().execute({"path": "."}, _context(policy))
    searched = await SearchFilesTool().execute(
        {"path": ".", "pattern": "*", "recursive": True}, _context(policy)
    )
    grepped = await GrepFilesTool().execute(
        {"path": ".", "pattern": "do not expose", "recursive": True}, _context(policy)
    )

    assert all(entry["path"] != "link.txt" for entry in listed["entries"])
    assert searched["matches"] == []
    assert grepped["matches"] == []
    assert len(approval.requests) == 3


@pytest.mark.asyncio
async def test_glob_pattern_escape_is_checked_before_enumeration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("do not expose", encoding="utf-8")
    approval = _Approval(False)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await ListDirectoryTool().execute(
        {"path": ".", "pattern": "../parent-data/*"},
        _context(policy),
    )

    assert result["ok"] is False
    assert "approval_denied" in str(result["error"])
    assert len(approval.requests) == 1
    access = getattr(approval.requests[0], "workspace_access")
    assert access[0].canonical == outside.resolve()


@pytest.mark.asyncio
async def test_move_authorizes_in_scope_source_and_outside_destination_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inline_to_thread(func: object, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "parent-data"
    workspace.mkdir()
    outside.mkdir()
    source = workspace / "source.txt"
    destination = outside / "moved.txt"
    source.write_text("move me", encoding="utf-8")
    approval = _Approval(True)
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(workspace),
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await MoveFileTool().execute(
        {"source": str(source), "destination": str(destination)}, _context(policy)
    )

    assert result["ok"] is True
    assert destination.read_text(encoding="utf-8") == "move me"
    assert not source.exists()
    assert len(approval.requests) == 1
