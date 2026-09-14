"""PRD-191 recovery receipt and transcript projection coverage."""

from __future__ import annotations

import json

import pytest

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.recovery_projection import ToolRecoveryProjector
from agenthicc.tui.conversation_store import ConversationEvent, ConversationStore
from agenthicc.tui.runtime.replay import ConversationReplayer


def test_projector_deduplicates_a_repeated_recovery_signal() -> None:
    store = ConversationStore()
    projector = ToolRecoveryProjector(store)
    signal = type(
        "RecoverySignal",
        (),
        {"exchange_id": "exchange-1", "event_id": "stable-recovery-1", "call_count": 2},
    )()

    assert projector.project_repaired(signal) is True
    assert projector.project_repaired(signal) is False
    events = [event for turn in store.turns() for event in turn.events]
    assert len(events) == 0

    # The store keeps the event projection even when no UI turn is open.
    # It is accessible through the event-ID guard once the next event arrives.
    event = store.append_event(
        "tool_recovery",
        {"text": "duplicate"},
        event_id="stable-recovery-1",
    )
    assert event.payload["text"].startswith("Tool execution")


def test_conversation_store_rejects_duplicate_event_ids() -> None:
    store = ConversationStore()
    first = store.append_event("system", {"text": "once"}, event_id="event-1")
    second = store.append_event("system", {"text": "twice"}, event_id="event-1")
    assert second is first


def test_recovery_receipt_is_durable_and_redacted(tmp_path) -> None:
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    memory = JournaledShortTermMemory(journal)
    memory.on_tool_exchange_aborted(
        type(
            "Exchange",
            (),
            {"exchange_id": "exchange-1", "call_ids": ("secret-call-id",)},
        )(),
        repaired=True,
    )
    journal.close()

    records = [
        json.loads(line) for line in (tmp_path / "conversation.jsonl").read_text().splitlines()
    ]
    receipt = next(item for item in records if item["kind"] == "tool_exchange_aborted")
    assert receipt["event_id"]
    assert "secret-call-id" not in json.dumps(receipt)


def test_replayed_event_ids_seed_the_store_without_notification() -> None:
    store = ConversationStore()
    event = ConversationEvent(
        event_id="historical-recovery",
        kind="tool_recovery",
        payload={"text": "already shown"},
    )
    store.remember_events([event])
    duplicate = store.append_event(
        "tool_recovery",
        {"text": "should not replace"},
        event_id="historical-recovery",
    )
    assert duplicate is event


@pytest.mark.asyncio
async def test_legacy_replayer_preserves_ids_and_is_idempotent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversation.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "historical-recovery",
                "kind": "tool_recovery",
                "payload": {"text": "already shown"},
                "timestamp": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agenthicc.tui.runtime.replay.get_session_log_path", lambda _session_id: path
    )
    store = ConversationStore()
    observed = []
    store.on_event(observed.append)
    replayer = ConversationReplayer("session", store, object())

    await replayer.run()
    await replayer.run()

    assert len(observed) == 1
    assert observed[0].event_id == "historical-recovery"
