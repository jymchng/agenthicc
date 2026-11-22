"""Durable workflow frames are a pure projection of kernel events."""

from collections.abc import Mapping

from agenthicc.kernel import AppState, Event, root_reducer
from agenthicc.runners.workflow_return_delivery import workflow_digest


def _push(frame: str, parent: str | None, depth: int) -> Event:
    return Event.create(
        "workflow_frame_pushed",
        {
            "frame_id": frame,
            "parent_frame_id": parent,
            "workflow_name": frame,
            "run_id": f"run-{frame}",
            "origin": "agent",
            "depth": depth,
            "intent_digest": workflow_digest("hello"),
        },
    )


def test_workflow_frame_reducer_replays_nested_returns_without_side_effects() -> None:
    initial = AppState.create()
    state = initial
    assert workflow_digest("hello") == "2cf24dba5fb0a30e"
    events = [
        _push("root", None, 0),
        _push("parent", "root", 1),
        _push("child", "parent", 2),
        Event.create(
            "workflow_frame_returned",
            {
                "frame_id": "child",
                "status": "completed",
                "summary_digest": workflow_digest("completed"),
                "artifact_names": ["report"],
            },
        ),
        Event.create("workflow_return_delivered", {"return_key": "return:parent:run-child"}),
    ]
    for event in events:
        original: Mapping[str, object] = dict(event.payload)
        state, effects = root_reducer(state, event)
        assert effects == [] and original == event.payload
    assert not initial.workflow_frames
    root, parent, child = state.workflow_frames
    assert (root.status, parent.status, child.status) == ("suspended", "running", "completed")
    assert child.delivered and child.artifact_names == ("report",)
    replay = initial
    for event in events:
        replay, _ = root_reducer(replay, Event.from_dict(event.to_dict()))
    assert replay.workflow_frames == state.workflow_frames
