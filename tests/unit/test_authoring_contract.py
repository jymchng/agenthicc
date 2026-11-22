"""The shared verdict, repair budget, and scoped generation contract."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, cast

import pytest

from agenthicc.tools.base import tool_name
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.workflows.authoring.budget import RepairBudget, RepairBudgetExhausted
from agenthicc.workflows.authoring.scoped_tools import make_scoped_writer_tools
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport, decide

pytestmark = pytest.mark.unit

_Writer = Callable[[str, str], Awaitable[dict[str, object]]]


@pytest.mark.parametrize(
    ("severity", "action", "expected"),
    [
        ("error", "approve", "reject"),
        ("error", "reject", "reject"),
        ("warning", "approve", "approve"),
        ("warning", "reject", "reject"),
    ],
)
def test_runner_verdict_requires_a_clean_report_and_agent_approval(
    severity: Literal["error", "warning"],
    action: Literal["approve", "reject"],
    expected: Literal["approve", "reject"],
) -> None:
    report = ValidationReport(
        digest="a" * 64,
        findings=(Finding("tool.name_mismatch", severity, "example.py", 3, "Name mismatch"),),
    )

    assert decide(report, action) == expected


def test_repair_budget_charges_only_the_permitted_routes() -> None:
    original = RepairBudget(max_cycles=3)
    budget = original
    for used in range(1, 4):
        budget = budget.charge()
        assert budget.used == used
    with pytest.raises(RepairBudgetExhausted, match="limit 3"):
        budget.charge()
    assert original.used == 0
    assert budget.used == 3


@pytest.fixture
def scoped_write(tmp_path: Path) -> _Writer:
    return _writer(tmp_path)


def _writer(path: Path) -> _Writer:
    tools = make_scoped_writer_tools(
        path, suffixes=frozenset({".py"}), max_file_bytes=32, max_files=1
    )
    return cast(
        _Writer, next(candidate for candidate in tools if tool_name(candidate) == "write_file")
    )


async def test_scoped_writer_reports_each_rejection(tmp_path: Path, scoped_write: _Writer) -> None:
    for path, content, code in [
        ("../outside.py", "", "path_traversal"),
        (str(tmp_path / "absolute.py"), "", "absolute_path"),
        ("file.md", "", "invalid_suffix"),
        ("large.py", "x" * 33, "file_too_large"),
        (".agenthicc-pending.json", "", "runner_metadata"),
        ("manifest.json", "", "runner_metadata"),
    ]:
        result = await scoped_write(path, content)
        assert result["ok"] is False
        assert result["code"] == code
    assert (await scoped_write("candidate.py", "value = 1"))["ok"] is True
    assert (await scoped_write("other.py", "value = 2"))["code"] == "file_limit"
    assert (await scoped_write("candidate.py", "value = 3"))["ok"] is True
    assert (tmp_path / "candidate.py").read_text(encoding="utf-8") == "value = 3"


async def test_scoped_writer_rejects_symlink(tmp_path: Path, scoped_write: _Writer) -> None:
    outside = tmp_path / "original.txt"
    outside.write_text("original", encoding="utf-8")
    try:
        (tmp_path / "candidate.py").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    result = await scoped_write("candidate.py", "changed")
    assert result["code"] == "symlink"
    assert outside.read_text(encoding="utf-8") == "original"


async def test_scoped_writer_cannot_author_under_the_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".agenthicc" / "workflows" / "user-workflow"
    root.mkdir(parents=True)
    result = await _writer(root)("candidate.py", "value = 1")
    assert result["code"] == "outside_workspace"
    assert not (root / "candidate.py").exists()


async def test_pending_workflow_writer_uses_its_runner_owned_scope(tmp_path: Path) -> None:
    root = tmp_path / ".agenthicc" / "workflows" / "new-workflow"
    root.mkdir(parents=True)
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path))
    token = set_current_workspace_access(policy)
    try:
        direct = await policy.authorize_tool("write_file", {"path": str(root / "candidate.py")})
        authored = await _writer(root)("candidate.py", "value = 1")
    finally:
        reset_current_workspace_access(token)
    assert direct.code == "approval_unavailable"
    assert authored["ok"] is True


async def test_concurrent_scoped_writes_share_the_file_limit(
    tmp_path: Path, scoped_write: _Writer
) -> None:
    first, second = await asyncio.gather(
        scoped_write("first.py", "one"), scoped_write("second.py", "two")
    )
    assert sum(result["ok"] is True for result in (first, second)) == 1
    assert sum(result.get("code") == "file_limit" for result in (first, second)) == 1
    assert await asyncio.to_thread(lambda: len(list(tmp_path.glob("*.py")))) == 1


def test_authoring_checkpoint_requires_persisted_budget() -> None:
    from agenthicc.workflows.checkpoint import (
        CheckpointValidationError,
        context_from_payload,
        context_to_payload,
    )
    from agenthicc.workflows.create_workflow.state import CreateWorkflowContext

    original = CreateWorkflowContext("Author a workflow", "run", repair_budget=RepairBudget(3, 2))
    payload = context_to_payload(original)
    restored = context_from_payload(payload)
    assert isinstance(restored, CreateWorkflowContext)
    assert restored.repair_budget == original.repair_budget

    fields = payload["fields"]
    assert isinstance(fields, dict)
    del fields["repair_budget"]
    with pytest.raises(CheckpointValidationError, match="missing its repair budget"):
        context_from_payload(payload)
