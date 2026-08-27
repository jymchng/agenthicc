"""Bounded discovery-cache and source-fingerprint tests (PRD-176)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agenthicc.runners.discovery_cache import (
    DISCOVERY_CACHE_VERSION,
    fingerprint_sources,
    read_discovery_cache,
    write_discovery_cache,
)

pytestmark = pytest.mark.unit


def test_fingerprint_changes_when_a_source_is_added_or_removed(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    first = root / "one.py"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    original = fingerprint_sources((root,))

    second = root / "two.py"
    second.write_text("VALUE = 2\n", encoding="utf-8")
    added = fingerprint_sources((root,))
    assert added["fingerprint"] != original["fingerprint"]
    second.unlink()
    removed = fingerprint_sources((root,))
    assert removed["fingerprint"] == original["fingerprint"]


def test_fingerprint_hash_catches_same_stat_content_change(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    source = root / "plugin.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = source.stat()
    first = fingerprint_sources((root,))

    source.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = fingerprint_sources((root,))

    assert changed["fingerprint"] != first["fingerprint"]


def test_discovery_cache_is_atomic_bounded_and_permission_restricted(tmp_path: Path) -> None:
    path = tmp_path / ".agenthicc" / "cache" / "extension-discovery.json"
    fingerprint = fingerprint_sources((tmp_path / "missing",))

    write_discovery_cache(path, fingerprint)

    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = read_discovery_cache(path)
    assert loaded is not None
    assert loaded["version"] == DISCOVERY_CACHE_VERSION
    assert loaded["fingerprint"] == fingerprint["fingerprint"]
    assert not list(path.parent.glob("*.tmp"))


def test_invalid_or_oversized_discovery_cache_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "discovery.json"
    path.write_text("not-json", encoding="utf-8")
    assert read_discovery_cache(path) is None

    path.write_bytes(b"x" * (256 * 1024 + 1))
    assert read_discovery_cache(path) is None
