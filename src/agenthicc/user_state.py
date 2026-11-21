"""What agenthicc remembers about this machine's user between runs (TUX-03).

``~/.agenthicc/state.json`` holds three small facts, none of them secret:

- ``version`` — the version that last started. No file (or no version in it)
  is the first run on this machine, which shows the mascot once; a different
  version means the user upgraded, so the header says what's new once.
- ``release_notes_seen`` — the newest version whose notes ``/release-notes``
  showed, so the next view lists everything since.
- ``tip_index`` — how many sessions have started, which rotates the composer's
  tip deterministically.

The file is user-owned state, never configuration: it is rewritten whole and
atomically, an unreadable one reads as a fresh machine, and a failed write is
ignored (a read-only home just repeats the header's first-run extras).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from agenthicc.runners.process_lease import atomic_replace, read_json_object

__all__ = ["UserState", "load_user_state", "save_user_state", "state_path"]


def state_path() -> Path:
    """``~/.agenthicc/state.json`` (resolved when called, so tests can move HOME)."""
    return Path.home() / ".agenthicc" / "state.json"


@dataclass(frozen=True, slots=True)
class UserState:
    """The persisted facts. Every field defaults to "never recorded"."""

    version: str = ""
    release_notes_seen: str = ""
    tip_index: int = 0

    @property
    def first_run(self) -> bool:
        """No version was ever recorded: this is the first run on the machine."""
        return not self.version

    def upgraded_to(self, installed: str) -> bool:
        """The user ran an older version before: say what's new, once."""
        return bool(self.version) and self.version != installed

    def started(self, installed: str) -> UserState:
        """The state after a session of *installed* started."""
        return replace(
            self,
            version=installed,
            release_notes_seen=self.release_notes_seen or installed,
            tip_index=self.tip_index + 1,
        )


def load_user_state(path: Path | None = None) -> UserState:
    """The saved state, or a fresh one when the file is missing or unreadable."""
    raw = read_json_object(path or state_path())
    if not raw:
        return UserState()
    version = raw.get("version")
    seen = raw.get("release_notes_seen")
    index = raw.get("tip_index")
    return UserState(
        version=version if isinstance(version, str) else "",
        release_notes_seen=seen if isinstance(seen, str) else "",
        tip_index=index if isinstance(index, int) and not isinstance(index, bool) else 0,
    )


def save_user_state(state: UserState, path: Path | None = None) -> bool:
    """Write *state* atomically; ``False`` when the home directory is not writable."""
    document = {
        "version": state.version,
        "release_notes_seen": state.release_notes_seen,
        "tip_index": state.tip_index,
    }
    try:
        atomic_replace(
            path or state_path(),
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            prefix=".state-",
        )
    except OSError:
        return False
    return True
