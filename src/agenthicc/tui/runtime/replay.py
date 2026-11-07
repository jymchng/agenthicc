"""ConversationReplayer — feeds historical session events through the render pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.tui.runtime.mode_manager import ModeManager

from agenthicc.tui.runtime.session_log import SessionEventLog

log = logging.getLogger(__name__)

#: The transcript event that brackets replayed history (TUX-06 §2.6).
REPLAY_SEPARATOR = "replay_separator"


class ConversationReplayer:
    """Feeds stored session events through the existing render pipeline.

    Usage::

        replayer = ConversationReplayer(session_id, conv_store, mode_manager)
        await replayer.run()

    Each event is re-injected via ``conv_store.append_event`` with its stored
    identity and **original timestamp**, so subscribers
    (``ScrollBufferAppender``) render it with the time it happened, not the
    time of the replay.  A ``replay_separator`` event opens the replayed
    history (``── replayed from 2 Nov 05:04 · 827 events ──``).
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
        events = SessionEventLog.load(self._session_id, rendered=False)
        if not events:
            self._conv_store.notification.set(
                f"No conversation log found for session {self._session_id[:12]}."
            )
            return

        self._conv_store.notification.set(f"⏮ Replaying session {self._session_id[:12]}…")
        self._conv_store.append_event(
            REPLAY_SEPARATOR,
            {"edge": "start", "since": events[0].timestamp, "events": len(events)},
            event_id=f"replay-start:{self._session_id}",
        )
        for event in events:
            self._conv_store.append_event(
                event.kind,
                event.payload,
                event_id=event.event_id,
                timestamp=event.timestamp,
            )
            # Yield to the event loop so ScrollBufferAppender can flush each
            # render before the next event arrives, keeping the output ordered.
            await asyncio.sleep(0)

        self._conv_store.notification.set(
            f"⏮ Replay complete ({len(events)} events) — "
            "session context restored. Send a message to continue."
        )
