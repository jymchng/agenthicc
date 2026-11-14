"""Release notes from the packaged ``CHANGELOG.md`` (TUX-03).

The changelog that ships with the package is the only source: nothing is
fetched, cached or rewritten. It is a Keep-a-Changelog file whose sections read
``## [Unreleased]`` and ``## [0.1.0] — 2025-01-01``, newest first.

:func:`select` answers what ``/release-notes`` lists: every section newer than
the last version whose notes were viewed, up to the installed version, plus the
installed version's own and any ``Unreleased`` section (work in the build that
is not in a release yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ReleaseEntry",
    "changelog_path",
    "load_entries",
    "parse_changelog",
    "select",
    "version_key",
]

UNRELEASED = "Unreleased"

#: ``## [0.1.0] — 2025-01-01``, ``## [Unreleased]``, ``## 0.2.0 (2025-02-03)``.
_HEADING = re.compile(
    r"^##\s+\[?(?P<version>[^\]\s]+)\]?(?:\s*(?:[—–-]|\()\s*(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"
)
_NUMBER = re.compile(r"\d+")
#: A changelog is prose; a file past this is not the one we ship.
_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    """One ``##`` section of the changelog."""

    version: str
    date: str
    body: str

    @property
    def unreleased(self) -> bool:
        return self.version.casefold() == UNRELEASED.casefold()

    @property
    def title(self) -> str:
        return self.version if self.unreleased else f"{self.version}  {self.date}".rstrip()


def version_key(version: str) -> tuple[int, ...]:
    """``"0.12.3"`` → ``(0, 12, 3)``; ``"0+unknown"`` → ``(0,)``; no digits → ``()``."""
    return tuple(int(part) for part in _NUMBER.findall(version.partition("+")[0]))


def parse_changelog(text: str) -> tuple[ReleaseEntry, ...]:
    """The changelog's sections, in file order (newest first)."""
    entries: list[ReleaseEntry] = []
    version = ""
    date = ""
    body: list[str] = []

    def close() -> None:
        if version:
            entries.append(ReleaseEntry(version, date, "\n".join(body).strip("\n")))

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            close()
            version = heading.group("version")
            date = heading.group("date") or ""
            body = []
        elif version and line.strip() != "---":
            body.append(line)
    close()
    return tuple(entry for entry in entries if entry.body.strip())


def changelog_path() -> Path | None:
    """The changelog beside the package's docs, or ``None`` when none ships.

    A source checkout has it at the repository root; an installed distribution
    has it in ``share/agenthicc/docs`` with the other documents.
    """
    from agenthicc.tools.introspect import docs_root

    root = docs_root()
    candidates = (
        [root / "CHANGELOG.md", root.parent / "CHANGELOG.md"]
        if root is not None
        else [Path(__file__).resolve().parents[2] / "CHANGELOG.md"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_entries(path: Path | None = None) -> tuple[ReleaseEntry, ...]:
    """The packaged changelog's sections; empty when it is missing or unreadable."""
    target = path or changelog_path()
    if target is None:
        return ()
    try:
        if target.stat().st_size > _MAX_BYTES:
            return ()
        return parse_changelog(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return ()


def select(
    entries: tuple[ReleaseEntry, ...], *, installed: str, last_seen: str = ""
) -> tuple[ReleaseEntry, ...]:
    """The sections to show: ``Unreleased``, the installed version, and what is newer than *last_seen*.

    A build that has no section of its own (a source tree, ``0+unknown``) shows
    the newest release instead, so the pager is never empty while the
    changelog has content.
    """
    installed_key = version_key(installed)
    seen_key = version_key(last_seen)
    releases = [entry for entry in entries if not entry.unreleased]
    chosen = [
        entry
        for entry in releases
        if version_key(entry.version) == installed_key
        or (bool(seen_key) and seen_key < version_key(entry.version) <= installed_key)
    ]
    if not chosen:
        chosen = releases[:1]
    keep = {id(entry) for entry in chosen}
    return tuple(entry for entry in entries if entry.unreleased or id(entry) in keep)
