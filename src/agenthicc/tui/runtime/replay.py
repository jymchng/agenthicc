"""ConversationReplayer — feeds historical session events through the render pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.tui.runtime.mode_manager import ModeManager

from agenthicc.tui.runtime.session_log import get_session_log_path
from agenthicc.tui.conversation_store import ConversationEvent

log = logging.getLogger(__name__)


def load_for_replay(session_id: str) -> list[tuple[str, dict[str, object]]]:
    """Load (kind, payload) pairs from a session log with rendered=False.

    Unlike ``SessionEventLog.load()`` which sets ``rendered=True`` to avoid
    re-displaying on ``--resume``, this function loads events fresh so
    ``ScrollBufferAppender`` will render each one when it is re-injected into
    the ConversationStore.

    Returns an empty list if the session log does not exist.
    """
    path: Path = get_session_log_path(session_id)
    if not path.exists():
        return []
    pairs: list[tuple[str, dict[str, object]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if (
                isinstance(data, dict)
                and isinstance(data.get("kind"), str)
                and isinstance(data.get("payload"), dict)
            ):
                pairs.append((data["kind"], data["payload"]))
        except Exception:  # noqa: BLE001
            pass
    return pairs


def _load_events_for_replay(session_id: str) -> list[ConversationEvent]:
    """Load replay records while retaining their projection identities.

    ``load_for_replay`` remains a pair-based compatibility API.  The active
    replayer uses the richer event form so ``ConversationStore`` can reject a
    record that was already projected into this session.
    """
    path: Path = get_session_log_path(session_id)
    if not path.exists():
        return []
    events: list[ConversationEvent] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if (
                isinstance(data, dict)
                and isinstance(data.get("kind"), str)
                and isinstance(data.get("payload"), dict)
            ):
                event_id = data.get("event_id")
                events.append(
                    ConversationEvent(
                        event_id=(
                            event_id
                            if isinstance(event_id, str) and event_id
                            else f"legacy-replay-{index}"
                        ),
                        kind=data["kind"],
                        payload=data["payload"],
                        rendered=False,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
    return events


class ConversationReplayer:
    """Feeds stored session events through the existing render pipeline.

    Usage::

        replayer = ConversationReplayer(session_id, conv_store, mode_manager)
        await replayer.run()

    Each event is re-injected via ``conv_store.append_event`` so subscribers
    (``ScrollBufferAppender``) pick it up and render it exactly as they did
    originally.  No new rendering code is needed.
    """

    def __init__(
        self,
        session_id: str,
        conv_store: ConversationStore,
        mode_manager: ModeManager,
    ) -> None:
        self._session_id: str = session_id
        self._conv_store: ConversationStore = conv_store
        self._mode_manager: ModeManager = mode_manager

    async def run(self) -> None:
        """Replay all events then show a completion notification."""
        events = _load_events_for_replay(self._session_id)
        if not events:
            self._conv_store.notification.set(
                f"No conversation log found for session {self._session_id[:12]}."
            )
            return

        self._conv_store.notification.set(f"⏮ Replaying session {self._session_id[:12]}…")

        for event in events:
            try:
                self._conv_store.append_event(
                    event.kind,
                    event.payload,
                    event_id=event.event_id,
                )
            except TypeError:
                # Older store adapters accepted only (kind, payload).  They
                # remain supported, while the current store gets the stable
                # identity needed for replay idempotency.
                self._conv_store.append_event(event.kind, event.payload)
            # Yield to the event loop so ScrollBufferAppender can flush each
            # render before the next event arrives, keeping the output ordered.
            await asyncio.sleep(0)

        self._conv_store.notification.set(
            f"⏮ Replay complete ({len(events)} events) — "
            "session context restored. Send a message to continue."
        )
