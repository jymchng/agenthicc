"""Idempotent user-facing projections for tool-exchange recovery (PRD-191)."""

from __future__ import annotations

from agenthicc.memory.journal import tool_exchange_recovery_event_id

__all__ = ["ToolRecoveryProjector"]


class ToolRecoveryProjector:
    """Project each durable tool-recovery fact into a session once.

    The provider runner may discover the same repair during interruption,
    preflight, and resume. The journal is authoritative across processes;
    ``ConversationStore`` supplies an additional in-process event-ID guard.
    """

    def __init__(self, conversation_store: object, journal: object | None = None) -> None:
        self._conversation_store = conversation_store
        self._seen: set[str] = set()
        # The journal records that a repair occurred; the session transcript
        # records whether its user-facing projection was rendered.  Do not
        # treat the former as the latter, or a restart could suppress the only
        # notice when the process crashed after recovery but before UI append.
        del journal

    def project_repaired(self, signal: object) -> bool:
        """Project one repaired exchange and return whether it was new."""
        exchange_id = str(getattr(signal, "exchange_id", "") or "")
        if not exchange_id:
            return False
        event_id = str(
            getattr(signal, "event_id", "")
            or tool_exchange_recovery_event_id(exchange_id, "repaired")
        )
        if event_id in self._seen:
            return False
        has_event_id = getattr(self._conversation_store, "_has_event_id", None)
        if callable(has_event_id) and has_event_id(event_id):
            self._seen.add(event_id)
            return False
        self._seen.add(event_id)
        append_event = getattr(self._conversation_store, "append_event", None)
        if not callable(append_event):
            return False
        payload = {
            "event_id": event_id,
            "exchange_id": exchange_id,
            "status": "repaired",
            "call_count": int(getattr(signal, "call_count", 0) or 0),
            "text": (
                "Tool execution was interrupted; incomplete tool results were "
                "recorded so the session can continue safely."
            ),
        }
        try:
            append_event("tool_recovery", payload, event_id=event_id)
        except TypeError:
            # Compatibility with pre-PRD-191 conversation-store doubles. The
            # projector's local seen set still prevents duplicate calls.
            append_event("tool_recovery", payload)
        return True

    def project_legacy_repair(self, *, exchange_id: str, call_count: int = 0) -> bool:
        """Project a repair discovered by an old memory adapter."""
        if not exchange_id:
            return False
        signal = type(
            "LegacyToolRecoverySignal",
            (),
            {"exchange_id": exchange_id, "event_id": "", "call_count": call_count},
        )()
        return self.project_repaired(signal)
