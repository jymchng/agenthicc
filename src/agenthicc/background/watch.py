"""Change notification for the background registry and its workers (AGX-04).

The agents screen used to poll: a page refresh four times a second and a
maintenance pass every few seconds, whether or not anything changed. A
:class:`FileWatcher` instead wakes its owner when something it watches
changes — a file, the entries of a directory, or a worker process exiting:

- **Linux:** inotify on each watched file's directory (so appends, atomic
  replacements, creation and deletion are all seen) and ``pidfd_open`` for
  process exits.
- **macOS and BSD:** kqueue ``EVFILT_VNODE`` on each file or directory,
  reopened after a rename or delete, and ``EVFILT_PROC`` for process exits.
- **Elsewhere, or with ``backend="stat"``:** a ``stat`` of each file every
  ``fallback_interval_s`` — one system call per file, no reads — and no
  process notification; :meth:`FileWatcher.watch_process` returns ``False``
  so the caller knows to probe instead.

Native notification can miss changes on network filesystems, so a slower
``stat`` sweep (``safety_interval_s``) runs alongside it as a safety net.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import functools
import os
import select
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

__all__ = ["FileWatcher", "WatchBackend", "WatchChanges"]

WatchBackend = Literal["inotify", "kqueue", "stat"]

#: Changed watch keys. A directory watch maps to the names of the entries that
#: changed, or ``None`` when the backend cannot tell which; file and process
#: watches map to an empty set.
WatchChanges = dict[str, frozenset[str] | None]

_Fingerprint = tuple[int, int, int]

# inotify(7)
_IN_MODIFY = 0x00000002
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_Q_OVERFLOW = 0x00004000
_IN_NONBLOCK = 0o4000
_IN_CLOEXEC = 0o2000000
# A write raises IN_MODIFY; IN_CLOSE_WRITE and IN_ATTRIB would only repeat it.
_INOTIFY_MASK = _IN_MODIFY | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
_EVENT_HEADER = struct.Struct("iIII")

# pidfd_open(2) is 434 in the generic syscall table, which these architectures
# share. Some Python builds (for example python-build-standalone) lack
# os.pidfd_open, so the system call is made directly where the number is known.
_SYS_PIDFD_OPEN = 434
_PIDFD_MACHINES = frozenset(
    {"x86_64", "amd64", "aarch64", "arm64", "i386", "i686", "armv7l", "armv6l"}
    | {"ppc64le", "ppc64", "s390x", "riscv64"}
)


@functools.cache
def _libc() -> ctypes.CDLL:
    # The process's own symbols include libc's; ctypes.util.find_library would
    # spawn ldconfig, which stalls the screen as it opens.
    return ctypes.CDLL(None, use_errno=True)


def _pidfd_open(pid: int) -> int:
    """Return a pidfd for *pid*; ``ProcessLookupError`` if it no longer exists."""
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "pidfd_open is unavailable")
    try:
        native = os.pidfd_open
    except AttributeError:
        pass  # this Python build lacks it; make the system call directly
    else:
        return native(pid)
    if os.uname().machine not in _PIDFD_MACHINES:
        raise OSError(errno.ENOSYS, "pidfd_open is unavailable")
    syscall = _libc().syscall
    syscall.restype = ctypes.c_long
    result = int(syscall(ctypes.c_long(_SYS_PIDFD_OPEN), ctypes.c_long(pid), ctypes.c_long(0)))
    if result < 0:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            raise ProcessLookupError(code, os.strerror(code))
        raise OSError(code, os.strerror(code))
    return result


def _fingerprint(path: Path) -> _Fingerprint:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, 0)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass
class _Watch:
    path: Path
    fingerprint: _Fingerprint
    directory: bool


class _Inotify:
    """A minimal inotify binding: directory watches, drained on readiness."""

    def __init__(self) -> None:
        libc = _libc()
        self._add = libc.inotify_add_watch
        self._add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        self._add.restype = ctypes.c_int
        self._remove = libc.inotify_rm_watch
        self._remove.argtypes = (ctypes.c_int, ctypes.c_int)
        self._remove.restype = ctypes.c_int
        init = libc.inotify_init1
        init.argtypes = (ctypes.c_int,)
        init.restype = ctypes.c_int
        fd = init(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd
        self._directories: dict[int, Path] = {}

    def add_directory(self, directory: Path) -> None:
        if directory in self._directories.values():
            return
        descriptor = self._add(self.fd, os.fsencode(directory), _INOTIFY_MASK)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {directory}")
        self._directories[descriptor] = directory

    def remove_directory(self, directory: Path) -> None:
        for descriptor, watched in tuple(self._directories.items()):
            if watched == directory:
                del self._directories[descriptor]
                self._remove(self.fd, descriptor)

    def read(self) -> tuple[list[tuple[Path, str]], bool]:
        """Return ``(directory, entry name)`` events and whether the queue overflowed."""
        changed: list[tuple[Path, str]] = []
        overflow = False
        while True:
            try:
                data = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                break
            if not data:
                break
            offset = 0
            while offset + _EVENT_HEADER.size <= len(data):
                descriptor, mask, _cookie, length = _EVENT_HEADER.unpack_from(data, offset)
                offset += _EVENT_HEADER.size
                raw_name = data[offset : offset + length].split(b"\0", 1)[0]
                offset += length
                if mask & _IN_Q_OVERFLOW:
                    overflow = True
                    continue
                directory = self._directories.get(descriptor)
                if directory is not None and raw_name:
                    changed.append((directory, os.fsdecode(raw_name)))
        return changed, overflow

    def close(self) -> None:
        os.close(self.fd)


class _VnodeQueue(Protocol):
    """The kqueue operations the watcher uses (kqueue exists only on macOS and BSD)."""

    @property
    def fd(self) -> int: ...

    def arm(self, key: str, path: Path) -> bool: ...

    def disarm(self, key: str) -> None: ...

    def watch_process(self, key: str, pid: int) -> None: ...

    def unwatch_process(self, key: str) -> None: ...

    def read(self) -> tuple[set[str], set[str]]: ...

    def close(self) -> None: ...


if sys.platform != "linux" and sys.platform != "win32":

    class _Kqueue:
        """kqueue vnode watches, reopened when a file is replaced, and process exits."""

        def __init__(self) -> None:
            self.queue = select.kqueue()
            self._descriptors: dict[str, int] = {}
            self._processes: dict[int, str] = {}

        @property
        def fd(self) -> int:
            return self.queue.fileno()

        def arm(self, key: str, path: Path) -> bool:
            self.disarm(key)
            flags = os.O_EVTONLY if sys.platform == "darwin" else os.O_RDONLY
            try:
                descriptor = os.open(path, flags)
            except OSError:
                return False  # not created yet; the sweep arms it later
            event = select.kevent(
                descriptor,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=(
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_ATTRIB
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_RENAME
                ),
            )
            self.queue.control([event], 0, 0)
            self._descriptors[key] = descriptor
            return True

        def disarm(self, key: str) -> None:
            descriptor = self._descriptors.pop(key, None)
            if descriptor is not None:
                os.close(descriptor)

        def watch_process(self, key: str, pid: int) -> None:
            """Watch *pid* for exit; raises ``ProcessLookupError`` if it already exited."""
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            self.queue.control([event], 0, 0)
            self._processes[pid] = key

        def unwatch_process(self, key: str) -> None:
            for pid, watched in tuple(self._processes.items()):
                if watched == key:
                    del self._processes[pid]
                    event = select.kevent(
                        pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_DELETE
                    )
                    try:
                        self.queue.control([event], 0, 0)
                    except OSError:
                        pass  # it already fired or exited

        def read(self) -> tuple[set[str], set[str]]:
            """Return the file keys and the process keys whose events fired."""
            events = self.queue.control(None, 64, 0)
            files: set[str] = set()
            processes: set[str] = set()
            for event in events:
                if event.filter == select.KQ_FILTER_PROC:
                    key = self._processes.pop(int(event.ident), None)
                    if key is not None:
                        processes.add(key)
                    continue
                for key, descriptor in self._descriptors.items():
                    if descriptor == event.ident:
                        files.add(key)
            return files, processes

        def close(self) -> None:
            for key in tuple(self._descriptors):
                self.disarm(key)
            self.queue.close()


class FileWatcher:
    """Wake an asyncio consumer when watched files, directories or processes change.

    Watches are named, so a consumer can repoint one (for example the
    selected session's journal) without disturbing the others. ``wait``
    returns what changed since the previous call.
    """

    def __init__(
        self,
        *,
        fallback_interval_s: float = 0.25,
        safety_interval_s: float = 2.0,
        backend: WatchBackend | None = None,
    ) -> None:
        self._fallback_interval_s = fallback_interval_s
        self._safety_interval_s = safety_interval_s
        self._watches: dict[str, _Watch] = {}
        self._changed: WatchChanges = {}
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inotify: _Inotify | None = None
        self._kqueue: _VnodeQueue | None = None
        self._pidfds: dict[str, int] = {}
        self._kqueue_processes: set[str] = set()
        # Watches whose path did not exist yet; the sweep arms them later.
        self._unarmed: set[str] = set()
        self.backend: WatchBackend = self._open(backend)

    def _open(self, requested: WatchBackend | None) -> WatchBackend:
        if requested == "stat":
            return "stat"
        if requested in (None, "inotify") and sys.platform.startswith("linux"):
            try:
                self._inotify = _Inotify()
            except (OSError, AttributeError):
                self._inotify = None
            else:
                return "inotify"
        if requested in (None, "kqueue") and sys.platform != "linux" and sys.platform != "win32":
            try:
                self._kqueue = _Kqueue()
            except OSError:
                self._kqueue = None
            else:
                return "kqueue"
        return "stat"

    @property
    def reports_entry_names(self) -> bool:
        """Whether directory watches report which entries changed."""
        return self._inotify is not None

    def _attach(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        if self._inotify is not None:
            self._loop.add_reader(self._inotify.fd, self._on_inotify)
        elif self._kqueue is not None:
            self._loop.add_reader(self._kqueue.fd, self._on_kqueue)

    @staticmethod
    def _directory_of(watch: _Watch) -> Path:
        return watch.path if watch.directory else watch.path.parent

    def _add(self, key: str, path: Path, *, directory: bool) -> None:
        current = self._watches.get(key)
        if current is not None and current.path == path and current.directory == directory:
            return
        self._watches[key] = _Watch(path, _fingerprint(path), directory)
        if current is not None:
            self._release(current)
        self._arm(key)

    def _arm(self, key: str) -> None:
        watch = self._watches[key]
        armed = True
        if self._inotify is not None:
            try:
                self._inotify.add_directory(self._directory_of(watch))
            except OSError:
                armed = False  # the directory does not exist yet
        elif self._kqueue is not None:
            armed = self._kqueue.arm(key, watch.path)
        if armed:
            self._unarmed.discard(key)
        else:
            self._unarmed.add(key)

    def _release(self, watch: _Watch) -> None:
        """Stop watching *watch*'s directory once no other watch needs it."""
        if self._inotify is None:
            return
        directory = self._directory_of(watch)
        if all(self._directory_of(other) != directory for other in self._watches.values()):
            self._inotify.remove_directory(directory)

    def watch(self, key: str, path: Path) -> None:
        """Watch the file *path* under *key*, replacing what *key* watched before."""
        self._add(key, path, directory=False)

    def watch_directory(self, key: str, path: Path) -> None:
        """Watch the entries of the directory *path* under *key*."""
        self._add(key, path, directory=True)

    def watch_process(self, key: str, pid: int) -> bool:
        """Report *key* when process *pid* exits.

        Returns ``False`` when this platform or backend cannot notify process
        exits; the caller must then probe the process itself. A process that
        has already exited is reported at once.
        """
        self.unwatch(key)
        if self.backend == "stat":
            return False
        self._attach()
        assert self._loop is not None
        if sys.platform.startswith("linux"):
            try:
                descriptor = _pidfd_open(pid)
            except ProcessLookupError:
                self._mark(key, frozenset())
                return True
            except OSError:
                return False
            self._pidfds[key] = descriptor
            self._loop.add_reader(descriptor, self._on_pidfd, key)
            return True
        if self._kqueue is not None:
            try:
                self._kqueue.watch_process(key, pid)
            except ProcessLookupError:
                self._mark(key, frozenset())
                return True
            except OSError:
                return False
            self._kqueue_processes.add(key)
            return True
        return False

    def unwatch(self, key: str) -> None:
        watch = self._watches.pop(key, None)
        self._unarmed.discard(key)
        if watch is not None:
            self._release(watch)
        if self._kqueue is not None:
            self._kqueue.disarm(key)
            if key in self._kqueue_processes:
                self._kqueue_processes.discard(key)
                self._kqueue.unwatch_process(key)
        descriptor = self._pidfds.pop(key, None)
        if descriptor is not None:
            self._close_pidfd(descriptor)

    def _close_pidfd(self, descriptor: int) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.remove_reader(descriptor)
        os.close(descriptor)

    def _mark(self, key: str, names: frozenset[str] | None) -> None:
        if key in self._changed:
            previous = self._changed[key]
            self._changed[key] = None if previous is None or names is None else previous | names
        else:
            self._changed[key] = names
        self._event.set()

    def _on_inotify(self) -> None:
        assert self._inotify is not None
        events, overflow = self._inotify.read()
        if overflow:
            for key, watch in self._watches.items():
                self._mark(key, None if watch.directory else frozenset())
            return
        for key, watch in self._watches.items():
            if watch.directory:
                names = frozenset(name for directory, name in events if directory == watch.path)
                if names:
                    # Keep the safety sweep from reporting the same change
                    # again, without the names.
                    watch.fingerprint = _fingerprint(watch.path)
                    self._mark(key, names)
            elif any(
                directory == watch.path.parent and name == watch.path.name
                for directory, name in events
            ):
                watch.fingerprint = _fingerprint(watch.path)
                self._mark(key, frozenset())

    def _on_kqueue(self) -> None:
        assert self._kqueue is not None
        files, processes = self._kqueue.read()
        for key in files:
            watch = self._watches.get(key)
            if watch is not None:
                watch.fingerprint = _fingerprint(watch.path)
                # A delete or rename leaves the old vnode; follow the path.
                self._arm(key)
                self._mark(key, None if watch.directory else frozenset())
        for key in processes:
            self._kqueue_processes.discard(key)
            self._mark(key, frozenset())

    def _on_pidfd(self, key: str) -> None:
        descriptor = self._pidfds.pop(key, None)
        if descriptor is not None:
            self._close_pidfd(descriptor)
        self._mark(key, frozenset())

    def _sweep(self) -> None:
        """Stat every watched path and report the ones that changed."""
        for key in tuple(self._unarmed):
            self._arm(key)
        for key, watch in self._watches.items():
            current = _fingerprint(watch.path)
            if current != watch.fingerprint:
                watch.fingerprint = current
                if self._kqueue is not None:
                    self._arm(key)
                self._mark(key, None if watch.directory else frozenset())

    async def wait(self, timeout: float | None) -> WatchChanges:
        """Return what changed, waiting up to *timeout* seconds (forever if ``None``)."""
        self._attach()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        interval = self._fallback_interval_s if self.backend == "stat" else self._safety_interval_s
        while not self._changed:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                break
            step = interval if remaining is None else min(interval, remaining)
            try:
                await asyncio.wait_for(self._event.wait(), timeout=step)
            except TimeoutError:
                self._sweep()
        changed, self._changed = self._changed, {}
        self._event.clear()
        return changed

    def close(self) -> None:
        for key in tuple(self._pidfds):
            self._close_pidfd(self._pidfds.pop(key))
        loop = self._loop
        if self._inotify is not None:
            if loop is not None and not loop.is_closed():
                loop.remove_reader(self._inotify.fd)
            self._inotify.close()
            self._inotify = None
        if self._kqueue is not None:
            if loop is not None and not loop.is_closed():
                loop.remove_reader(self._kqueue.fd)
            self._kqueue.close()
            self._kqueue = None
