"""End-to-end recovery projection and replay journey for PRD-191."""

from __future__ import annotations

import pytest

from agenthicc.runners.recovery_projection import ToolRecoveryProjector
from agenthicc.tui.conversation_store import ConversationStore
from agenthicc.tui.runtime.replay import ConversationReplayer
from agenthicc.tui.runtime.session_log import SessionEventLog

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_interrupted_exchange_is_rendered_once_after_resume(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "prd191-e2e"
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
        {"exchange_id": "exchange-1", "event_id": "", "call_count": 2},
    )()
    assert projector.project_repaired(signal) is True
    session_log.close()

    resumed = ConversationStore()
    observed = []
    resumed.on_event(observed.append)
    replayer = ConversationReplayer(session_id, resumed, object())
    await replayer.run()
    await replayer.run()

    assert len(observed) == 1
    assert observed[0].kind == "tool_recovery"
    assert observed[0].payload["exchange_id"] == "exchange-1"
