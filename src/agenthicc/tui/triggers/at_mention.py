"""AtMentionTrigger — file, directory and subagent mentions for ``@`` (TUX-05).

Matching behaviour
------------------
* Case-insensitive prefix, substring, and fuzzy matching via
  ``mentions.matcher.filter_and_rank()``.
* Path-segment matching: ``@read`` matches ``docs/README.md`` because
  ``README.md`` is a path segment.
* Directory navigation preserves ``/`` and ``\\`` separators and expands
  relative, absolute, and ``~``-prefixed directory prefixes.
* Ranking: exact → filename prefix → path-segment prefix → filename
  substring → path substring → fuzzy; within a tier, files mentioned
  earlier in this session and files changed in the working tree first.
* With an empty fragment, recently mentioned files come first, then changed
  files, then the directory listing.
* Hidden entries (names starting with ``'.'``) are always skipped; entries
  ``.gitignore`` ignores are skipped unless the fragment names them exactly.

Each row has a kind glyph (directory or file) and a muted detail: a file's
size, a directory's entry count. ``git status`` runs on a worker thread
(:class:`WorkingTreeStatus`), never on the event loop; the picker uses the
last answer and asks for a fresh one at most every two seconds.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.agents.definitions import AgentSpec

from agenthicc.mentions.matcher import filter_and_rank
from agenthicc.mentions.parser import _is_valid_mention_position
from agenthicc.tui.theme import glyphs
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerResult

__all__ = ["AtMentionTrigger", "WorkingTreeStatus", "human_size"]

#: How often the picker asks ``git status`` again, at most.
REFRESH_INTERVAL_S = 2.0
#: How long a directory scan is reused between keystrokes.
SCAN_TTL_S = 2.0
#: Files remembered as mentioned in this session.
RECENT_LIMIT = 20
#: Changed files added to an empty fragment's listing.
CHANGED_LIMIT = 50

GitRunner = Callable[[Path], tuple[str, str] | None]


def human_size(size: int) -> str:
    """``512 B``, ``1.2 KB``, ``3.4 MB``."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _run_git(cwd: Path) -> tuple[str, str] | None:
    """``(prefix, status -z output)`` for *cwd*'s repository, or ``None``."""
    try:
        prefix = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--ignored=traditional"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix, status


@dataclass(frozen=True)
class _TreeState:
    changed: tuple[str, ...] = ()
    ignored: frozenset[str] = frozenset()


def _parse_status(prefix: str, output: str) -> _TreeState:
    """Changed and ignored paths, relative to the directory *prefix* names."""
    changed: list[str] = []
    ignored: set[str] = set()
    fields = output.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the original path of a rename or copy
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        if not relative:
            continue
        if code == "!!":
            ignored.add(relative.rstrip("/"))
        else:
            changed.append(relative)
    return _TreeState(tuple(changed), frozenset(ignored))


class WorkingTreeStatus:
    """The working tree's changed and ignored files, from ``git status``.

    :meth:`refresh` returns at once and runs git on a daemon thread; readers
    see the previous answer until the new one is complete.
    """

    def __init__(
        self,
        cwd: Path,
        *,
        runner: GitRunner = _run_git,
        min_interval_s: float = REFRESH_INTERVAL_S,
    ) -> None:
        self._cwd = cwd
        self._runner = runner
        self._min_interval_s = min_interval_s
        self._state = _TreeState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started_at = float("-inf")

    @property
    def changed(self) -> tuple[str, ...]:
        """Changed and untracked paths, relative to the directory."""
        return self._state.changed

    @property
    def ignored(self) -> frozenset[str]:
        """Ignored paths (no trailing ``/``), relative to the directory."""
        return self._state.ignored

    def is_ignored(self, relative: str) -> bool:
        """Whether *relative* or one of its parent directories is ignored."""
        ignored = self._state.ignored
        if not ignored:
            return False
        parts = relative.rstrip("/").split("/")
        return any("/".join(parts[: depth + 1]) in ignored for depth in range(len(parts)))

    def refresh(self) -> threading.Thread | None:
        """Start a background ``git status`` unless one ran in the last interval."""
        with self._lock:
            now = time.monotonic()
            running = self._thread is not None and self._thread.is_alive()
            if running or now - self._started_at < self._min_interval_s:
                return None
            self._started_at = now
            thread = threading.Thread(
                target=self.refresh_now, name="at-picker-git-status", daemon=True
            )
            self._thread = thread
        thread.start()
        return thread

    def refresh_now(self) -> None:
        """Run ``git status`` in the calling thread (a worker thread)."""
        result = self._runner(self._cwd)
        self._state = _TreeState() if result is None else _parse_status(*result)


