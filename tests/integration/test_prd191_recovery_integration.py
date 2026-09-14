"""Persistence-boundary coverage for PRD-191 recovery projections."""

from __future__ import annotations

import pytest

from agenthicc.runners.recovery_projection import ToolRecoveryProjector
from agenthicc.tui.conversation_store import ConversationStore
from agenthicc.tui.runtime.session_log import SessionEventLog

pytestmark = pytest.mark.integration


def test_recovery_projection_survives_session_log_rehydration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "prd191-recovery"
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log._SESSION_INDEX",
        tmp_path / "sessions" / "index.json",
    )

    store = ConversationStore()
    session_log = SessionEventLog(session_id)
    store.on_event(session_log.append)
    projector = ToolRecoveryProjector(store)
    signal = type(
        "RecoverySignal",
        (),
        {"exchange_id": "exchange-1", "event_id": "", "call_count": 1},
    )()

    assert projector.project_repaired(signal) is True
    session_log.close()

    restored_events = SessionEventLog.load(session_id, rendered=False)
    assert len(restored_events) == 1
    assert restored_events[0].event_id
    assert restored_events[0].kind == "tool_recovery"

    restored_store = ConversationStore()
    restored_store.remember_events(restored_events)
    restored_projector = ToolRecoveryProjector(restored_store)
    assert restored_projector.project_repaired(signal) is False
    assert restored_store._has_event_id(restored_events[0].event_id)
