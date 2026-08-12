"""Process-aware, crash-recoverable file ownership primitives.

This module contains the low-level pieces shared by session ownership and
workflow-run claims.  It deliberately has no session or workflow knowledge:
callers provide the record payload and decide what a conflict means.

The liveness policy is conservative.  A local owner can be reclaimed only when
the process is absent, a zombie, or its recorded process-start identity no
longer matches.  Unknown hosts, permission failures, malformed records, and
unsupported process identity checks are treated as live/unknown.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO

if os.name != "nt":
    import fcntl

__all__ = [
    "InterProcessLock",
    "InterProcessLockError",
    "atomic_publish",
    "atomic_replace",
    "directory_fsync",
    "owner_alive",
    "process_identity",
    "read_json_object",
    "remove_if_owner",
]

ProcessIdentityResolver = Callable[[int], tuple[str, str] | None]


class InterProcessLockError(RuntimeError):
    """Raised when the platform cannot establish a required file lock."""


class InterProcessLock(AbstractContextManager[None]):
    """A blocking, cross-process advisory lock for short critical sections.

    The lock is intentionally explicit about unsupported or failed backends.
    Callers protecting a correctness invariant must not silently continue
    without exclusion.  The file remains on disk as a lock namespace; the
    operating-system lock, not its contents, provides the critical section.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self._backend: str | None = None

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise InterProcessLockError(f"cannot open lock file {self.path}: {exc}") from exc
        self._handle = handle
        try:
            # Windows byte-range locking requires a byte to exist.  Creating
            # it before acquiring is harmless; all correctness comes from the
            # subsequent OS-level lock.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                self._backend = "msvcrt"
            else:
                flock = getattr(fcntl, "flock", None)
                lock_ex = getattr(fcntl, "LOCK_EX", None)
                if not callable(flock) or not isinstance(lock_ex, int):
                    raise OSError("fcntl locking backend is unavailable")
                flock(handle.fileno(), lock_ex)
                self._backend = "fcntl"
        except (ImportError, OSError, AttributeError) as exc:
            handle.close()
            self._handle = None
            raise InterProcessLockError(f"cannot lock {self.path}: {exc}") from exc
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        handle = self._handle
        backend = self._backend
        self._handle = None
        self._backend = None
        if handle is None:
            return None
        try:
            if backend == "msvcrt":
                import msvcrt  # noqa: PLC0415

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif backend == "fcntl":
                flock = getattr(fcntl, "flock", None)
                lock_un = getattr(fcntl, "LOCK_UN", None)
                if not callable(flock) or not isinstance(lock_un, int):
                    raise OSError("fcntl unlocking backend is unavailable")
                flock(handle.fileno(), lock_un)
        finally:
            handle.close()
        return None


def process_identity(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start-token)`` for a local process when available."""

    if os.name == "nt":
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        end_comm = stat.rfind(")")
        if end_comm < 0:
            return None
        fields = stat[end_comm + 2 :].split()
        if len(fields) <= 19:
            return None
        state = fields[0]
        start_time = fields[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            boot_id = ""
        return state, f"{boot_id}:{start_time}" if boot_id else start_time
    except (OSError, UnicodeError, ValueError):
        return None


def owner_alive(
    payload: Mapping[str, object],
    *,
    identity_resolver: ProcessIdentityResolver = process_identity,
) -> bool:
    """Return whether *payload* must be treated as an active owner.

    A malformed or unverifiable payload returns ``True``.  This is deliberate:
    callers may report it as an unknown conflict, but must not reclaim it.
    """

    host = payload.get("host")
    pid = payload.get("pid")
    if (
        host != socket.gethostname()
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
    ):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # An error other than a definitive "no such process" is ambiguous;
        # never reclaim on an unclassified platform/permission result.
        return True
    try:
        identity = identity_resolver(pid)
    except Exception:  # noqa: BLE001
        # A resolver failure is an inability to prove that this is a stale
        # owner.  Treat it as live/unknown rather than making a destructive
        # PID-reuse decision on incomplete evidence.
        return True
    if identity is not None:
        state, current_start_token = identity
        if state == "Z":
            return False
        recorded_start_token = payload.get("process_start_token")
        if (
            isinstance(recorded_start_token, str)
            and recorded_start_token
            and recorded_start_token != current_start_token
        ):
            return False
    return True


def atomic_publish(path: Path, encoded: bytes, *, prefix: str) -> None:
    """Publish complete *encoded* bytes at *path* or raise ``FileExistsError``.

    A private temporary file is fsynced before a hard link makes the visible
    destination.  The destination therefore cannot be an empty or partially
    written record after a process is killed during publication.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.link(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_replace(path: Path, encoded: bytes, *, prefix: str) -> None:
    """Atomically replace *path* with complete, fsynced *encoded* bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
        directory_fsync(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def directory_fsync(path: Path) -> None:
    """Best-effort fsync of a directory after an atomic namespace change."""

    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Windows and some filesystems do not permit directory fsync.  The
        # record itself was already fsynced before publication.
        pass


def read_json_object(path: Path, *, max_bytes: int = 64 * 1024) -> dict[str, object] | None:
    """Read a bounded JSON object, returning ``None`` for missing and ``{}`` invalid."""

    try:
        if path.stat().st_size > max_bytes:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def remove_if_owner(path: Path, owner_id: str) -> bool:
    """Remove *path* only if its current JSON record has *owner_id*."""

    payload = read_json_object(path)
    if payload is None or payload.get("owner_id") != owner_id:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
