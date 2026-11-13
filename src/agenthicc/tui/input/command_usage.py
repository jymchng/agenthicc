"""How often and how recently each command ran, per project (TUX-05 §2.2).

The ``/`` picker and the Ctrl+P palette break ranking ties with these counts
and list the five most used commands under *Suggested*. They live with the
rest of the user's local state in ``~/.agenthicc/state/command-usage.json``
(``AGENTHICC_COMMAND_USAGE`` overrides the path), keyed by project directory,
so one project's habits do not reorder another's picker.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

__all__ = ["CommandUsage", "default_usage_path"]

#: Commands remembered per project; the least recently used are dropped first.
COMMAND_LIMIT = 200
#: Projects remembered; the least recently active are dropped first.
PROJECT_LIMIT = 100


def default_usage_path() -> Path:
    """``~/.agenthicc/state/command-usage.json``, or ``AGENTHICC_COMMAND_USAGE``."""
    override = os.environ.get("AGENTHICC_COMMAND_USAGE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agenthicc" / "state" / "command-usage.json"


_Counts = dict[str, tuple[int, float]]


def _parse(raw: object) -> dict[str, _Counts]:
    projects: dict[str, _Counts] = {}
    if not isinstance(raw, dict):
        return projects
    for project, commands in raw.items():
        if not isinstance(project, str) or not isinstance(commands, dict):
            continue
        counts: _Counts = {}
        for name, entry in commands.items():
            if (
                isinstance(name, str)
                and isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], int)
                and isinstance(entry[1], int | float)
            ):
                counts[name] = (entry[0], float(entry[1]))
        projects[project] = counts
    return projects


class CommandUsage:
    """Run counts and last-run times of the commands used in one project."""

    def __init__(self, project: Path, path: Path | None = None) -> None:
        self._project = str(project.resolve())
        self._path = path or default_usage_path()
        self._projects: dict[str, _Counts] | None = None

    def _load(self) -> dict[str, _Counts]:
        if self._projects is None:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            self._projects = _parse(raw)
        return self._projects

    def counts(self) -> Mapping[str, tuple[int, float]]:
        """Command name → ``(count, last_used)`` for this project."""
        return dict(self._load().get(self._project, {}))

    def most_used(self, limit: int = 5) -> list[str]:
        """The *limit* most used commands, most used (then most recent) first."""
        ranked = sorted(self.counts().items(), key=lambda item: (-item[1][0], -item[1][1]))
        return [name for name, _ in ranked[:limit]]

    def record(self, name: str, *, now: float | None = None) -> None:
        """Count one run of *name* and save; a write failure is not an error."""
        projects = self._load()
        counts = projects.setdefault(self._project, {})
        count, _ = counts.get(name, (0, 0.0))
        counts[name] = (count + 1, time.time() if now is None else now)
        if len(counts) > COMMAND_LIMIT:
            for stale, _ in sorted(counts.items(), key=lambda item: item[1][1])[
                : len(counts) - COMMAND_LIMIT
            ]:
                del counts[stale]
        if len(projects) > PROJECT_LIMIT:
            latest = {
                key: max((v[1] for v in value.values()), default=0.0)
                for key, value in projects.items()
            }
            for stale in sorted(latest, key=latest.__getitem__)[: len(projects) - PROJECT_LIMIT]:
                del projects[stale]
        payload = {
            project: {command: [entry[0], entry[1]] for command, entry in commands.items()}
            for project, commands in projects.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            return
