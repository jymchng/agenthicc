"""agenthicc persistence and cassette coverage for PRD-190."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, TokenUsage

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.testing.cassette import CassetteEntry, SessionCassette
from agenthicc.testing.recording_transport import RecordingTransport

pytestmark = pytest.mark.unit


def _completion() -> Completion:
    completion_type = cast(Any, Completion)
    return cast(
        Completion,
        completion_type(
            id="reasoning-1",
            model="gateway-model",
            content="",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            reasoning_content="exact provider reasoning",
        ),
    )


@pytest.mark.asyncio
async def test_recording_transport_preserves_reasoning_content(tmp_path: Path) -> None:
    from lauren_ai._transport._mock import MockTransport

    inner = MockTransport()
    inner.queue_response(_completion())
    cassette_path = tmp_path / "cassette.jsonl"
    recorder = RecordingTransport(inner, cassette_path)

    await recorder.complete([], model="gateway-model")

    recorded = json.loads(cassette_path.read_text(encoding="utf-8"))
    assert recorded["response"]["reasoning_content"] == "exact provider reasoning"

    cassette = SessionCassette.from_path(cassette_path)
    replay = cassette.to_mock_transport()
    replay_result = await replay.complete([], model="gateway-model", stream=True)
    replayed_chunks = [chunk async for chunk in cast(AsyncIterator[Any], replay_result)]
    assert vars(replayed_chunks[0]).get("reasoning_content_delta") == "exact provider reasoning"


def test_journaled_memory_round_trips_reasoning_content(tmp_path: Path) -> None:
    journal_path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(journal_path)
    memory = JournaledShortTermMemory(journal, max_tokens=100_000)
    try:
        memory.add_assistant(_completion())
    finally:
        memory.close()

    resumed = JournaledShortTermMemory(ConversationJournal(journal_path), max_tokens=100_000)
    try:
        assert resumed.messages()[0]["reasoning_content"] == "exact provider reasoning"
    finally:
        resumed.close()


def test_plain_short_term_memory_keeps_optional_field_json_safe() -> None:
    memory = ShortTermMemory(max_tokens=100_000)
    memory.add_assistant(_completion())
    snapshot = memory.snapshot()
    assert snapshot["messages"][0]["reasoning_content"] == "exact provider reasoning"


def test_malformed_cassette_reasoning_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="reasoning_content must be a string"):
        CassetteEntry.from_dict(
            {
                "response": {
                    "content": "",
                    "stop_reason": "end_turn",
                    "tool_calls": [],
                    "reasoning_content": {"unexpected": "structure"},
                }
            }
        )
