"""Bounded fingerprints for deferred extension discovery (PRD-176).

The cache deliberately stores no imported objects, source contents, prompts, or
credentials.  It is an acceleration/diagnostic record: an in-process result
cache can reuse validated objects, while the durable record tells a later
process which source fingerprint was last inspected.  The source files remain
authoritative and are always fingerprinted before a cached result is reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from agenthicc.runners.process_lease import directory_fsync

__all__ = [
    "DISCOVERY_CACHE_VERSION",
    "DiscoveryFingerprint",
    "fingerprint_sources",
    "read_discovery_cache",
    "write_discovery_cache",
]

DISCOVERY_CACHE_VERSION: Final[int] = 1
_MAX_CACHE_BYTES: Final[int] = 256 * 1024
_MAX_HASH_BYTES: Final[int] = 2 * 1024 * 1024
_HASH_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml"}
)


class DiscoveryFingerprint(dict[str, object]):
    """Typed-at-runtime JSON record for a set of extension source paths."""


def _source_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            continue
        try:
            candidates = resolved.rglob("*")
        except OSError:
            continue
        for path in candidates:
            if not path.is_file() or any(part in {"__pycache__", ".git"} for part in path.parts):
                continue
            try:
                canonical = path.resolve()
            except OSError:
                continue
            if canonical not in seen:
                seen.add(canonical)
                files.append(canonical)
    return sorted(files, key=lambda path: str(path))


def fingerprint_sources(roots: Iterable[Path]) -> DiscoveryFingerprint:
    """Return a deterministic, bounded source fingerprint.

    Every source contributes its path, size, and nanosecond mtime.  Small source
    files also contribute a content digest, catching an editor or restore
    operation that preserves timestamps.  File contents are never persisted in
    the cache itself.
    """
    digest = hashlib.sha256()
    count = 0
    total_bytes = 0
    for path in _source_files(roots):
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = str(path)
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}".encode("ascii"))
        if path.suffix.lower() in _HASH_SUFFIXES and stat.st_size <= _MAX_HASH_BYTES:
            try:
                content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                content_digest = "unreadable"
            digest.update(b"\0")
            digest.update(content_digest.encode("ascii"))
        count += 1
        total_bytes += stat.st_size
    return DiscoveryFingerprint(
        {
            "version": DISCOVERY_CACHE_VERSION,
            "fingerprint": digest.hexdigest(),
            "file_count": count,
            "source_bytes": total_bytes,
        }
    )


def read_discovery_cache(path: Path) -> DiscoveryFingerprint | None:
    """Read one bounded cache record, returning ``None`` if unusable."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    fingerprint = value.get("fingerprint")
    version = value.get("version")
    if version != DISCOVERY_CACHE_VERSION or not isinstance(fingerprint, str):
        return None
    return DiscoveryFingerprint(
        {
            "version": DISCOVERY_CACHE_VERSION,
            "fingerprint": fingerprint[:128],
            "file_count": value.get("file_count", 0),
            "source_bytes": value.get("source_bytes", 0),
            "checked_at": value.get("checked_at", 0.0),
        }
    )


def write_discovery_cache(path: Path, fingerprint: DiscoveryFingerprint) -> None:
    """Atomically write a small permission-restricted cache record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(fingerprint)
    payload["version"] = DISCOVERY_CACHE_VERSION
    payload["checked_at"] = time.time()
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_CACHE_BYTES:
        raise OSError("extension discovery cache exceeds size limit")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        directory_fsync(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
