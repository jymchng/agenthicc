"""Unit tests for WindowsFilesystemBackend."""

from __future__ import annotations

import pytest

from agenthicc.tools.fs.windows import WindowsFilesystemBackend

pytestmark = pytest.mark.unit


def test_inherits_linux_write_read(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    b.write_text("hello.txt", "hello world")
    assert b.read_text("hello.txt") == "hello world"


def test_name_is_windows(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    assert b.name == "windows"


def test_reserved_name_rejected(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    with pytest.raises(PermissionError):
        b.write_text("CON.txt", "x")


def test_reserved_com_rejected(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    with pytest.raises(PermissionError):
        b.write_text("COM1.log", "x")


def test_stat_permissions_empty(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    b.write_text("f.txt", "data")
    s = b.stat("f.txt")
    assert s.permissions == ""


def test_stat_backend_is_windows(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    b.write_text("f.txt", "data")
    s = b.stat("f.txt")
    assert s.backend == "windows"


def test_symlink_raises(tmp_path):
    b = WindowsFilesystemBackend(tmp_path)
    with pytest.raises(NotImplementedError):
        b.symlink("target", "link")
