"""Unit tests for WorkspaceView and NetworkGuard (PRD-04 sandbox primitives)."""

from __future__ import annotations

import os

import pytest

from agenthicc.tools.sandbox import NetworkGuard, WorkspaceView

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
