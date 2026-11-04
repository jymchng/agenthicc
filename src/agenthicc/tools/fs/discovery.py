"""Bounded, authorized filesystem discovery for search and listing tools.

TUIR-01 and TUIR-02. ``search_files``, ``grep_files`` and ``list_directory``
used to ``rglob`` everything, authorize every candidate on the event loop
(three canonicalisations each), and only then filter. One search of a
33,000-file project held the event loop for 14 s. Discovery now works like
this:

- **One walk, off the loop.** A single ``os.scandir`` walk runs in a worker
  thread, under a session-wide limiter (:data:`MAX_CONCURRENT_WALKS`).
- **Prune before descending.** Ignored directories are skipped before the walk
  enters them: ``.gitignore`` rules in a git repository, and a default set
  (``node_modules``, ``.venv``, ``__pycache__``…) elsewhere.
- **Filter before authorizing.** An entry is tested for type and glob match
  first. Only matches are kept, and the walk stops at the result cap, the
  examined-entry cap or the time cap, saying which.
- **Authorization by construction.** An entry reached from an in-scope root
  without passing through a symlink is in scope, with no syscall. A symlink is
  resolved once and classified by prefix. A symlink pointing outside the scope
  goes to the full mode-aware policy, so Safe still asks, Plan still denies
  and Yolo still bypasses.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

__all__ = [
    "DEFAULT_IGNORED_NAMES",
    "MAX_CONCURRENT_WALKS",
    "MAX_EXAMINED_ENTRIES",
    "MAX_WALK_SECONDS",
    "DiscoveredEntry",
    "DiscoveryResult",
    "configure_walk_limit",
    "discover",
]

#: Directory names skipped outside a git repository (and always ``.git``).
DEFAULT_IGNORED_NAMES: Final = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
#: A walk stops after examining this many entries (``examined_cap``).
MAX_EXAMINED_ENTRIES: Final = 50_000
#: A walk stops after this many seconds (``time_cap``).
MAX_WALK_SECONDS: Final = 10.0
#: Heavy filesystem walks that may run at once across the session, by default.
MAX_CONCURRENT_WALKS: Final = 2
#: The walking thread yields the GIL this often (entries).
_YIELD_EVERY: Final = 1_000

TruncationReason = Literal["", "result_cap", "examined_cap", "time_cap"]

_limiters: dict[tuple[int, int], asyncio.Semaphore] = {}
_limit_lock = threading.Lock()


def configure_walk_limit(limit: int) -> None:
    """Set how many walks may run at once (``[tools] max_fs_walks``)."""
    from agenthicc.tools import fs

    fs.configure_walk_limit(limit)


def _limiter() -> asyncio.Semaphore:
    from agenthicc.tools import fs

    limit = fs.walk_limit()
    key = (id(asyncio.get_running_loop()), limit)
    with _limit_lock:
        semaphore = _limiters.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            _limiters[key] = semaphore
        return semaphore


@dataclass(frozen=True, slots=True)
class DiscoveredEntry:
    """One kept path: as the caller spells it, and its canonical target."""

    relative: str
    original: Path
    canonical: Path
    is_dir: bool


@dataclass(slots=True)
class DiscoveryResult:
    entries: list[DiscoveredEntry] = field(default_factory=list)
    examined: int = 0
    symlinked: int = 0
    outside_scope: int = 0
    denied: int = 0
    truncated: TruncationReason = ""

    def diagnostics(self) -> dict[str, object]:
        report: dict[str, object] = {
            "examined": self.examined,
            "kept": len(self.entries),
            "symlinked": self.symlinked,
            "outside_scope": self.outside_scope,
            "denied": self.denied,
        }
        if self.truncated:
            report["truncated_by"] = self.truncated
        return report


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


# ── ignore rules ──────────────────────────────────────────────────────────────

_IgnoreKey = tuple[str, int]
_ignore_cache: dict[_IgnoreKey, frozenset[str] | None] = {}
_ignore_lock = threading.Lock()


def _git_ignored(root: Path) -> frozenset[str] | None:
    """Paths under *root* that git ignores (directories end with ``/``), or ``None``.

    Cached per root and index modification time, so a pool of workers runs
    ``git`` once per repository per index change.
    """
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0:
        return None
    index = Path(top.stdout.strip()) / "index"
    try:
        stamp = index.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(root), stamp)
    with _ignore_lock:
        if key in _ignore_cache:
            return _ignore_cache[key]
    try:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    result = (
        frozenset(item for item in listed.stdout.decode(errors="replace").split("\0") if item)
        if listed.returncode == 0
        else None
    )
    with _ignore_lock:
        _ignore_cache[key] = result
    return result


@dataclass(frozen=True, slots=True)
class _IgnoreRules:
    git_ignored: frozenset[str] | None

    def skips(self, relative: str, name: str, is_dir: bool) -> bool:
        if name == ".git":
            return True
        if self.git_ignored is None:
            return is_dir and name in DEFAULT_IGNORED_NAMES
        key = relative + "/" if is_dir else relative
        return key in self.git_ignored


# ── the walk ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Walk:
    root: Path
    roots: tuple[str, ...]
    root_in_scope: bool
    pattern: str
    recursive: bool
    include_hidden: bool
    files_only: bool
    limit: int
    rules: _IgnoreRules | None
    deadline: float
    result: DiscoveryResult = field(default_factory=DiscoveryResult)
    deferred: list[tuple[str, Path]] = field(default_factory=list)

    def matches(self, relative: str) -> bool:
        pattern = self.pattern
        if pattern in {"", "*", "**", "**/*"}:
            return self.recursive or "/" not in relative
        anchored = pattern.removeprefix("**/")
        path = PurePosixPath(relative)
        if not path.match(anchored):
            return False
        if self.recursive or pattern.startswith("**/"):
            return True
        # A non-recursive glob is anchored at the root, like Path.glob.
        return len(path.parts) == len(PurePosixPath(anchored).parts)

    def run(self) -> None:
        stack: list[tuple[str, str]] = [("", str(self.root))]
        while stack:
            prefix, directory = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                if self._stop():
                    return
                self.result.examined += 1
                if self.result.examined % _YIELD_EVERY == 0:
                    time.sleep(0)  # let the event loop's thread take the GIL
                relative = f"{prefix}{entry.name}"
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if self.rules is not None and self.rules.skips(relative, entry.name, is_dir):
                    continue
                hidden = entry.name.startswith(".")
                if (
                    is_dir
                    and not is_symlink
                    and self.recursive
                    and (self.include_hidden or not hidden)
                ):
                    stack.append((relative + "/", entry.path))
                if not self.include_hidden and hidden:
                    continue
                if self.files_only and is_dir:
                    continue
                if not self.matches(relative):
                    continue
                self._keep(relative, Path(entry.path), is_symlink, is_dir)
            # Depth-first, in name order: deterministic results.
            stack.sort(key=lambda item: item[0], reverse=True)

    def _stop(self) -> bool:
        if len(self.result.entries) >= self.limit:
            self.result.truncated = "result_cap"
        elif self.result.examined >= MAX_EXAMINED_ENTRIES:
            self.result.truncated = "examined_cap"
        elif time.monotonic() > self.deadline:
            self.result.truncated = "time_cap"
        return bool(self.result.truncated)

    def _keep(self, relative: str, original: Path, is_symlink: bool, is_dir: bool) -> None:
        if not is_symlink and self.root_in_scope:
            # Reached without a symlink from an in-scope root: in scope.
            self.result.entries.append(DiscoveredEntry(relative, original, original, is_dir))
            return
        if is_symlink:
            self.result.symlinked += 1
        real = os.path.realpath(original)
        if _within(real, self.roots):
            self.result.entries.append(DiscoveredEntry(relative, original, Path(real), is_dir))
            return
        self.result.outside_scope += 1
        self.deferred.append((relative, original))


async def discover(
    context: Mapping[str, object],
    root: Path,
    *,
    pattern: str = "*",
    recursive: bool = True,
    include_hidden: bool = True,
    files_only: bool = False,
    include_ignored: bool = False,
    limit: int = 1_000,
    operation: str,
    tool_name: str,
) -> DiscoveryResult:
    """Walk *root* (already authorized and canonical) and keep authorized matches."""
    from agenthicc.tools.fs import _policy

    policy = _policy(context)
    roots = (
        tuple(str(item) for item in policy.scope.allowed_roots)
        if policy is not None
        else (str(root),)
    )
    root_text = str(root)
    walk = _Walk(
        root=root,
        roots=roots,
        root_in_scope=_within(root_text, roots),
        pattern=pattern,
        recursive=recursive,
        include_hidden=include_hidden,
        files_only=files_only,
        limit=max(1, limit),
        rules=None,
        deadline=0.0,
    )

    def _run() -> None:
        walk.rules = None if include_ignored else _IgnoreRules(_git_ignored(root))
        walk.deadline = time.monotonic() + MAX_WALK_SECONDS
        walk.run()

    async with _limiter():
        await asyncio.to_thread(_run)
    result = walk.result
    if policy is None:
        # No session policy: nothing to decide beyond the walk itself.
        result.entries.extend(
            DiscoveredEntry(relative, original, original, original.is_dir())
            for relative, original in walk.deferred
        )
        return result
    for relative, original in walk.deferred:
        try:
            canonical = await policy.authorize(
                str(original),
                operation=operation,
                tool_name=tool_name,
                tool_input={"path": str(original)},
            )
        except (OSError, PermissionError, ValueError):
            result.denied += 1
            continue
        result.entries.append(DiscoveredEntry(relative, original, canonical, canonical.is_dir()))
    result.entries.sort(key=lambda entry: entry.relative)
    return result
