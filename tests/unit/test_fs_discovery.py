"""TUIR-01/02 and TOOLR-09: discovery and tool I/O stay off the event loop."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import agenthicc.tools.fs.discovery as discovery
from agenthicc.tools.fs import (
    GrepFilesTool,
    ListDirectoryTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope

pytestmark = pytest.mark.unit


@dataclass
class _Mode:
    name: str


class _Approvals:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[object] = []

    async def request_approval(self, request: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse

        self.requests.append(request)
        return ApprovalResponse(allowed=self.allowed, scope_grant="target_once")


def _context(
    root: Path, mode: str = "Yolo", approvals: _Approvals | None = None
) -> dict[str, object]:
    policy = WorkspaceAccessPolicy(
        WorkspaceScope.create(root),
        mode_provider=lambda: _Mode(mode),
        approval_service=approvals,  # type: ignore[arg-type]
    )
    return {"workspace_root": str(policy.scope.primary_root), "workspace_access": policy}


def _tree(root: Path, packages: int = 20, files: int = 50) -> int:
    count = 0
    for top in ("src", "node_modules"):
        for p in range(packages):
            directory = root / top / f"pkg{p}"
            directory.mkdir(parents=True, exist_ok=True)
            for f in range(files):
                (directory / f"mod{f}.py").write_text(f"def handler_{p}_{f}():\n    return {f}\n")
                count += 1
    return count


class _LagSampler:
    def __init__(self) -> None:
        self.worst = 0.0
        self._running = True

    async def run(self) -> None:
        while self._running:
            start = time.perf_counter()
            await asyncio.sleep(0.005)
            self.worst = max(self.worst, time.perf_counter() - start - 0.005)

    def stop(self) -> None:
        self._running = False


async def _max_stall(coroutine: object) -> tuple[object, float]:
    sampler = _LagSampler()
    task = asyncio.create_task(sampler.run())
    await asyncio.sleep(0.01)
    result = await coroutine  # type: ignore[misc]
    sampler.stop()
    await task
    return result, sampler.worst


async def test_search_over_a_large_tree_keeps_the_loop_responsive(tmp_path: Path) -> None:
    _tree(tmp_path)
    context = _context(tmp_path)

    result, stall = await _max_stall(
        SearchFilesTool().execute({"pattern": "*.py", "path": "."}, context)
    )

    assert result["ok"] is True  # type: ignore[index]
    matches = result["matches"]  # type: ignore[index]
    assert len(matches) == 1_000 and all(item.startswith("src/") for item in matches)
    assert stall < 0.25, f"loop stalled {stall * 1000:.0f} ms"


async def test_in_scope_entries_are_never_resolved_one_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TUIR-01 criterion 6: no canonicalisation or policy call per in-scope entry."""
    _tree(tmp_path, packages=3, files=20)
    (tmp_path / "src" / "link.py").symlink_to(tmp_path / "src" / "pkg0" / "mod0.py")
    context = _context(tmp_path)
    realpaths: list[str] = []
    original = os.path.realpath

    def counting(path: object, *args: object, **kwargs: object) -> str:
        realpaths.append(str(path))
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(discovery.os.path, "realpath", counting)
    authorized: list[str] = []
    policy = context["workspace_access"]
    assert isinstance(policy, WorkspaceAccessPolicy)
    original_authorize = policy.authorize

    async def counting_authorize(requested: object, **kwargs: object) -> Path:
        authorized.append(str(requested))
        return await original_authorize(requested, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(policy, "authorize", counting_authorize)
    result = await SearchFilesTool().execute({"pattern": "*.py", "path": "."}, context)

    assert result["count"] == 61
    walk_realpaths = [path for path in realpaths if path.endswith(".py")]
    assert walk_realpaths == [str(tmp_path / "src" / "link.py")]  # the symlink only
    assert authorized == ["."]  # the root; the in-scope symlink needs no policy call


@pytest.mark.parametrize(
    ("mode", "approve", "visible", "asked"),
    [("Plan", False, False, 0), ("Yolo", False, True, 0), ("Safe", True, True, 1)],
)
async def test_a_symlink_out_of_the_workspace_is_decided_by_the_policy(
    tmp_path: Path, mode: str, approve: bool, visible: bool, asked: int
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n")
    (workspace / "inside.py").write_text("y = 2\n")
    (workspace / "escape.py").symlink_to(outside / "secret.py")
    approvals = _Approvals(approve)

    result = await SearchFilesTool().execute(
        {"pattern": "*.py", "path": "."}, _context(workspace, mode, approvals)
    )

    assert ("escape.py" in result["matches"]) is visible
    assert "inside.py" in result["matches"]
    assert result["discovery"]["outside_scope"] == 1
    assert len(approvals.requests) == asked


async def test_a_directory_symlink_loop_terminates(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.py").write_text("")
    (tmp_path / "a" / "loop").symlink_to(tmp_path)

    result = await asyncio.wait_for(
        SearchFilesTool().execute({"pattern": "*.py", "path": "."}, _context(tmp_path)), 10
    )

    assert result["matches"] == ["a/f.py"]


async def test_git_ignored_directories_are_never_walked(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("generated/\n")
    _tree(tmp_path, packages=2, files=5)
    (tmp_path / "generated").mkdir()
    for n in range(500):
        (tmp_path / "generated" / f"g{n}.py").write_text("")

    result = await SearchFilesTool().execute({"pattern": "*.py", "path": "."}, _context(tmp_path))

    assert not any(item.startswith("generated/") for item in result["matches"])
    # Neither the ignored directory's 500 files nor node_modules' 10 were examined.
    assert result["discovery"]["examined"] < 100


async def test_default_ignores_apply_outside_git_and_can_be_bypassed(tmp_path: Path) -> None:
    _tree(tmp_path, packages=1, files=3)
    context = _context(tmp_path)

    default = await SearchFilesTool().execute({"pattern": "*.py"}, context)
    everything = await SearchFilesTool().execute(
        {"pattern": "*.py", "include_ignored": True}, context
    )

    assert all(item.startswith("src/") for item in default["matches"])
    assert any(item.startswith("node_modules/") for item in everything["matches"])


async def test_caps_stop_the_walk_and_say_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path, packages=3, files=20)
    monkeypatch.setattr(discovery, "MAX_EXAMINED_ENTRIES", 10)

    result = await SearchFilesTool().execute({"pattern": "*.py"}, _context(tmp_path))

    assert result["truncated"] is True
    assert result["discovery"]["truncated_by"] == "examined_cap"


async def test_walks_share_one_limiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.tools import fs

    _tree(tmp_path, packages=2, files=10)
    fs.configure_walk_limit(1)
    active = {"now": 0, "peak": 0}
    original = discovery._Walk.run

    def tracked(self: discovery._Walk) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            time.sleep(0.02)
            original(self)
        finally:
            active["now"] -= 1

    monkeypatch.setattr(discovery._Walk, "run", tracked)
    try:
        context = _context(tmp_path)
        await asyncio.gather(
            *(SearchFilesTool().execute({"pattern": "*.py"}, context) for _ in range(6))
        )
    finally:
        fs.configure_walk_limit(2)

    assert active["peak"] == 1


async def test_git_runs_once_per_repository_per_index_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _tree(tmp_path, packages=1, files=3)
    calls: list[list[str]] = []
    original = subprocess.run

    def counting(argv: list[str], *args: object, **kwargs: object) -> object:
        if "ls-files" in argv:
            calls.append(argv)
        return original(argv, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(discovery.subprocess, "run", counting)
    discovery._ignore_cache.clear()
    context = _context(tmp_path)
    for _ in range(3):
        await SearchFilesTool().execute({"pattern": "*.py"}, context)
        await GrepFilesTool().execute({"pattern": "handler"}, context)

    assert len(calls) == 1


async def test_grep_reports_files_and_refuses_a_bad_regex(tmp_path: Path) -> None:
    _tree(tmp_path, packages=1, files=3)
    context = _context(tmp_path)

    found = await GrepFilesTool().execute({"pattern": "handler_0_1"}, context)
    bad = await GrepFilesTool().execute({"pattern": "("}, context)

    assert found["count"] == 1 and found["files_matched"] == 1
    assert bad["ok"] is False and bad["error_code"] == "invalid_argument"


async def test_a_flat_listing_still_shows_literal_contents(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "a.txt").write_text("a")

    result = await ListDirectoryTool().execute({"path": "."}, _context(tmp_path))

    assert [entry["path"] for entry in result["entries"]] == ["a.txt", "node_modules"]


# ── TOOLR-09 ─────────────────────────────────────────────────────────────────


async def test_checksum_of_a_large_file_does_not_stall_the_loop(tmp_path: Path) -> None:
    from agenthicc.tools.fs.agent_tools import checksum_file

    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(200 * 1024 * 1024)
    os.chdir(tmp_path)

    result, stall = await _max_stall(checksum_file(path="big.bin"))

    assert result["ok"] is True  # type: ignore[index]
    assert stall < 0.25, f"loop stalled {stall * 1000:.0f} ms"


async def test_a_write_between_read_and_stat_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tools.fs import file_cache

    cache = file_cache.WorkspaceFileCache(tmp_path / "cache.db")
    monkeypatch.setattr(file_cache, "_cache", cache)
    target = tmp_path / "f.txt"
    target.write_text("old")
    original_read = Path.read_text

    def racing_read(self: Path, *args: object, **kwargs: object) -> str:
        text = original_read(self, *args, **kwargs)  # type: ignore[arg-type]
        if self == target:
            target.write_text("new content")  # lands after the read, before the stat
        return text

    monkeypatch.setattr(Path, "read_text", racing_read)
    first = await ReadFileTool().execute({"path": str(target)}, _context(tmp_path))
    monkeypatch.setattr(Path, "read_text", original_read)
    second = await ReadFileTool().execute({"path": str(target)}, _context(tmp_path))

    assert first["content"] == "old" and "cached" not in first
    assert second["content"] == "new content"


async def test_a_write_refreshes_the_cache_with_what_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tools.fs import file_cache

    monkeypatch.setattr(file_cache, "_cache", file_cache.WorkspaceFileCache(tmp_path / "c.db"))
    context = _context(tmp_path)
    await WriteFileTool().execute({"path": "f.txt", "content": "a\r\nb\r\n"}, context)

    result = await ReadFileTool().execute({"path": "f.txt"}, context)

    assert result["cached"] is True and result["content"] == "a\nb\n"


async def test_source_search_does_not_stall_the_loop() -> None:
    from agenthicc.tools.introspect.agent_tools import search_agenthicc_source

    result, stall = await _max_stall(search_agenthicc_source(query="def "))

    assert result["ok"] is True  # type: ignore[index]
    assert stall < 0.25, f"loop stalled {stall * 1000:.0f} ms"