@dataclass(frozen=True)
class _Entry:
    relative: str  # path relative to the scanned directory, no trailing "/"
    is_dir: bool
    detail: str


def _entry_count(path: Path) -> str:
    try:
        with os.scandir(path) as entries:
            count = sum(1 for entry in entries if not entry.name.startswith("."))
    except OSError:
        return ""
    return f"{count} entr{'y' if count == 1 else 'ies'}"


def _describe(path: Path, is_dir: bool) -> str:
    if is_dir:
        return _entry_count(path)
    try:
        return human_size(path.stat().st_size)
    except OSError:
        return ""


def _list(path: Path) -> list[os.DirEntry[str]]:
    """Visible entries of *path*, directories first, by name."""
    try:
        with os.scandir(path) as scan:
            entries = [entry for entry in scan if not entry.name.startswith(".")]
    except OSError:
        return []

    def is_dir(entry: os.DirEntry[str]) -> bool:
        try:
            return entry.is_dir()
        except OSError:
            return False

    return sorted(entries, key=lambda entry: (not is_dir(entry), entry.name))


class AtMentionTrigger(TriggerHandlerBase):
    """File/directory and subagent trigger for the '@' character."""

    char = "@"
    label = "Mention File or Agent"

    def __init__(
        self,
        agents: Callable[[], Sequence[AgentSpec]] | None = None,
        *,
        git: GitRunner = _run_git,
    ) -> None:
        self._agents = agents
        self._git = git
        self._status: dict[Path, WorkingTreeStatus] = {}
        self._scans: dict[Path, tuple[float, list[_Entry]]] = {}
        self._recent: list[str] = []

    # ------------------------------------------------------------------
    # Working tree and recency
    # ------------------------------------------------------------------

    def working_tree(self, cwd: Path) -> WorkingTreeStatus:
        """The ``git status`` cache for *cwd* (created on first use)."""
        status = self._status.get(cwd)
        if status is None:
            status = WorkingTreeStatus(cwd, runner=self._git)
            self._status[cwd] = status
        return status

    def note_mention(self, value: str) -> None:
        """Remember *value* as mentioned in this session (most recent first)."""
        if value in self._recent:
            self._recent.remove(value)
        self._recent.insert(0, value)
        del self._recent[RECENT_LIMIT:]

    @property
    def recent(self) -> tuple[str, ...]:
        """Values mentioned in this session, most recent first."""
        return tuple(self._recent)

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------

    @staticmethod
    def _search_dir(cwd: Path, dir_part: str, separator: str) -> Path:
        """Resolve a typed directory prefix without losing path semantics."""
        # Include the separator when resolving.  This matters for a direct
        # child of the filesystem root (``/etc``): resolving only ``dir_part``
        # would otherwise turn the empty prefix into the session cwd.
        expanded = Path(f"{dir_part}{separator}").expanduser()
        return expanded if expanded.is_absolute() else cwd / expanded

    @staticmethod
    def _item(display: str, is_dir: bool, detail: str) -> MatchItem:
        g = glyphs()
        return MatchItem(
            value=f"{display}/" if is_dir else display,
            label=display,
            detail=detail,
            glyph=g.folder if is_dir else g.info,
            path=True,
        )

    def _scan(self, cwd: Path) -> list[_Entry]:
        """Top-level entries and their children, reused for a moment."""
        cached = self._scans.get(cwd)
        now = time.monotonic()
        if cached is not None and now - cached[0] < SCAN_TTL_S:
            return cached[1]
        entries: list[_Entry] = []
        for entry in _list(cwd):
            path = Path(entry.path)
            is_dir = entry.is_dir()
            entries.append(_Entry(entry.name, is_dir, _describe(path, is_dir)))
            # Always include immediate children so path-segment matching works:
            # e.g. @read → docs/README.md even when "docs" doesn't start with "read".
            if is_dir:
                for child in _list(path):
                    child_dir = child.is_dir()
                    entries.append(
                        _Entry(
                            f"{entry.name}/{child.name}",
                            child_dir,
                            _describe(Path(child.path), child_dir),
                        )
                    )
        self._scans[cwd] = (now, entries)
        return entries

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        """Return filesystem matches for *fragment* relative to *ctx.cwd*.

        When *fragment* contains "/" the search is scoped to the indicated
        subdirectory.  Without "/", both top-level entries **and** their
        immediate children are candidates, enabling path-segment matching
        (``@read`` matches ``docs/README.md``).
        """
        cwd = ctx.cwd

        # Typing ``@agent`` opens the visible subagent definitions; selecting
        # one inserts ``@agent:NAME``. Hidden definitions remain invocable by
        # exact name but are never offered in this picker (AGX-09 §5.3).
        if self._agents is not None and fragment.casefold().startswith("agent"):
            query = fragment[len("agent") :].removeprefix(":").casefold()
            return [
                MatchItem(
                    value=f"agent:{spec.name}",
                    label=f"@agent:{spec.name}",
                    detail=spec.description,
                )
                for spec in self._agents()
                if spec.subagent and not spec.hidden and spec.name.casefold().startswith(query)
            ]

        status = self.working_tree(cwd)
        status.refresh()

        # ── navigating into a subdirectory ──────────────────────────────────
        separator_index = max(fragment.rfind("/"), fragment.rfind("\\"))
        if separator_index >= 0:
            dir_part = fragment[:separator_index]
            separator = fragment[separator_index]
            file_prefix = fragment[separator_index + 1 :]
            search_dir = self._search_dir(cwd, dir_part, separator)
            if not search_dir.is_dir():
                return []
            # Subdirectory navigation uses strict case-insensitive PREFIX matching
            # only — NOT substring or fuzzy.  The user has already chosen the
            # directory; the suffix is the start of the filename they want.
            # Using filter_and_rank here would make "." match every file that has
            # an extension (substring hit), so "@docs/." would populate the picker
            # with all docs instead of returning no results.
            prefix_cf = file_prefix.casefold()
            results: list[MatchItem] = []
            for entry in _list(search_dir):
                if prefix_cf and not entry.name.casefold().startswith(prefix_cf):
                    continue
                display_path = f"{dir_part}{separator}{entry.name}"
                exact = entry.name.casefold() == prefix_cf
                if not exact and status.is_ignored(display_path.replace("\\", "/")):
                    continue
                is_dir = entry.is_dir()
                results.append(
                    self._item(display_path, is_dir, _describe(Path(entry.path), is_dir))
                )
            return results

        # ── top-level search: build candidate pool then rank ─────────────────
        if not cwd.is_dir():
            return []

        query = fragment.casefold()
        candidates: dict[str, MatchItem] = {}
        for scanned in self._scan(cwd):
            named = query in (
                scanned.relative.casefold(),
                scanned.relative.rsplit("/", 1)[-1].casefold(),
            )
            if status.is_ignored(scanned.relative) and not named:
                continue
            item = self._item(scanned.relative, scanned.is_dir, scanned.detail)
            candidates[item.value] = item
        changed = [path for path in status.changed if not status.is_ignored(path)]
        for relative in [*self._recent, *changed[:CHANGED_LIMIT]]:
            clean = relative.rstrip("/")
            if not clean or any(part.startswith(".") for part in clean.split("/")):
                continue
            path = cwd / clean
            is_dir = path.is_dir()
            if not (is_dir or path.is_file()):
                continue
            item = self._item(clean, is_dir, _describe(path, is_dir))
            candidates.setdefault(item.value, item)

        # Values carry a trailing "/" for directories, as git reports them.
        boosted = [*self._recent, *changed]
        if not fragment:
            order = {value: index for index, value in enumerate(dict.fromkeys(boosted))}
            first = sorted(
                (item for item in candidates.values() if item.value in order),
                key=lambda item: order[item.value],
            )
            rest = [item for item in candidates.values() if item.value not in order]
            return [*first, *rest]
        return filter_and_rank(fragment, list(candidates.values()), boosted=boosted)

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> TriggerResult:
        """Insert the selected path (with leading '@') into *buf*."""
        if item is None:
            return TriggerResult(buffer=buf + ["@"] + list(fragment))
        if not item.value.startswith("agent:"):
            self.note_mention(item.value)
        return TriggerResult(buffer=buf + list("@" + item.value))

    def can_activate(self, buf: list[str]) -> bool:
        # Keep picker activation aligned with submitted-message parsing:
        # valid prose boundary, and not inside an inline/fenced code span.
        prefix = "".join(buf)
        return _is_valid_mention_position(prefix + "@", len(prefix))

    # get_hint → inherited from TriggerHandlerBase (returns None)
