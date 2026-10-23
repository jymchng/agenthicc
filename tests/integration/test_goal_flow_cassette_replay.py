"""Hermetic replay of a recorded goal_flow campaign.

The fixture in ``tests/fixtures/goal_flow_campaign/`` is a real 547-request
recording. Replaying it drives the *current* goal_flow state machine, agent
turn, tool routing, capability metadata, and approval handling with the exact
model responses that were recorded — including six implement/verify cycles and
live goal-list mutations — while every side-effecting built-in tool is replaced
by a schema-identical stub. Any change to the workflow's phase graph, its
transition tools, the tool catalog, or a tool's accepted arguments makes the
replay diverge from the recording, and this test fails.

See the fixture README for the shape of the recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.testing import SessionCassette, run_headless_replay

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "goal_flow_campaign"
CASSETTE = FIXTURE / "cassette.jsonl"
APPROVALS = FIXTURE / "approvals.jsonl"

#: First request of the fresh goal_flow run; earlier entries finish another run.
RUN_START = 34
#: Approvals answered before RUN_START (the ask_user call at entry 19).
APPROVALS_BEFORE_RUN = 1

#: The goal_flow tools that change workflow state, plus the question tool.
TRANSITION_TOOLS = frozenset(
    {
        "ask_user",
        "complete_clarification",
        "finalize_goals",
        "goal_implemented",
        "verify_goal",
        "append_goal",
        "insert_goal",
        "complete_workflow",
    }
)

#: Recorded calls that were already invalid when recorded (see fixture README).
KNOWN_RECORDED_MODEL_ERRORS = ("entry 405: patch_file:",)


def _campaign() -> SessionCassette:
    return SessionCassette.from_path(CASSETTE, APPROVALS).slice(
        RUN_START, skip_approvals=APPROVALS_BEFORE_RUN
    )


def test_recording_has_the_documented_shape() -> None:
    cassette = SessionCassette.from_path(CASSETTE, APPROVALS)

    assert len(cassette.entries) == 547
    assert len(cassette.approvals) == 6
    assert [entry.index for entry in cassette.entries] == list(range(547))
    assert {entry.index for entry in cassette.entries if entry.is_tool_free} == {
        0,
        2,
        4,
        199,
        339,
        499,
    }
    valid_stops = {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
    assert all(entry.response_stop_reason in valid_stops for entry in cassette.entries)

    run_start = cassette.entries[RUN_START]
    previous = cassette.entries[RUN_START - 1]
    assert "complete_clarification" in run_start.tool_names_available
    assert "complete_clarification" not in previous.tool_names_available
    assert sum(
        entry.called_tool_names.count("ask_user") for entry in cassette.entries[:RUN_START]
    ) == (APPROVALS_BEFORE_RUN)
    meta = json.loads((FIXTURE / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == "84348746-7ff0-43a5-b2c3-0cd9e04a9ff8"


def test_slice_drops_the_earlier_run_and_its_answers() -> None:
    campaign = _campaign()

    assert campaign.entries[0].index == RUN_START
    assert len(campaign.entries) == 547 - RUN_START
    assert len(campaign.approvals) == 5
    assert len(campaign.replay_entries(skip_tool_free=True)) == 510
    with pytest.raises(ValueError):
        campaign.slice(0, skip_approvals=6)


# The replay serves 510 recorded responses through the real runner. It needs
# minutes of CPU, about three times as much under branch coverage, and a loaded
# host stretches the wall time further; these ceilings only stop a hang.
@pytest.mark.timeout(1560)
async def test_goal_flow_campaign_replays_hermetically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    campaign = _campaign()
    replayed_entries = campaign.replay_entries(skip_tool_free=True)

    result = await run_headless_replay(
        campaign,
        workflow="goal_flow",
        intent="Continue the documentation campaign.",
        cfg=AgenthiccConfig(),
        hermetic=True,
        mode="Yolo",
        timeout_s=1500,
    )

    # The workflow consumed every recorded response and asked for more: the
    # recording ends mid-campaign while goal 7 is being implemented.
    assert result.error is None
    assert result.status == "exhausted", result
    assert result.transport_calls == len(replayed_entries) == 510
    assert result.skipped_entries == 3  # summaries at 199, 339, 499

    # Exactly the recorded questions were asked, in order; no tool approval
    # was raised in Yolo mode, so no answer came from the replay fallback.
    assert result.approvals_consumed == len(campaign.approvals) == 5
    assert result.approvals_unrecorded == 0

    # The state machine accepted every recorded transition, in recorded order.
    expected = [
        name
        for entry in replayed_entries
        for name in entry.called_tool_names
        if name in TRANSITION_TOOLS
    ]
    replayed = [exchange for exchange in result.tool_exchanges if exchange.name in TRANSITION_TOOLS]
    assert [exchange.name for exchange in replayed] == expected
    rejected = [(exchange.name, exchange.result) for exchange in replayed if not exchange.accepted]
    assert not rejected, rejected
    assert expected.count("complete_clarification") == 1
    assert expected.count("finalize_goals") == 1
    assert expected.count("goal_implemented") == 6
    assert expected.count("verify_goal") == 6
    assert expected.count("append_goal") == 4
    assert expected.count("insert_goal") == 1

    # Every recorded call still routes to an offered tool with the same
    # arguments; the only violation is the model error present at recording.
    assert len(result.contract_violations) == len(KNOWN_RECORDED_MODEL_ERRORS), (
        result.contract_violations
    )
    for violation, known in zip(
        sorted(result.contract_violations), sorted(KNOWN_RECORDED_MODEL_ERRORS), strict=True
    ):
        assert violation.startswith(known), violation

    # Hermetic: nothing was written into the working directory.
    assert list(tmp_path.iterdir()) == []


async def test_unknown_workflow_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError, match="unknown workflow"):
        await run_headless_replay(
            _campaign(),
            workflow="no_such_workflow",
            intent="x",
            cfg=AgenthiccConfig(),
        )


async def test_unknown_mode_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="unknown mode"):
        await run_headless_replay(
            _campaign(),
            workflow="goal_flow",
            intent="x",
            cfg=AgenthiccConfig(),
            mode="Turbo",
        )
