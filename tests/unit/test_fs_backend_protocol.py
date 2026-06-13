"""Unit tests for the FilesystemBackend protocol, data structures, and BackendRouter."""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from agenthicc.tools.fs.backend import FileEntry, FileStat, FilesystemBackend, GrepMatch
from agenthicc.tools.fs.linux import LinuxFilesystemBackend
from agenthicc.tools.fs.router import BackendRouter, _detect_default_backend

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# FileStat
# ---------------------------------------------------------------------------


def test_filestat_is_frozen():
    fs = FileStat(
        path="x",
        size=10,
        is_dir=False,
        is_file=True,
        modified_at=0.0,
        created_at=0.0,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        fs.size = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FileEntry
# ---------------------------------------------------------------------------


def test_fileentry_defaults():
    entry = FileEntry("f.txt", "rel/f.txt", False)
    assert entry.size == -1


# ---------------------------------------------------------------------------
# GrepMatch
# ---------------------------------------------------------------------------


def test_grepmatch_fields():
    gm = GrepMatch(path="a.py", line_number=5, line="def foo():", match_start=0, match_end=3)
    assert gm.path == "a.py"
    assert gm.line_number == 5
    assert gm.line == "def foo():"
    assert gm.match_start == 0
    assert gm.match_end == 3


# ---------------------------------------------------------------------------
# Protocol runtime check
# ---------------------------------------------------------------------------


def test_linux_isinstance_check():
    assert isinstance(LinuxFilesystemBackend(), FilesystemBackend)


# ---------------------------------------------------------------------------
# BackendRouter
# ---------------------------------------------------------------------------


def test_backend_router_default_is_linux():
    router = BackendRouter()
    assert router.default.name == "linux"


def test_backend_router_prefix_match():
    mock_s3 = MagicMock()
    mock_s3.name = "s3"
    router = BackendRouter()
    router.register("s3://", mock_s3)
    resolved = router.resolve("s3://b/k")
    assert resolved is mock_s3


def test_backend_router_fallback():
    mock_s3 = MagicMock()
    mock_s3.name = "s3"
    router = BackendRouter()
    router.register("s3://", mock_s3)
    resolved = router.resolve("/local/path")
    assert resolved is router.default


# ---------------------------------------------------------------------------
# _detect_default_backend
# ---------------------------------------------------------------------------


def test_detect_default_linux():
    backend = _detect_default_backend(".")
    assert backend.name in ("linux", "windows", "pyodide")
