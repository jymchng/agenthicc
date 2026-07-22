"""Unit tests for WorkspaceView and NetworkGuard (PRD-04 sandbox primitives)."""

from __future__ import annotations

import os

import asyncio
from unittest.mock import patch

import pytest

from agenthicc.tools.sandbox import NetworkGuard, ResourceLimits, ToolSandbox, WorkspaceView

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# WorkspaceView
# ---------------------------------------------------------------------------


class TestWorkspaceViewResolve:
    def test_valid_relative_path(self, tmp_path):
        view = WorkspaceView(tmp_path)
        resolved = view.resolve("subdir/file.txt")
        assert resolved == tmp_path / "subdir" / "file.txt"

    def test_path_traversal_raises(self, tmp_path):
        view = WorkspaceView(tmp_path)
        with pytest.raises(PermissionError):
            view.resolve("../../etc/passwd")

    def test_absolute_path_outside_root_raises(self, tmp_path):
        view = WorkspaceView(tmp_path)
        with pytest.raises(PermissionError):
            view.resolve("/etc/passwd")

    def test_symlink_escape_raises(self, tmp_path):
        """A symlink inside the workspace pointing outside must be caught."""
        outside = tmp_path.parent / "outside_target.txt"
        outside.write_text("secret")
        link = tmp_path / "escape_link"
        os.symlink(outside, link)
        view = WorkspaceView(tmp_path)
        with pytest.raises(PermissionError):
            view.resolve("escape_link")

    def test_valid_absolute_path_inside_root(self, tmp_path):
        view = WorkspaceView(tmp_path)
        target = tmp_path / "inner" / "file.txt"
        resolved = view.resolve(str(target))
        assert resolved == target

    def test_root_itself_is_valid(self, tmp_path):
        view = WorkspaceView(tmp_path)
        assert view.resolve(".") == tmp_path


class TestWorkspaceViewReadWriteText:
    def test_round_trip(self, tmp_path):
        view = WorkspaceView(tmp_path)
        view.write_text("hello.txt", "hello world")
        assert view.read_text("hello.txt") == "hello world"

    def test_write_creates_parents(self, tmp_path):
        view = WorkspaceView(tmp_path)
        view.write_text("a/b/c.txt", "deep")
        assert view.read_text("a/b/c.txt") == "deep"

    def test_write_traversal_raises(self, tmp_path):
        view = WorkspaceView(tmp_path)
        with pytest.raises(PermissionError):
            view.write_text("../../evil.txt", "bad")

    def test_read_traversal_raises(self, tmp_path):
        view = WorkspaceView(tmp_path)
        with pytest.raises(PermissionError):
            view.read_text("../../etc/passwd")

    def test_exists_and_list_dir(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        view = WorkspaceView(tmp_path)
        assert view.exists("a.txt") is True
        assert view.exists("missing.txt") is False
        assert view.list_dir() == ["a.txt"]


# ---------------------------------------------------------------------------
# NetworkGuard
# ---------------------------------------------------------------------------


class TestNetworkGuard:
    def test_allowed_exact_domain_passes(self):
        guard = NetworkGuard(["example.com"])
        guard.check("https://example.com/path")  # should not raise

    def test_subdomain_passes(self):
        guard = NetworkGuard(["example.com"])
        guard.check("https://api.example.com/v1/resource")  # should not raise

    def test_denied_domain_raises(self):
        guard = NetworkGuard(["example.com"])
        with pytest.raises(PermissionError):
            guard.check("https://evil.com/steal")

    def test_invalid_url_no_host_raises(self):
        guard = NetworkGuard(["example.com"])
        with pytest.raises(PermissionError):
            guard.check("not-a-url-at-all")

    def test_multiple_allowed_domains(self):
        guard = NetworkGuard(["example.com", "trusted.org"])
        guard.check("https://api.trusted.org/data")  # should not raise

    def test_partial_suffix_does_not_match(self):
        """notexample.com must not match example.com."""
        guard = NetworkGuard(["example.com"])
        with pytest.raises(PermissionError):
            guard.check("https://notexample.com/page")


class TestToolSandbox:
    @pytest.mark.asyncio
    async def test_run_returns_result_with_timeout_disabled(self):
        sandbox = ToolSandbox()
        assert await sandbox.run(asyncio.sleep(0, result="done")) == "done"

    @pytest.mark.asyncio
    async def test_run_enforces_timeout(self):
        sandbox = ToolSandbox()
        with pytest.raises(asyncio.TimeoutError):
            await sandbox.run(asyncio.sleep(0.1), timeout_s=0.001)

    def test_allowed_paths_and_network_are_exposed(self, tmp_path):
        sandbox = ToolSandbox(
            allowed_paths=[tmp_path],
            network_allow_list=["example.com"],
            limits=ResourceLimits(cpu_seconds=2, memory_mb=64),
        )
        assert sandbox.workspace is not None
        assert sandbox.resolve("file.txt") == tmp_path / "file.txt"
        assert sandbox.limits.cpu_seconds == 2
        sandbox.check_url("https://api.example.com/data")
        with pytest.raises(PermissionError):
            sandbox.resolve("../outside.txt")
        with pytest.raises(PermissionError):
            sandbox.check_url("https://evil.com")

    def test_unrestricted_sandbox_resolves_absolute_path(self, tmp_path):
        sandbox = ToolSandbox()
        assert sandbox.resolve(tmp_path / "file.txt") == tmp_path / "file.txt"

    def test_resource_limits_are_applied_and_restored(self):
        import resource

        with patch.object(resource, "getrlimit", return_value=(-1, -1)):
            with patch.object(resource, "setrlimit") as setrlimit:
                with ToolSandbox(
                    limits=ResourceLimits(cpu_seconds=2, memory_mb=64)
                ).resource_limits():
                    pass
        assert setrlimit.call_count == 4

    def test_resource_limit_setup_failures_are_best_effort(self):
        import resource

        with patch.object(resource, "getrlimit", return_value=(-1, -1)):
            with patch.object(resource, "setrlimit", side_effect=OSError("unsupported")):
                with ToolSandbox(limits=ResourceLimits(cpu_seconds=1)).resource_limits():
                    pass
