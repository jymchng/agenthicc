"""Unit tests for the PRD-168 mode-aware workspace boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio

import pytest

from agenthicc.reactive import Signal
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspacePathStatus,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)

pytestmark = pytest.mark.unit


@dataclass
class _Mode:
    name: str


class _Approval:
    def __init__(self, *, grant: str = "target_once") -> None:
        self.grant = grant
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse

        self.requests.append(request)
        return ApprovalResponse(allowed=True, scope_grant=self.grant)


def _scope(tmp_path: Path) -> tuple[Path, Path, WorkspaceScope]:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    return workspace, outside, WorkspaceScope.create(workspace)


def test_scope_canonicalizes_symlinks_and_classifies_outside(tmp_path: Path) -> None:
    workspace, outside, scope = _scope(tmp_path)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to(outside, target_is_directory=True)

    resolved = scope.resolve("link/secret.txt")

    assert resolved.status is WorkspacePathStatus.OUTSIDE_SCOPE
    assert resolved.absolute == (outside / "secret.txt").resolve()
    assert resolved.root_id is None


def test_pattern_scope_uses_literal_directory_prefix(tmp_path: Path) -> None:
    workspace, outside, scope = _scope(tmp_path)

    outside_pattern = scope.resolve_pattern("../outside/*.txt", base=workspace)
    inside_pattern = scope.resolve_pattern("src/*.txt", base=workspace)

    assert outside_pattern.status is WorkspacePathStatus.OUTSIDE_SCOPE
    assert outside_pattern.absolute == outside.resolve()
    assert inside_pattern.status is WorkspacePathStatus.IN_SCOPE
    assert inside_pattern.absolute == (workspace / "src").resolve()


@pytest.mark.asyncio
async def test_plan_hard_blocks_outside_access_without_prompt(tmp_path: Path) -> None:
    _, outside, scope = _scope(tmp_path)
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Plan"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await policy.authorize_tool("read_file", {"path": str(outside / "secret.txt")})

    assert result.allowed is False
    assert result.code == "outside_workspace"
    assert approval.requests == []


@pytest.mark.asyncio
async def test_safe_outside_preflight_does_not_probe_target_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, outside, scope = _scope(tmp_path)
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )
    original_exists = Path.exists
    probed: list[Path] = []

    def _track_exists(path: Path) -> bool:
        if path == target:
            probed.append(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", _track_exists)
    result = await policy.authorize_tool("read_file", {"path": str(target)})

    assert result.allowed is True
    assert probed == []
    assert getattr(approval.requests[0], "workspace_access")[0].exists is None


@pytest.mark.asyncio
async def test_safe_approval_is_exact_and_target_turn_grant_resets(tmp_path: Path) -> None:
    _, outside, scope = _scope(tmp_path)
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    approval = _Approval(grant="target_turn")
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    first = await policy.authorize(str(target), operation="read", tool_name="read_file")
    second = await policy.authorize(str(target), operation="read", tool_name="read_file")
    policy.reset_turn_memory()
    third = await policy.authorize(str(target), operation="read", tool_name="read_file")

    assert first == target.resolve() == second == third
    assert len(approval.requests) == 2
    request = approval.requests[0]
    access = getattr(request, "workspace_access")
    assert access[0].canonical == target.resolve()
    assert access[0].operation == "read"


@pytest.mark.asyncio
async def test_yolo_allows_outside_access_without_approval(tmp_path: Path) -> None:
    _, outside, scope = _scope(tmp_path)
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Yolo"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    result = await policy.authorize_tool("read_file", {"path": str(target)})

    assert result.allowed is True
    assert result.code == "yolo_bypass"
    assert approval.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_mode", "allowed", "code"),
    [("Yolo", True, "yolo_bypass"), ("Plan", False, "outside_workspace")],
)
async def test_pending_scope_approval_reacts_to_mode_switch(
    tmp_path: Path, new_mode: str, allowed: bool, code: str
) -> None:
    _, outside, scope = _scope(tmp_path)
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    active_mode = Signal(_Mode("Safe"))
    cancelled = asyncio.Event()

    class _BlockingApproval:
        async def request_approval(self, request: object) -> object:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=active_mode,
        approval_service=_BlockingApproval(),  # type: ignore[arg-type]
    )
    task = asyncio.create_task(policy.authorize_tool("read_file", {"path": str(target)}))
    await asyncio.sleep(0)
    active_mode.set(_Mode(new_mode))
    result = await asyncio.wait_for(task, timeout=1)

    assert result.allowed is allowed
    assert result.code == code
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_approved_symlink_target_change_fails_closed(tmp_path: Path) -> None:
    workspace, outside, scope = _scope(tmp_path)
    first = outside / "first"
    second = outside / "second"
    first.mkdir()
    second.mkdir()
    (first / "report.txt").write_text("first", encoding="utf-8")
    (second / "report.txt").write_text("second", encoding="utf-8")
    link = workspace / "report-link"
    link.symlink_to(first, target_is_directory=True)

    class _RacingApproval(_Approval):
        async def request_approval(self, request: object) -> object:
            response = await super().request_approval(request)
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
            return response

    approval = _RacingApproval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="target_changed"):
        await policy.authorize("report-link/report.txt", operation="read", tool_name="read_file")


@pytest.mark.asyncio
async def test_scope_policy_handles_batch_and_command_arguments(tmp_path: Path) -> None:
    workspace, outside, scope = _scope(tmp_path)
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Plan"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    assert policy.requests_for_tool(
        "batch_write", {"files": [{"path": str(outside / "x"), "content": "x"}]}
    ) == ((str(outside / "x"), "write"),)
    assert policy.requests_for_tool(
        "batch_move",
        {"moves": [{"source": str(workspace / "x"), "destination": str(outside / "x")}]},
    ) == (
        (str(workspace / "x"), "write"),
        (str(outside / "x"), "write"),
    )
    assert policy.requests_for_tool("run_command", {"cwd": str(workspace)}) == (
        (str(workspace), "execute_cwd"),
    )
    result = await policy.authorize_tool(
        "batch_write", {"files": [{"path": str(outside / "x"), "content": "x"}]}
    )
    assert result.code == "outside_workspace"


@pytest.mark.asyncio
async def test_git_adapter_executes_canonical_root_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from agenthicc.tools.git import GitDiffTool

    workspace, outside, scope = _scope(tmp_path)
    target = outside / "report.txt"
    target.write_text("report", encoding="utf-8")
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )
    run_git = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr("agenthicc.tools.git._run_git", run_git)

    result = await GitDiffTool().execute(
        {"path": str(target)},
        {"workspace_root": str(workspace), "workspace_access": policy},
    )

    assert result["diff"] == ""
    called_root, called_args = run_git.call_args.args[:2]
    assert called_root == str(workspace.resolve())
    assert str(target.resolve()) in called_args


@pytest.mark.asyncio
async def test_run_tests_passes_canonical_target_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from agenthicc.tools.exec import RunTestsTool

    workspace, outside, scope = _scope(tmp_path)
    target = outside / "tests"
    target.mkdir()
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )
    run_proc = AsyncMock(return_value={"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr("agenthicc.tools.exec._run_proc", run_proc)

    await RunTestsTool().execute(
        {"path": str(target)},
        {"workspace_root": str(workspace), "workspace_access": policy},
    )

    command = run_proc.call_args.args[0]
    assert str(target.resolve()) in command


@pytest.mark.asyncio
async def test_task_local_policy_binding_is_restored(tmp_path: Path) -> None:
    _, _, scope = _scope(tmp_path)
    policy = WorkspaceAccessPolicy(scope)
    token = set_current_workspace_access(policy)
    try:
        from agenthicc.tools.workspace_access import current_workspace_access

        assert current_workspace_access() is policy
    finally:
        reset_current_workspace_access(token)

    from agenthicc.tools.workspace_access import current_workspace_access

    assert current_workspace_access() is None


def test_legacy_tool_sandbox_cannot_sync_bypass_safe_scope(tmp_path: Path) -> None:
    from agenthicc.tools.sandbox import ToolSandbox

    workspace, outside, scope = _scope(tmp_path)
    safe = WorkspaceAccessPolicy(scope, mode_provider=lambda: _Mode("Safe"))
    yolo = WorkspaceAccessPolicy(scope, mode_provider=lambda: _Mode("Yolo"))

    with pytest.raises(PermissionError, match="asynchronous workspace approval"):
        ToolSandbox(workspace_access=safe).resolve(str(outside / "file.txt"))
    assert (
        ToolSandbox(workspace_access=yolo).resolve(str(outside / "file.txt"))
        == (outside / "file.txt").resolve()
    )


@pytest.mark.asyncio
async def test_mention_injection_uses_the_same_outside_scope_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inline_to_thread(func: object, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    workspace, outside, scope = _scope(tmp_path)
    target = outside / "report.md"
    target.write_text("parent report", encoding="utf-8")
    approval = _Approval()
    policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=lambda: _Mode("Safe"),
        approval_service=approval,  # type: ignore[arg-type]
    )

    from agenthicc.mentions.injector import InjectionConfig, build_context_prefix

    prefix, injected = await build_context_prefix(
        "Please inspect @../outside/report.md",
        cwd=workspace,
        cfg=InjectionConfig(cwd=workspace, workspace_access=policy),
    )

    assert "parent report" in prefix
    assert injected[0].ok is True
    assert len(approval.requests) == 1
    assert getattr(approval.requests[0], "workspace_access")[0].operation == "read"
