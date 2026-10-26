"""What the agents hub remembers per user (AGX-06 §5.3, §5.5).

The chosen sort and grouping, the order of pinned agents, and what the user
has seen of each agent's activity — so a row whose activity changed since the
user last looked is marked across reopenings of the hub.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from agenthicc.background.model import BackgroundSession
from agenthicc.tui.workspace.agents_hub.model import GroupBy, SortKey

__all__ = ["HubPrefs", "activity_mark", "default_prefs_path"]

#: Agents whose seen activity is remembered; the least recently changed go first.
SEEN_LIMIT = 2_000


def default_prefs_path() -> Path:
    """``~/.agenthicc/state/agents-hub.json``; ``AGENTHICC_AGENTS_HUB_STATE`` overrides it."""
    override = os.environ.get("AGENTHICC_AGENTS_HUB_STATE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agenthicc" / "state" / "agents-hub.json"


def activity_mark(session: BackgroundSession) -> str:
    """What "the agent's activity" is for the unseen mark.

    Heartbeats move ``last_active`` constantly; a change of state, attempt or
    reported activity is what a user would call new.
    """
    return f"{session.status.value}|{session.attempt}|{session.latest_activity}"


@dataclass
class HubPrefs:
    sort: SortKey = SortKey.ATTENTION
    group_by: GroupBy = GroupBy.STATUS
    #: Pinned agents, top first; agents not listed follow by recent activity.
    pin_order: list[str] = field(default_factory=list)
    #: Session ID → the activity mark the user last saw.
    seen: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> HubPrefs:
        path = path or default_prefs_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path)
        if not isinstance(raw, dict):
            return cls(path=path)
        prefs = cls(path=path)
        try:
            prefs.sort = SortKey(raw.get("sort", SortKey.ATTENTION.value))
        except ValueError:
            pass
        try:
            prefs.group_by = GroupBy(raw.get("group_by", GroupBy.STATUS.value))
        except ValueError:
            pass
        order = raw.get("pin_order")
        if isinstance(order, list):
            prefs.pin_order = [item for item in order if isinstance(item, str)]
        seen = raw.get("seen")
        if isinstance(seen, dict):
            prefs.seen = {
                key: value
                for key, value in seen.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        return prefs

    def save(self) -> None:
        """Write atomically; a failure only loses what the hub remembers."""
        path = self.path or default_prefs_path()
        seen = dict(list(self.seen.items())[-SEEN_LIMIT:])
        payload = {
            "sort": self.sort.value,
            "group_by": self.group_by.value,
            "pin_order": self.pin_order[-SEEN_LIMIT:],
            "seen": seen,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            return

    def unseen(self, session: BackgroundSession) -> bool:
        """Whether *session* changed since the user last looked.

        An agent the hub has never shown is not new: its first sighting is
        remembered instead.
        """
        mark = activity_mark(session)
        known = self.seen.get(session.session_id)
        if known is None:
            self.seen[session.session_id] = mark
            return False
        return known != mark

    def mark_seen(self, session: BackgroundSession) -> None:
        self.seen.pop(session.session_id, None)
        self.seen[session.session_id] = activity_mark(session)
