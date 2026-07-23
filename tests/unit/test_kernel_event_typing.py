"""Typed event decoding and reducer-ingress validation coverage."""

from __future__ import annotations

import pytest

from agenthicc.kernel.events import Event

pytestmark = pytest.mark.unit


def _event_dict() -> dict[str, object]:
    return {
        "event_id": "event-1",
        "event_type": "IntentCreated",
        "timestamp": 123.5,
        "payload": {"intent_id": "intent-1", "raw_text": "Fix the bug"},
        "source_agent_id": None,
        "tool_call_id": None,
    }


def test_event_from_dict_validates_and_round_trips_typed_fields() -> None:
    event = Event.from_dict(_event_dict())

    assert event.event_id == "event-1"
    assert event.timestamp == 123.5
    assert event.payload["intent_id"] == "intent-1"
    assert event.to_dict()["payload"] == _event_dict()["payload"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", 123),
        ("event_type", None),
        ("timestamp", "not-a-number"),
        ("payload", ["not", "an", "object"]),
        ("source_agent_id", 123),
    ],
)
def test_event_from_dict_rejects_invalid_outer_fields(field: str, value: object) -> None:
    data = _event_dict()
    data[field] = value

    with pytest.raises(ValueError, match="event payload field"):
        Event.from_dict(data)


def test_event_from_dict_rejects_non_string_mapping_keys() -> None:
    data = _event_dict()
    data["payload"] = {1: "invalid key"}

    with pytest.raises(ValueError, match="payload"):
        Event.from_dict(data)
